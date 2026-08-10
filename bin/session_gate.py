#!/usr/bin/env python3
"""session_gate.py — SessionStart hook: mechanical session-setup checks.

This hook CHECKS and POINTS; it never restates a rule. A rule copied into this
output is a cached copy that goes stale silently — the stale-copy trap relocated
into the hook. Check results and doc pointers only.

It exists separately from session_bootstrap.py rather than extending it:
bootstrap is an agent-identity command (requires a registered --agent, starts
watchers, exits 1 on stale tooling), while a SessionStart hook has no agent
identity and must never fail the session. What the hook needs from
bootstrap — the origin/main currency probe and the contract-version read — it
imports and reuses rather than duplicating.

The invoking checkout pointer supplies the project identity explicitly; an
absent or unregistered identity skips the checks rather than guessing.

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
from _helpers import PROJECTS_FILE, get_project, projects_registry_missing  # noqa: E402
from _watcher_liveness import check_markers, evaluate_coverage, handoff_file  # noqa: E402
from llm_collab.bb_client import (  # noqa: E402
    PINNED_BB_VERSION,
    bb_executable_from_project,
    subprocess_transport,
)
from llm_collab.ledger.paths import validate_project_id  # noqa: E402

ORCHESTRATOR_DOC = "docs/workflows/orchestrator-sessions.md"

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


def resolve_project_id(argv: list[str] | None = None) -> tuple[str | None, str | None]:
    """Return the explicit registered project, or the reason to skip checks."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "--project" or not args[1]:
        return None, "absent"
    project_id = args[1]
    try:
        validate_project_id(project_id)
    except ValueError:
        return project_id, "invalid"
    try:
        registered = get_project(project_id)
    # The bounded registry reader fails closed with SystemExit. Preserve that
    # refusal as UNKNOWN; ordinary unexpected resolution errors are the same
    # incomplete state, while KeyboardInterrupt is intentionally not caught.
    except FileNotFoundError:
        return project_id, "registry_not_found"
    except SystemExit:
        return project_id, "registry_unresolvable"
    except Exception:
        return project_id, "registry_unresolvable"
    if registered is None:
        try:
            registry_missing = projects_registry_missing()
        except Exception:
            return project_id, "registry_unresolvable"
        if registry_missing:
            return project_id, "registry_not_found"
        return project_id, "unregistered"
    return project_id, None


