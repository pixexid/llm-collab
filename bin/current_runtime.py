#!/usr/bin/env python3
"""Validate this checkout against origin/main before starting bootstrap."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_MARKER = re.compile(r"CONTRACT_VERSION:\s*(\S+)")

# GH-503: a mutation entrypoint that reasons/acts from a stale tree is our #1
# recurring reliability failure. This exit code is distinct from other refusals
# (e.g. inbox route/activation exit 75) so callers can tell a stale-runtime
# refusal apart from a routing/authorization refusal.
RUNTIME_GATE_REFUSED = 78

# Recovery-ONLY escape hatch, deliberately DISTINCT from session_bootstrap's
# --allow-stale-tooling (which is a bootstrap/diagnostic waiver). This one is set
# to the EXACT command name being run, so it can never blanket-authorize other
# mutations by inference, and its use is always announced loudly. Never a default.
RECOVERY_WAIVER_ENV = "LLM_COLLAB_ALLOW_STALE_RUNTIME_RECOVERY"

# Test-ONLY bypass, distinct from the production recovery waiver above and from
# session_bootstrap's --allow-stale-tooling. The suite runs from a feature-branch
# worktree (HEAD != origin/main), which the gate would legitimately refuse; the
# harness sets this so CLI/subprocess tests of unrelated behavior are not gated.
# It must never be set in a real deployment; it is not a recovery path.
TEST_BYPASS_ENV = "LLM_COLLAB_RUNTIME_GATE_TEST_BYPASS"

# Best-effort label so a refusal names WHICH tree is stale (deployed vs source).
_DEPLOYED_RUNTIME = Path.home() / ".local" / "share" / "llm-collab" / "runtime" / "main"


class ToolingError(RuntimeError):
    pass


def git(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ToolingError(f"git {' '.join(args)} failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ToolingError(f"git {' '.join(args)} failed: {detail}")
    return result


def contract_version(text: str) -> str:
    match = CONTRACT_MARKER.search(text[:200])
    if not match:
        raise ToolingError("AGENTS.md has no CONTRACT_VERSION marker")
    return match.group(1)


def current_tooling() -> dict[str, str]:
    if not (ROOT / ".git").exists():
        raise ToolingError(f"{ROOT} is not a git checkout")

    git("fetch", "origin", "main", "--quiet")
    origin_main = git("rev-parse", "origin/main").stdout.strip()
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != origin_main:
        raise ToolingError(
            "runtime must be exact origin/main; "
            f"origin/main={origin_main} HEAD={head}"
        )
    tracked_changes = git(
        "status", "--porcelain=v1", "--untracked-files=no"
    ).stdout.strip()
    if tracked_changes:
        raise ToolingError("runtime has tracked changes; refusing bootstrap")

    local_contract = contract_version((ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    origin_contract = contract_version(
        git("show", "origin/main:AGENTS.md").stdout
    )
    if local_contract != origin_contract:
        raise ToolingError(
            f"contract mismatch: checkout={local_contract} origin/main={origin_contract}"
        )
    return {
        "head": head,
        "origin_main": origin_main,
        "contract_version": local_contract,
    }


def _tree_label(root: Path) -> str:
    try:
        if root.resolve() == _DEPLOYED_RUNTIME.resolve():
            return "deployed runtime"
    except OSError:
        pass
    return f"source checkout {root}"


def require_current_runtime(command: str, *, environ=None, exit_on_refusal: bool = True):
    """Gate a mutation-capable entrypoint on exact-current origin/main.

    Validates the tree this module executes from (the code that will run the
    mutation) via current_tooling(): fetch origin/main, HEAD==origin/main, no
    tracked dirt, matching AGENTS contract. On success returns the evidence.

    On any staleness/dirt/fetch failure it FAILS CLOSED with an unmistakable
    refusal that names the stale tree, HEAD, and the command. The only bypass is
    the recovery waiver env set to the EXACT command name — announced loudly, and
    scoped to that one command so it can never silently authorize other mutations.

    Fetch failure is a refusal, not a silent pass. Read-only diagnostics must not
    call this; it is for delivery, session-mutation/registration, and watcher
    startup.
    """
    env = os.environ if environ is None else environ
    if env.get(TEST_BYPASS_ENV):
        return {"test_bypass": command}
    try:
        evidence = current_tooling()
        return evidence
    except (OSError, ToolingError) as error:
        label = _tree_label(ROOT)
        if env.get(RECOVERY_WAIVER_ENV) == command:
            print(
                f"[runtime-gate] RECOVERY OVERRIDE for '{command}': {label} is STALE "
                f"({error}). Proceeding ONLY because {RECOVERY_WAIVER_ENV}={command} was "
                f"set for recovery. This bypasses the freshness guard — normal delivery, "
                f"registration, and watcher startup MUST NOT set this.",
                file=sys.stderr,
                flush=True,
            )
            return {"waived": command, "reason": str(error)}
        print(
            f"[runtime-gate] REFUSED '{command}': {label} is not exact-current origin/main. "
            f"{error}\n"
            f"  Fix: bring the runtime to origin/main with a clean tree, then retry.\n"
            f"  Recovery ONLY: set {RECOVERY_WAIVER_ENV}={command} to bypass this one "
            f"command deliberately (loud, operator-visible, never a default).",
            file=sys.stderr,
            flush=True,
        )
        if exit_on_refusal:
            raise SystemExit(RUNTIME_GATE_REFUSED)
        raise


def parse_args() -> tuple[bool, list[str]]:
    parser = argparse.ArgumentParser(
        description="Validate current llm-collab tooling before session bootstrap."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and report current tooling without starting bootstrap.",
    )
    args, bootstrap_args = parser.parse_known_args()
    if not args.check and not bootstrap_args:
        parser.error("pass session_bootstrap.py arguments, or use --check")
    if args.check and bootstrap_args:
        parser.error("--check cannot be combined with bootstrap arguments")
    return args.check, bootstrap_args


def main() -> int:
    check_only, bootstrap_args = parse_args()
    try:
        evidence = current_tooling()
    except (OSError, ToolingError) as error:
        print(f"[tooling] REFUSED: {error}", file=sys.stderr)
        return 1

    print(
        f"[tooling] current: contract v{evidence['contract_version']} "
        f"HEAD {evidence['head']} origin/main {evidence['origin_main']}",
        file=sys.stdout if check_only else sys.stderr,
        flush=True,
    )
    if check_only:
        return 0

    bootstrap = ROOT / "bin" / "session_bootstrap.py"
    return subprocess.run(
        [sys.executable, str(bootstrap), *bootstrap_args], cwd=ROOT
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
