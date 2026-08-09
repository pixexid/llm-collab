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
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(SCRIPT_DIR), str(SCRIPT_DIR.parent)]

import session_bootstrap  # noqa: E402
from _watcher_liveness import FRESH, check_markers, foreign_fresh, handoff_file  # noqa: E402
from llm_collab.bb_client import PINNED_BB_VERSION, subprocess_transport  # noqa: E402

ORCHESTRATOR_DOC = "docs/workflows/orchestrator-sessions.md"
HOOK_PROJECT_ID = "llm-collab"  # this hook is llm-collab's own repo hook

BB_PROBE_TIMEOUT_SECONDS = 10.0
# The version envelope is ~100 bytes; 64 KiB is generous. The bound lives at the
# earliest untrusted read — the subprocess streams — via the repository's
# bounded transport, which kills the child and raises on overflow instead of
# accumulating unbounded output in a SessionStart hook.
BB_PROBE_MAX_RESPONSE_CHARS = 64 * 1024
# A Claude Code hook payload is a small JSON object on stdin; bound the read.
MAX_HOOK_PAYLOAD_BYTES = 64 * 1024

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

_MARK = {PASS: "✓", FAIL: "✗", UNKNOWN: "?"}


def _line(check: str, status: str, detail: str) -> None:
    print(f"[session-gate] {_MARK[status]} {check}: {status} — {detail}")


def bb_version_check() -> tuple[str, str]:
    """Installed bb vs PINNED_BB_VERSION. A broken probe is UNKNOWN, not a pass.

    Reads through the shared bounded transport (llm_collab.bb_client) rather
    than a second bounding implementation: both streams are capped while read,
    and overflow/timeout/launch failures raise typed errors, which this hook
    surfaces as UNKNOWN — never a pass, never a crash.
    """
    try:
        transport = subprocess_transport(
            ["bb"], max_response_chars=BB_PROBE_MAX_RESPONSE_CHARS
        )
        result = transport(["settings", "version", "--json"], BB_PROBE_TIMEOUT_SECONDS)
    except Exception as error:
        return UNKNOWN, f"bb version probe could not run: {type(error).__name__}: {error}"
    if result.exit_code != 0:
        return UNKNOWN, f"bb settings version exited {result.exit_code}"
    try:
        current = json.loads(result.stdout)["currentVersion"]
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


def current_session_id(stdin) -> str | None:
    """This session's identity, from the Claude Code hook payload on stdin.

    A SessionStart hook receives `{"session_id": ..., "hook_event_name": ...}`
    on stdin; that is the hook's only honest source of the current session's
    id. Run by hand (a TTY, or empty/piped garbage), there is no payload and
    no identity — None, and the caller says so rather than guessing (I5).
    """
    try:
        if stdin.isatty():
            return None
        raw = stdin.read(MAX_HOOK_PAYLOAD_BYTES + 1)
    except (OSError, ValueError):
        return None
    if not raw or len(raw) > MAX_HOOK_PAYLOAD_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def watcher_checks(own_session_id: str | None) -> list[tuple[str, str, str]]:
    """Watcher liveness markers; stale/absent/unreadable is FAIL, never a pass.

    A FRESH marker owned by a DIFFERENT session is the predecessor-watchers
    incident: the previous session's watchers were never stopped and are
    double-notifying this one. Loud, and it names the foreign session id.
    """
    results = []
    report = check_markers(HOOK_PROJECT_ID)
    foreign = {entry["name"] for entry in foreign_fresh(report, own_session_id)}
    for entry in report:
        check = f"watcher {entry['name']} [{HOOK_PROJECT_ID}]"
        if entry["status"] == FRESH and entry["name"] in foreign:
            results.append((
                check,
                FAIL,
                f"fresh marker owned by FOREIGN session {entry['session_id']} — "
                "a predecessor session's watcher is still running and "
                "double-notifying this one; message that session to TaskStop "
                "its watchers, or escalate to the operator",
            ))
        elif entry["status"] == FRESH and own_session_id is None:
            results.append((
                check,
                UNKNOWN,
                f"fresh marker owned by session {entry.get('session_id')}; "
                "current session identity unavailable, ownership unverified",
            ))
        elif entry["status"] == FRESH:
            results.append((
                check,
                PASS,
                f"marker {entry['age_seconds']}s old, owned by this session",
            ))
        elif entry["status"] == "unreadable":
            results.append((check, UNKNOWN, f"marker unreadable: {entry.get('detail', '?')}"))
        else:
            results.append((check, FAIL, f"marker {entry['status']} ({entry['marker']})"))
    return results


def run_checks(own_session_id: str | None = None) -> bool:
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

    for check, status, detail in watcher_checks(own_session_id):
        _line(check, status, detail)
        clean = clean and status == PASS
    if own_session_id is None:
        _line(
            "watcher ownership",
            UNKNOWN,
            "current session id unavailable (no hook payload on stdin); "
            "foreign-watcher check could not run",
        )
        clean = False

    return clean


def handoff_line() -> str:
    """The handoff pointer — the project-scoped runtime path, and whether it exists.

    An absent handoff at session start is worth knowing immediately and is not
    a hook failure, so absence is stated on the line rather than implied by a
    path the reader must stat themselves. An unresolvable state root says so
    instead of printing nothing.
    """
    try:
        path = handoff_file(HOOK_PROJECT_ID)
        exists = path.exists()
    except (Exception, SystemExit) as error:
        return f"handoff:  <path unresolvable: {type(error).__name__}: {error}>"
    if exists:
        return f"handoff:  {path}"
    return f"handoff:  {path} (ABSENT — no handoff written yet)"


def main() -> int:
    print("[session-gate] session-setup checks (results and pointers only):")
    # I5: coverage must be observable. A reader in ANY checkout must be able to
    # tell which project's markers this hook checked without reading the source.
    print(f"[session-gate] project: {HOOK_PROJECT_ID} — watcher markers checked for this project")
    own_session_id = current_session_id(sys.stdin)
    try:
        clean = run_checks(own_session_id)
    except BaseException as error:  # a broken gate must be loud, never a pass
        # BaseException on purpose: this hook must never fail the session, and a
        # SystemExit leaking out of a probe is still a broken probe.
        clean = False
        _line("session-gate itself", UNKNOWN, f"probe broke: {type(error).__name__}: {error}")
        print("[session-gate] the checks above are INCOMPLETE — no setup claim was verified")
    print(f"[session-gate] protocol: {ORCHESTRATOR_DOC}")
    print(f"[session-gate] {handoff_line()}")
    if not clean:
        print("━" * 60)
        print("⚠️  SESSION SETUP INCOMPLETE — see the ✗/? lines above and the pointers")
        print("━" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
