#!/usr/bin/env python3
"""
pm2_watchers.py — Manage PM2-based persistent inbox watchers.

Enable watchers through docs/workflows/pm2-log-rotation.md so the archive and
rotation gate precedes the first start. The manager implements that workflow;
its usage examples below are non-creating operations only.

PM2 app names use the pattern: {workspace_name}-{agent_id}
(workspace_name from collab.config.json)

Usage:
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
import json
import os
import re
import selectors
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
# Bound on `pm2 jlist` output, the same value bin/deploy_runtime.py uses. jlist is
# an untrusted-size read of the whole process table; without a bound the timeout
# does not prevent excessive memory before json.loads. See _pm2_run_bounded.
PM2_JLIST_MAX_BYTES = 16 * 1024 * 1024
SIDECAR_READINESS_TIMEOUT_SECONDS = 15
SIDECAR_READINESS_POLL_SECONDS = 0.25
SIDECAR_READINESS_PROBE_TIMEOUT_SECONDS = 1
SIDECAR_IDENTITY_TIMEOUT_SECONDS = 5
SIDECAR_IDENTITY_PROCESS_TIMEOUT_SECONDS = 6

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
    p.add_argument("--runtime-home", help="Expected Codex app-server runtime home")
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


def sidecar_is_pm2_registered(agent_id: str, *, snapshot=None) -> bool:
    """True when PM2 actually knows this app, so cleanup can reach an orphan.

    When a jlist snapshot is available (the status command reads one bounded
    snapshot for the whole run), answer from it: `pm2 describe` is a separate,
    unbounded read, and running it before the snapshot would let a large response
    exhaust memory before the jlist bound applies (GH-682). Without a snapshot
    (start/restart/etc.) it falls back to describe -- the GH-684 residual.
    """
    name = app_name(agent_id)
    if snapshot is not None:
        result, entries = snapshot
        if not isinstance(entries, list):
            return False
        return any(isinstance(entry, dict) and entry.get("name") == name for entry in entries)
    result = pm2_run(["describe", name], capture_output=True)
    text = f"{getattr(result, 'stdout', '') or ''}{getattr(result, 'stderr', '') or ''}".lower()
    if "doesn't exist" in text or "not found" in text:
        return False
    return name.lower() in text


def sidecar_ids_for_command(command: str, *, snapshot=None) -> list[str]:
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
        if name in enabled or sidecar_is_pm2_registered(name, snapshot=snapshot)
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


def _pm2_run_bounded(
    pm2: str, args_list: list[str], max_output_bytes: int
) -> subprocess.CompletedProcess:
    """Read pm2 output with a size bound; abort on exceed, never truncate.

    Mirrors bin/deploy_runtime.py's _pm2_run_bounded rather than a second style.
    Used for untrusted-size reads -- `pm2 jlist` is the whole process table --
    where subprocess.run would buffer the entire output before any size check, so
    the timeout does not bound memory before json.loads. Reads in chunks and
    refuses once output exceeds the bound: a truncated buffer that still parsed
    JSON would drop a real watcher and read as absent, which is the fail-open
    GH-678 is about. Abort (sys.exit) on exceed; never return a partial buffer.
    """
    command = [pm2] + args_list
    label = " ".join(args_list)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        stream = process.stdout
        if stream is None:
            print(f"[error] pm2 {label} returned no stdout pipe", file=sys.stderr)
            sys.exit(1)
        output = bytearray()
        timeout_seconds = pm2_timeout_seconds()
        deadline = time.monotonic() + timeout_seconds
        with selectors.DefaultSelector() as selector:
            # POSIX-only. DefaultSelector selects over the subprocess stdout pipe
            # via epoll/kqueue; on Windows it falls back to select(), which does
            # not accept pipes and raises OSError at the first selection. bin/ PM2
            # tooling is documented POSIX-only (see docs/adapters/pm2.md); this is
            # the same pattern as bin/deploy_runtime.py. The library makes the
            # platform branch explicit where it has one (ledger/store.py and
            # daemon/observe.py fail closed on unsupported platforms); bin/ states
            # it in the doc instead.
            selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print(f"[error] pm2 {label} timed out after {timeout_seconds}s", file=sys.stderr)
                    sys.exit(124)
                if not selector.select(remaining):
                    print(f"[error] pm2 {label} timed out after {timeout_seconds}s", file=sys.stderr)
                    sys.exit(124)
                chunk = os.read(stream.fileno(), min(64 * 1024, max_output_bytes + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_output_bytes:
                    print(
                        f"[error] pm2 {label} output exceeds {max_output_bytes} bytes; "
                        f"refusing to parse it",
                        file=sys.stderr,
                    )
                    sys.exit(1)
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # Post-EOF wait: PM2 closed stdout after emitting output then hung before
        # exiting, so EOF broke the read loop and process.wait() raised. Route it
        # through the same timeout diagnostic + exit 124 as the read-loop deadline,
        # restoring parity with deploy_runtime.py's _pm2_run_bounded (which catches
        # subprocess.SubprocessError, of which TimeoutExpired is a subclass). The
        # bare `except OSError` here dropped this branch, so the wait-timeout was a
        # traceback + exit 1 instead of the established timeout diagnostic.
        print(f"[error] pm2 {label} timed out after {timeout_seconds}s", file=sys.stderr)
        sys.exit(124)
    except OSError as error:
        print(f"[error] pm2 {label} failed: {error}", file=sys.stderr)
        sys.exit(1)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if process is not None and process.stdout is not None:
            process.stdout.close()
    return subprocess.CompletedProcess(command, returncode, bytes(output).decode(errors="replace"), "")


def pm2_run(
    args_list: list[str], *, capture_output: bool = False, max_output_bytes: int | None = None
) -> subprocess.CompletedProcess:
    pm2 = resolve_pm2()
    if not pm2:
        print("[error] pm2 not found. Install: npm install -g pm2", file=sys.stderr)
        sys.exit(1)
    if max_output_bytes is not None:
        return _pm2_run_bounded(pm2, args_list, max_output_bytes)
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


def codex_sidecar_is_ready(
    *, timeout_seconds: float = SIDECAR_READINESS_PROBE_TIMEOUT_SECONDS
) -> bool:
    curl = shutil.which("curl")
    if not curl:
        print("[error] curl not found; cannot probe Codex app-server readiness", file=sys.stderr)
        sys.exit(1)
    port = os.environ.get("LLM_COLLAB_CODEX_APP_SERVER_PORT", "8767")
    try:
        result = subprocess.run(
            [
                curl,
                "--silent",
                "--output",
                os.devnull,
                "--write-out",
                "%{http_code}",
                f"http://127.0.0.1:{port}/readyz",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == "200"


def wait_for_codex_sidecar_readiness() -> None:
    deadline = time.monotonic() + SIDECAR_READINESS_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if codex_sidecar_is_ready(
            timeout_seconds=min(SIDECAR_READINESS_PROBE_TIMEOUT_SECONDS, remaining)
        ):
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


def verify_codex_sidecar_runtime_home(expected_runtime_home: str) -> None:
    port = os.environ.get("LLM_COLLAB_CODEX_APP_SERVER_PORT", "8767")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "bin" / "codex_app_server_identity_probe.py"),
                "--endpoint",
                f"ws://127.0.0.1:{port}",
                "--expected-runtime-home",
                expected_runtime_home,
                "--token-file",
                str(sidecar_token_file()),
                "--timeout-seconds",
                str(SIDECAR_IDENTITY_TIMEOUT_SECONDS),
            ],
            capture_output=True,
            text=True,
            timeout=SIDECAR_IDENTITY_PROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[error] Codex app-server identity probe failed: {exc}", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0:
        print(
            f"[error] Codex app-server identity probe failed: "
            f"{result.stderr or result.stdout}",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


class _Pm2ReadBudget:
    """Cumulative byte budget across every `pm2 jlist` read in one command run.

    Mirrors the shape of bin/_bounded_io.py's ReadBudget -- limit/spent/charge,
    refuse on exceed -- because the repository already owns that accumulator style
    for untrusted reads; this is the same style applied to a subprocess read, not a
    second one. Per-call bounds (PM2_JLIST_MAX_BYTES passed to each jlist) satisfy
    'raise, never truncate' for a single read but not 'cumulative across one run'
    (AGENTS.md "Bounded work fails closed"): `start --all` and `restart --all` read
    jlist once per target -- the process table MUTATES between reads, so the
    read-only one-snapshot fix that made `status --all` cumulative by construction
    (#683) does not transfer. One of these is created per command in main() and
    threaded through _read_jlist_snapshot / watcher_status so every jlist read in
    the run charges ONE total, aborting (sys.exit) when it is exceeded. Aborts
    never truncate: a truncated jlist that still parses reports a real watcher as
    absent, the fail-open GH-678 closed.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0

    def charge(self, count: int, label: str) -> None:
        self.spent += count
        if self.spent > self.limit:
            print(
                f"[error] cumulative pm2 {label} output exceeds the run budget: "
                f"{self.spent} > {self.limit} bytes across reads; aborting",
                file=sys.stderr,
            )
            sys.exit(1)


def _read_jlist_snapshot(*, budget=None) -> tuple[subprocess.CompletedProcess, object]:
    """Read and parse `pm2 jlist` once with the size bound; the run's jlist budget.

    `pm2 jlist` is the same process table for every target in a batch, so reading
    it once per target multiplies untrusted parse work by N. Per-call bounding is
    not cumulative across one run (AGENTS.md "Bounded work fails closed"); one
    bounded read is, by construction -- one source, one bound, one parse. The bound
    refuses to parse an oversized table rather than truncating it, because a
    truncated jlist that still parses reports a real watcher as absent -- the
    fail-open GH-678 closed. Malformed jlist fails closed (entries=None) rather
    than fall back to matching rendered text.

    When a run-level budget is threaded in (start/restart/ensure --all), the bytes
    of every read are charged against that one cumulative total before the parse
    boundary (json.loads) -- so N per-target reads in one command share a single
    bound instead of N per-call bounds (GH-684). Charging happens regardless of
    whether the bytes later parse: the untrusted read already happened.
    """
    result = pm2_run(["jlist"], capture_output=True, max_output_bytes=PM2_JLIST_MAX_BYTES)
    if budget is not None:
        budget.charge(len((result.stdout or "").encode("utf-8", errors="replace")), "jlist")
    try:
        entries = json.loads(result.stdout or "[]")
    except ValueError:
        return result, None
    return result, entries


def _status_from_snapshot(
    result: subprocess.CompletedProcess, entries: object, agent_id: str
) -> tuple[subprocess.CompletedProcess, str | None]:
    """Structured pm2_env.status for one agent out of a shared jlist snapshot."""
    name = app_name(agent_id)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == name:
                env = entry.get("pm2_env")
                if isinstance(env, dict):
                    return result, env.get("status")
                return result, None
    return result, None


def watcher_status(
    agent_id: str, *, snapshot=None, budget=None
) -> tuple[subprocess.CompletedProcess, str | None]:
    """Read the watcher's PM2 status as a structured value from `pm2 jlist`.

    Returns (result, status) where status is the matched entry's pm2_env.status
    ("online", "stopped", "errored", ...) or None when PM2 does not list the app.

    This is the one liveness authority for start, ensure, restart and status. It
    reads a JSON field rather than scanning rendered text because a `pm2 describe`
    table echoes operator-chosen strings -- the app name (agent id), script path,
    script args (--me/--project/--repo-target), both log paths and exec cwd
    (runtime home) -- and any of them can contain "status" or "online". Every
    text-matching form tried here (a substring, then a row/field regex) was
    satisfiable by those fields while the real status was errored, so a dead
    watcher read as live; the regex's colon alternative even matched the literal
    "status: online" appearing in any field. `pm2 jlist` returns status as a
    structured field, so liveness is a value comparison (`status == "online"`),
    not a token scan -- which removes the defect class instead of tightening the
    match a third time.
    """
    # A status batch passes one snapshot so `status --all` reads jlist ONCE under
    # a single cumulative bound for the whole run rather than N bounded reads of
    # the same table (start/ensure/restart mutate between reads and stay fresh).
    # Those mutating commands read fresh per target and charge each read against
    # the run-level budget threaded from main() (GH-684): still per-call bounded,
    # but N reads in one command now share one cumulative total.
    if snapshot is None:
        snapshot = _read_jlist_snapshot(budget=budget)
    result, entries = snapshot
    return _status_from_snapshot(result, entries, agent_id)


def start_agent(agent_id: str, *, budget=None) -> None:
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

    result, status = watcher_status(agent_id, budget=budget)
    if result.returncode != 0 or status != "online":
        print(f"[error] pm2 started {name} but it is not online", file=sys.stderr)
        sys.exit(result.returncode or 1)


def ensure_agent(agent_id: str, *, runtime_home: str | None = None, budget=None) -> None:
    _, status = watcher_status(agent_id, budget=budget)
    if status == "online":
        print(f"[watcher] {agent_id} already running.")
    else:
        start_agent(agent_id, budget=budget)
    if is_sidecar(agent_id):
        wait_for_codex_sidecar_readiness()
        if runtime_home is not None:
            verify_codex_sidecar_runtime_home(runtime_home)


def process_status_exit_code(agent_id: str, *, snapshot=None) -> int:
    result, status = watcher_status(agent_id, snapshot=snapshot)
    if result.returncode != 0:
        if result.stderr:
            print(
                result.stderr,
                end="" if result.stderr.endswith("\n") else "\n",
                file=sys.stderr,
            )
        return result.returncode
    name = app_name(agent_id)
    print(f"{name}: {status if status is not None else 'not found'}")
    return 0 if status == "online" else 1


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

    # Sidecar discovery has two halves ordered around the jlist snapshot. The
    # token-gated half (enabled_sidecar_ids) needs no PM2 read, so for status a
    # sidecar whose token is present is reported BEFORE the snapshot -- a PM2
    # failure must not suppress its [sidecar] line (collaborator [ax] lines are
    # already safe above). The registration half (a process left running after its
    # token was removed, reachable by cleanup/inspection but never invented on a
    # clean install) needs PM2 data, which for status comes from the one bounded
    # jlist snapshot read below -- so no unbounded `pm2 describe` precedes the
    # budget (AGENTS.md: begin at the earliest parse boundary AND stay cumulative).
    # start/restart --all still discover via describe (GH-684).
    token_sidecars = set(enabled_sidecar_ids()) if defer_sidecars else set()
    if args.command == "status":
        for name in token_sidecars:
            print(f"[sidecar] target={name} (no AX surface)")

    # One cumulative budget for every `pm2 jlist` read in this run (GH-684).
    # status reads jlist once (the #683 snapshot) so it charges once; start and
    # restart read fresh per target because the process table mutates between
    # reads, and those N reads now share this one total instead of N per-call
    # bounds. Aborts never truncate: a parsed-but-truncated jlist reports a real
    # watcher as absent (fail-open, GH-678).
    run_budget = _Pm2ReadBudget(PM2_JLIST_MAX_BYTES)
    status_snapshot = _read_jlist_snapshot(budget=run_budget) if args.command == "status" else None

    if defer_sidecars:
        # safe now: every collaborator AX line is already on stdout
        sidecars = sidecar_ids_for_command(args.command, snapshot=status_snapshot)
        targets.extend(sidecars)
        if args.command == "status":
            for name in sidecars:
                if name not in token_sidecars:
                    print(f"[sidecar] target={name} (no AX surface)")

    status_exit_code = 0
    for agent_id in targets:
        name = app_name(agent_id)
        if args.command == "start":
            start_agent(agent_id, budget=run_budget)
        elif args.command == "restart":
            # Re-read the deployed ecosystem so the running process matches the
            # definition that the current-runtime gate approved. startOrRestart
            # exits 0 while the process lands errored or stopped, so propagate its
            # exit code and verify online afterwards -- exactly what start does.
            # The return value used to be discarded, so start_watcher reported ok
            # for a watcher that was not running (GH-678).
            restarted = pm2_run(["startOrRestart", str(ecosystem_path()), "--only", name])
            if restarted.returncode != 0:
                print(f"[error] pm2 failed to restart {name} (exit {restarted.returncode})", file=sys.stderr)
                sys.exit(restarted.returncode)
            result, status = watcher_status(agent_id, budget=run_budget)
            if result.returncode != 0 or status != "online":
                print(f"[error] pm2 restarted {name} but it is not online", file=sys.stderr)
                sys.exit(result.returncode or 1)
        elif args.command == "ensure":
            ensure_agent(agent_id, runtime_home=args.runtime_home, budget=run_budget)
        elif args.command == "stop":
            pm2_run(["stop", name])
        elif args.command == "delete":
            pm2_run(["delete", name])
        elif args.command == "status":
            result = process_status_exit_code(agent_id, snapshot=status_snapshot)
            if result != 0 and status_exit_code == 0:
                status_exit_code = result
        elif args.command == "logs":
            pm2_run(["logs", name, "--lines", str(args.lines), "--nostream"])

    if status_exit_code != 0:
        sys.exit(status_exit_code)


if __name__ == "__main__":
    main()
