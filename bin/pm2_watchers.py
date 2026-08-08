#!/usr/bin/env python3
"""
pm2_watchers.py — Manage PM2-based persistent inbox watchers.

PM2 app names use the pattern: {workspace_name}-{agent_id}
(workspace_name from collab.config.json)

Usage:
  python bin/pm2_watchers.py start --agent orchestrator
  python bin/pm2_watchers.py ensure --agent orchestrator   # start if not running
  python bin/pm2_watchers.py status --all
  python bin/pm2_watchers.py stop --agent orchestrator
  python bin/pm2_watchers.py logs --agent orchestrator --lines 50
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import os
import re
import shutil
import subprocess
import time

sys.path.insert(0, str(Path(__file__).parent))
from _ax_trust import format_ax_status, probe_ax_trust
from _helpers import (
    ROOT,
    RUNTIME_ROOT,
    agent_ids,
    canonical_path,
    config_get,
    get_agent,
    watcher_enabled_agents,
)

COMMANDS = ("start", "restart", "ensure", "stop", "delete", "status", "logs")
DEFAULT_PM2_TIMEOUT_SECONDS = 15
SIDECAR_READINESS_TIMEOUT_SECONDS = 15
SIDECAR_READINESS_POLL_SECONDS = 0.25
SIDECAR_READINESS_PROBE_TIMEOUT_SECONDS = 1

# The blank set for token CONTENT, pinned explicitly because neither language's default
# matches the CLI and the two defaults are inverted from each other. Python's str.strip()
# treats U+0085 as blank and U+FEFF as content; JavaScript's String.trim() does the exact
# opposite. The CLI trims Rust's Unicode White_Space -- U+0085 in, U+FEFF out -- so a
# BOM-only token is content it accepts and a NEL-only token is empty to it. Enumerated here
# and mirrored by TOKEN_BLANK_PATTERN in pm2/ecosystem.config.cjs so that both gates agree
# with the process they gate rather than with their own language's convention.
TOKEN_BLANK_CHARS = (
    "\t\n\v\f\r \u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def parse_args():
    p = argparse.ArgumentParser(description="Manage PM2 inbox watchers.")
    p.add_argument("command", choices=COMMANDS)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--agent", help="Agent ID")
    g.add_argument("--all", action="store_true", help="Apply to all watcher-enabled agents")
    p.add_argument("--lines", type=int, default=40, help="Lines for logs command")
    return p.parse_args()


def resolve_pm2() -> str | None:
    return shutil.which("pm2")


def app_name(agent_id: str) -> str:
    workspace = config_get("workspace_name", "collab")
    return f"{workspace}-{agent_id}"


# Sidecar transports are PM2 apps but not inbox-watcher agents: they have no
# agents.json entry and no AX surface. They are addressed by app suffix so
# app_name() already resolves them, and gated on the same preconditions the
# ecosystem config uses so `--all` never targets an app the config omits.
SIDECAR_APP_IDS = ("codex-appserver",)


def sidecar_token_file() -> Path:
    configured = os.environ.get("LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE")
    if configured:
        return canonical_path(configured)
    return canonical_path(ROOT / ".secrets" / "codex_app_server_ws_token")


def sidecar_binary() -> Path:
    configured = os.environ.get("LLM_COLLAB_CODEX_BIN")
    if configured:
        return canonical_path(configured)
    return canonical_path("/Applications/ChatGPT.app/Contents/Resources/codex")


def token_is_usable(content: str) -> bool:
    """True when BOTH the CLI and our delivery client can use this secret.

    Agreeing with the CLI alone is not enough. The CLI accepts a BOM-only or U+001C-only
    token, but the delivery client puts the secret in an HTTP Authorization header: a BOM
    cannot be encoded there, an interior CRLF would split the header, and U+001C is not
    sendable -- so the gate would report a transport that cannot authenticate.

    The accepted token is the INTERSECTION: non-blank under the pinned CLI predicate, and
    every remaining character printable ASCII. Delegates to the delivery client's own
    implementation so the two cannot drift; a second copy of a two-sided predicate is how
    every earlier round of this concern went wrong.
    """
    from _session_autobridge import token_is_usable as client_token_is_usable

    return client_token_is_usable(content)


def sidecar_token_is_secure(path: Path) -> bool:
    """Mirror the ecosystem config's gate: owner-only regular file, no whitespace.

    The bearer token authorizes App Server turns on the operator's real Codex
    account, and a whitespace path is truncated by delivery discovery's flattened
    `ps` parsing, which would connect with no token at all.
    """
    if re.search(r"\s", str(path)):
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    if not path.is_file():
        return False
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        # POSIX-only, as on Windows. Fail closed on the sidecar rather than taking the
        # manager down before unrelated watchers are handled. The ecosystem config
        # already had this guard; this mirror was missing it.
        return False
    if info.st_uid != getuid():
        return False
    if (info.st_mode & 0o077) != 0:
        return False
    # An empty or whitespace-only token passes every path and mode check, and the Codex
    # CLI then exits immediately with "websocket auth secret must not be empty" -- so PM2
    # burns its restart budget while the manager still reports the transport configured.
    # An interrupted token rotation produces exactly this file.
    #
    # Decoding must be STRICT. errors="replace" turns invalid bytes into U+FFFD, which is
    # non-blank, so a truncated or binary token passed the content check while the CLI
    # exits before listening with "stream did not contain valid UTF-8".
    try:
        content = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return token_is_usable(content)


def enabled_sidecar_ids() -> list[str]:
    """Sidecars eligible to be STARTED. Gate creation only — never cleanup."""
    if sidecar_token_is_secure(sidecar_token_file()) and sidecar_binary().exists():
        return list(SIDECAR_APP_IDS)
    return []


# Commands that inspect or tear down must reach a process that is already running.
# Gating these on the same switch as creation meant that removing the token left a
# live server unreachable by `stop --all` / `delete --all` / `status --all`, which is
# the opposite of what the docs' "token presence is the enable switch" implies:
# removing the token does not stop a server that already loaded the bearer token.
# restart deliberately EXCLUDED: it relaunches the process, so it must satisfy the
# security gate. Treating it as non-creating let a sidecar whose token had become
# group/world-readable be restarted with that insecure token still in place.
NON_CREATING_COMMANDS = frozenset({"stop", "delete", "status", "logs"})


def sidecar_is_pm2_registered(agent_id: str) -> bool:
    """True when PM2 actually knows this app, so cleanup can reach an orphan."""
    name = app_name(agent_id)
    result = pm2_run(["describe", name], capture_output=True)
    text = f"{getattr(result, 'stdout', '') or ''}{getattr(result, 'stderr', '') or ''}".lower()
    if "doesn't exist" in text or "not found" in text:
        return False
    return name.lower() in text


def sidecar_ids_for_command(command: str) -> list[str]:
    """Targets for one command.

    start/ensure use the security gate. Cleanup and inspection must additionally reach
    a sidecar whose token was removed after it started -- but must not invent a target
    on an install that never enabled one, which made `status --all` report a phantom
    sidecar and exit non-zero.
    """
    if command not in NON_CREATING_COMMANDS:
        return enabled_sidecar_ids()
    enabled = set(enabled_sidecar_ids())
    return [
        name for name in SIDECAR_APP_IDS
        if name in enabled or sidecar_is_pm2_registered(name)
    ]


def is_sidecar(target: str) -> bool:
    """True only for a reserved sidecar id that is NOT a registered collaborator.

    A registered agent must always win. Otherwise an agents.json entry literally
    named `codex-appserver` would be silently reclassified as the transport: its AX
    report suppressed, its watcher_enabled flag bypassed, and the ecosystem emitting
    two apps with the same PM2 name.
    """
    if target not in SIDECAR_APP_IDS:
        return False
    try:
        if target in set(agent_ids()):
            return False
    except Exception:
        pass
    return True


def sidecar_id_conflicts() -> list[str]:
    """Reserved sidecar ids that a registered agent has taken over."""
    try:
        registered = set(agent_ids())
    except Exception:
        return []
    return [name for name in SIDECAR_APP_IDS if name in registered]


def pm2_timeout_seconds() -> int:
    raw_timeout = os.environ.get("LLM_COLLAB_PM2_TIMEOUT_SECONDS")
    if not raw_timeout:
        return DEFAULT_PM2_TIMEOUT_SECONDS
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError:
        print(
            f"[warn] Invalid LLM_COLLAB_PM2_TIMEOUT_SECONDS={raw_timeout!r}; "
            f"using {DEFAULT_PM2_TIMEOUT_SECONDS}s",
            file=sys.stderr,
        )
        return DEFAULT_PM2_TIMEOUT_SECONDS
    return max(1, timeout_seconds)


def pm2_run(args_list: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess:
    pm2 = resolve_pm2()
    if not pm2:
        print("[error] pm2 not found. Install: npm install -g pm2", file=sys.stderr)
        sys.exit(1)
    timeout_seconds = pm2_timeout_seconds()
    try:
        return subprocess.run(
            [pm2] + args_list,
            text=True,
            capture_output=capture_output,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[error] pm2 {' '.join(args_list)} timed out after {timeout_seconds}s",
            file=sys.stderr,
        )
        sys.exit(124)


def ecosystem_path() -> Path:
    return RUNTIME_ROOT / "pm2" / "ecosystem.config.cjs"


def codex_sidecar_is_ready() -> bool:
    curl = shutil.which("curl")
    if not curl:
        print("[error] curl not found; cannot probe Codex app-server readiness", file=sys.stderr)
        sys.exit(1)
    port = os.environ.get("LLM_COLLAB_CODEX_APP_SERVER_PORT", "8767")
    try:
        result = subprocess.run(
            [curl, "-s", f"http://127.0.0.1:{port}/readyz"],
            capture_output=True,
            text=True,
            timeout=SIDECAR_READINESS_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def wait_for_codex_sidecar_readiness() -> None:
    deadline = time.monotonic() + SIDECAR_READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if codex_sidecar_is_ready():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(SIDECAR_READINESS_POLL_SECONDS, remaining))
    print(
        f"[error] Codex app-server /readyz did not succeed within "
        f"{SIDECAR_READINESS_TIMEOUT_SECONDS}s",
        file=sys.stderr,
    )
    sys.exit(1)


def start_agent(agent_id: str) -> None:
    if not is_sidecar(agent_id):
        agent = get_agent(agent_id)
        if not agent.get("activation", {}).get("watcher_enabled", False):
            print(f"[skip] {agent_id} has watcher_enabled: false")
            return

    name = app_name(agent_id)
    started = pm2_run(["start", str(ecosystem_path()), "--only", name])
    if started.returncode != 0:
        print(f"[error] pm2 failed to start {name} (exit {started.returncode})", file=sys.stderr)
        sys.exit(started.returncode)

    status = pm2_run(["describe", name], capture_output=True)
    if status.returncode != 0 or "online" not in (status.stdout or "").lower():
        print(f"[error] pm2 started {name} but it is not online", file=sys.stderr)
        sys.exit(status.returncode or 1)


def ensure_agent(agent_id: str) -> None:
    result = pm2_run(["describe", app_name(agent_id)], capture_output=True)
    if "online" in result.stdout.lower():
        print(f"[watcher] {agent_id} already running.")
    else:
        start_agent(agent_id)
    if is_sidecar(agent_id):
        wait_for_codex_sidecar_readiness()


def main():
    args = parse_args()

    for reserved in sidecar_id_conflicts():
        print(
            f"[error] agents.json registers {reserved!r}, which is a reserved transport "
            "sidecar id. Rename the agent: the sidecar and the collaborator would "
            "otherwise share one PM2 app name.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Collaborator AX capability must print BEFORE any PM2-backed discovery: the
    # contract is that a PM2 failure never suppresses an agent's AX report. Resolving
    # sidecar orphans probes PM2, so that resolution is deferred until after the AX
    # lines below. On this operator's checkout a valid token short-circuited the probe
    # entirely, which masked the ordering violation locally.
    targets: list[str] = []
    defer_sidecars = False
    if args.all:
        targets = [a["id"] for a in watcher_enabled_agents()]
        defer_sidecars = True
    elif args.agent:
        if args.agent not in agent_ids() and not is_sidecar(args.agent):
            print(f"[error] Unknown agent: {args.agent!r}", file=sys.stderr)
            sys.exit(1)
        # A direct target bypassed the gate entirely, so `start --agent codex-appserver`
        # with an absent or insecure token reached PM2, which silently omitted the app
        # while the manager exited 0 reporting success.
        if (
            is_sidecar(args.agent)
            and args.command not in NON_CREATING_COMMANDS
            and args.agent not in enabled_sidecar_ids()
        ):
            print(
                f"[error] {args.agent} is not startable: the token file must be a "
                "regular file owned by you with mode 600 and no whitespace in its "
                "path, and the Codex binary must exist.",
                file=sys.stderr,
            )
            sys.exit(2)
        targets = [args.agent]
    else:
        print("[error] Specify --agent or --all", file=sys.stderr)
        sys.exit(1)

    if args.command == "status":
        # Print every target's AX state before invoking PM2. pm2_run exits on a
        # missing binary or timeout, but neither failure may suppress AX status.
        for agent_id in targets:
            if is_sidecar(agent_id):
                # Deliberately NOT an [ax] line: that prefix is the per-agent AX
                # capability contract consumers parse, and a transport sidecar has
                # no Accessibility surface to report on. That is the point of it.
                print(f"[sidecar] target={agent_id} (no AX surface)")
                continue
            print(format_ax_status(probe_ax_trust(get_agent(agent_id)), agent_id=agent_id))

    if defer_sidecars:
        # safe now: every collaborator AX line is already on stdout
        sidecars = sidecar_ids_for_command(args.command)
        targets.extend(sidecars)
        if args.command == "status":
            for name in sidecars:
                print(f"[sidecar] target={name} (no AX surface)")

    status_exit_code = 0
    for agent_id in targets:
        name = app_name(agent_id)
        if args.command == "start":
            start_agent(agent_id)
        elif args.command == "restart":
            # Re-read the deployed ecosystem so the running process matches the
            # definition that the current-runtime gate approved.
            pm2_run(["startOrRestart", str(ecosystem_path()), "--only", name])
        elif args.command == "ensure":
            ensure_agent(agent_id)
        elif args.command == "stop":
            pm2_run(["stop", name])
        elif args.command == "delete":
            pm2_run(["delete", name])
        elif args.command == "status":
            result = pm2_run(["describe", name])
            if result.returncode != 0 and status_exit_code == 0:
                status_exit_code = result.returncode
        elif args.command == "logs":
            pm2_run(["logs", name, "--lines", str(args.lines), "--nostream"])

    if status_exit_code != 0:
        sys.exit(status_exit_code)


if __name__ == "__main__":
    main()