def bb_version_check(project_id: str) -> tuple[str, str]:
    """Installed bb vs PINNED_BB_VERSION. A broken probe is UNKNOWN, not a pass.

    Reads through the shared bounded transport (llm_collab.bb_client) rather
    than a second bounding implementation: both streams are capped while read,
    and overflow/timeout/launch failures raise typed errors, which this hook
    surfaces as UNKNOWN — never a pass, never a crash.
    """
    try:
        # GH-728: probe through the project's configured executable via the one
        # resolver seam — never a bare PATH bb, which can be a different
        # installation than the one spawns use. An unconfigured project is
        # UNKNOWN (a setup fact), never a pass and never a session failure.
        executable = bb_executable_from_project(get_project(project_id))
        transport = subprocess_transport(
            executable, max_response_chars=BB_PROBE_MAX_RESPONSE_CHARS
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


def watcher_checks(project_id: str, own_session_id: str | None) -> list[tuple[str, str, str]]:
    """Render the shared coverage verdict (evaluate_coverage); never re-derive it.

    A FRESH marker owned by a DIFFERENT session is the predecessor-watchers
    incident: the previous session's watchers were never stopped and are
    double-notifying this one. Loud, and it names the foreign session id.
    """
    results = []
    verdicts = evaluate_coverage(check_markers(project_id), own_session_id)
    for verdict in verdicts:
        check = f"watcher {verdict['name']} [{project_id}]"
        reason = verdict["reason"]
        if reason == "covered":
            results.append((
                check,
                PASS,
                f"marker {verdict['age_seconds']}s old, owned by this session",
            ))
        elif reason == "foreign":
            results.append((
                check,
                FAIL,
                f"fresh marker owned by FOREIGN session {verdict['session_id']} — "
                "a predecessor session's watcher is still running and "
                "double-notifying this one; message that session to TaskStop "
                "its watchers, or escalate to the operator",
            ))
        elif reason == "owner_unknown":
            results.append((
                check,
                UNKNOWN,
                f"fresh marker owned by session {verdict.get('session_id')}; "
                "current session identity unavailable, ownership unverified",
            ))
        elif reason == "unreadable":
            results.append((check, UNKNOWN, f"marker unreadable: {verdict.get('detail', '?')}"))
        else:  # stale / absent
            results.append((check, FAIL, f"marker {reason} ({verdict['marker']})"))
    return results


def run_checks(project_id: str, own_session_id: str | None = None) -> bool:
    """Print every check line. Returns True when nothing failed or is unknown."""
    clean = True

    status, detail = bb_version_check(project_id)
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

    for check, status, detail in watcher_checks(project_id, own_session_id):
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


def handoff_line(project_id: str) -> str:
    """The handoff pointer — the project-scoped runtime path, and whether it exists.

    An absent handoff at session start is worth knowing immediately and is not
    a hook failure, so absence is stated on the line rather than implied by a
    path the reader must stat themselves. An unresolvable state root says so
    instead of printing nothing.
    """
    try:
        path = handoff_file(project_id)
        exists = path.exists()
    except (Exception, SystemExit) as error:
        return f"handoff:  <path unresolvable: {type(error).__name__}: {error}>"
    if exists:
        return f"handoff:  {path}"
    return f"handoff:  {path} (ABSENT — no handoff written yet)"


def main(argv: list[str] | None = None) -> int:
    project_id, skip_reason = resolve_project_id(argv)
    if skip_reason == "absent":
        print(
            "[session-gate] checks skipped: project identity absent "
            "(invoke with --project <project_id>)"
        )
        return 0
    if skip_reason == "unregistered":
        print(
            "[session-gate] checks skipped: project identity unregistered "
            f"({project_id!r} is not registered in projects.json)"
        )
        return 0
    if skip_reason == "invalid":
        print(
            "[session-gate] project identity: UNKNOWN — invalid project ID "
            f"{project_id!r}; checks not run"
        )
        print("━" * 60)
        print("⚠️  SESSION SETUP INCOMPLETE — see the ✗/? lines above and the pointers")
        print("━" * 60)
        return 0
    if skip_reason == "registry_not_found":
        print(
            "[session-gate] checks skipped: project registry not found "
            f"(no projects.json at {PROJECTS_FILE}; "
            "resolved from this hook's checkout root)"
        )
        return 0
    if skip_reason == "registry_unresolvable":
        print(
            "[session-gate] project registry: UNKNOWN — registry present but "
            "unresolvable; project identity could not be determined"
        )
        print("━" * 60)
        print("⚠️  SESSION SETUP INCOMPLETE — see the ✗/? lines above and the pointers")
        print("━" * 60)
        return 0
    print("[session-gate] session-setup checks (results and pointers only):")
    # I5: coverage must be observable. A reader in ANY checkout must be able to
    # tell which project's markers this hook checked without reading the source.
    print(f"[session-gate] project: {project_id} — watcher markers checked for this project")
    own_session_id = current_session_id(sys.stdin)
    try:
        clean = run_checks(project_id, own_session_id)
    except BaseException as error:  # a broken gate must be loud, never a pass
        # BaseException on purpose: this hook must never fail the session, and a
        # SystemExit leaking out of a probe is still a broken probe.
        clean = False
        _line("session-gate itself", UNKNOWN, f"probe broke: {type(error).__name__}: {error}")
        print("[session-gate] the checks above are INCOMPLETE — no setup claim was verified")
    print(f"[session-gate] protocol: {ORCHESTRATOR_DOC}")
    print(f"[session-gate] {handoff_line(project_id)}")
    if not clean:
        print("━" * 60)
        print("⚠️  SESSION SETUP INCOMPLETE — see the ✗/? lines above and the pointers")
        print("━" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
