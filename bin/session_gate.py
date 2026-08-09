#!/usr/bin/env python3
"""session_gate.py — SessionStart hook: mechanical session-setup checks.

This hook CHECKS and POINTS; it never restates a rule. A rule copied into this
output is a cached copy that goes stale silently — the stale-copy trap relocated
into the hook. Check results and doc pointers only.

It exists separately from session_bootstrap.py rather than extending it:
bootstrap is an agent-identity command (requires a registered --agent, starts
watchers, exits 1 on stale tooling), while a SessionStart hook must be
identity-free and must never fail the session. What the hook needs from
bootstrap — the origin/main currency probe and the contract-version read — it
imports and reuses rather than duplicating.

Never fails the session: main() always returns 0, and a probe that itself
breaks reports UNKNOWN — visibly distinct from PASS, never rendered as one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(SCRIPT_DIR), str(SCRIPT_DIR.parent)]

import session_bootstrap  # noqa: E402
from _watcher_liveness import FRESH, check_markers  # noqa: E402
from llm_collab.bb_client import PINNED_BB_VERSION  # noqa: E402

ORCHESTRATOR_DOC = "docs/workflows/orchestrator-sessions.md"
HANDOFF_FILE = "scratchpad/orchestrator-handoff.md"

BB_PROBE_TIMEOUT_SECONDS = 10.0

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

_MARK = {PASS: "✓", FAIL: "✗", UNKNOWN: "?"}


def _line(check: str, status: str, detail: str) -> None:
    print(f"[session-gate] {_MARK[status]} {check}: {status} — {detail}")


def bb_version_check() -> tuple[str, str]:
    """Installed bb vs PINNED_BB_VERSION. A broken probe is UNKNOWN, not a pass."""
    try:
        done = subprocess.run(
            ["bb", "settings", "version", "--json"],
            capture_output=True,
            text=True,
            timeout=BB_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return UNKNOWN, f"bb version probe could not run: {error}"
    if done.returncode != 0:
        return UNKNOWN, f"bb settings version exited {done.returncode}"
    try:
        current = json.loads(done.stdout)["currentVersion"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return UNKNOWN, "bb version envelope unreadable"
    if not isinstance(current, str):
        return UNKNOWN, "bb version envelope has no string currentVersion"
    if current == PINNED_BB_VERSION:
        return PASS, f"bb {current} == pinned {PINNED_BB_VERSION}"
    return FAIL, f"bb {current} != pinned {PINNED_BB_VERSION} — see {ORCHESTRATOR_DOC}"


def tooling_check() -> tuple[str, str]:
    """Deployed runtime vs origin/main — reused from session_bootstrap, not duplicated."""
    currency = session_bootstrap.tooling_currency()
    state = currency["state"]
    if state == session_bootstrap.TOOLING_CURRENT:
        fetched = "" if currency.get("fetched") else " (origin unreachable; last fetched ref)"
        return PASS, f"checkout {currency.get('head', '?')} has origin/main{fetched}"
    if state == session_bootstrap.TOOLING_STALE:
        return FAIL, (
            f"checkout {currency.get('head', '?')} is missing merged work "
            f"(origin/main {currency.get('origin_main', '?')})"
        )
    return UNKNOWN, f"currency unverifiable: {currency.get('reason', '?')}"


def watcher_checks() -> list[tuple[str, str, str]]:
    """Watcher liveness markers; stale/absent/unreadable is FAIL, never a pass."""
    results = []
    for entry in check_markers():
        if entry["status"] == FRESH:
            results.append((f"watcher {entry['name']}", PASS, f"marker {entry['age_seconds']}s old"))
        elif entry["status"] == "unreadable":
            results.append((f"watcher {entry['name']}", UNKNOWN, f"marker unreadable: {entry.get('detail', '?')}"))
        else:
            results.append((f"watcher {entry['name']}", FAIL, f"marker {entry['status']} ({entry['marker']})"))
    return results


def run_checks() -> bool:
    """Print every check line. Returns True when nothing failed or is unknown."""
    clean = True

    status, detail = bb_version_check()
    _line("bb version", status, detail)
    clean = clean and status == PASS

    status, detail = tooling_check()
    _line("tooling currency", status, detail)
    clean = clean and status == PASS

    version = session_bootstrap.contract_version()
    if version == "unknown":
        _line("contract", UNKNOWN, "AGENTS.md unreadable — contract version unverified")
        clean = False
    else:
        _line("contract", PASS, f"AGENTS.md version {version}")

    for check, status, detail in watcher_checks():
        _line(check, status, detail)
        clean = clean and status == PASS

    return clean


def main() -> int:
    print("[session-gate] session-setup checks (results and pointers only):")
    try:
        clean = run_checks()
    except BaseException as error:  # a broken gate must be loud, never a pass
        # BaseException on purpose: this hook must never fail the session, and a
        # SystemExit leaking out of a probe is still a broken probe.
        clean = False
        _line("session-gate itself", UNKNOWN, f"probe broke: {type(error).__name__}: {error}")
        print("[session-gate] the checks above are INCOMPLETE — no setup claim was verified")
    print(f"[session-gate] protocol: {ORCHESTRATOR_DOC}")
    print(f"[session-gate] handoff:  {HANDOFF_FILE}")
    if not clean:
        print("━" * 60)
        print("⚠️  SESSION SETUP INCOMPLETE — see the ✗/? lines above and the pointers")
        print("━" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
