#!/usr/bin/env python3
"""
session_bootstrap.py — Initialize an agent session.

Outputs the agent's identity.md FIRST so the LLM immediately knows
who it is, then shows inbox, then starts the watcher if applicable.

Before the first watcher-enabled run in a new workspace, follow
docs/workflows/pm2-log-rotation.md.

Usage:
  python bin/session_bootstrap.py --agent orchestrator
  python bin/session_bootstrap.py --agent worker --limit 5
  python bin/session_bootstrap.py --agent orchestrator --no-watcher
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import os
import subprocess

sys.path.insert(0, str(Path(__file__).parent))
import project_issue_queue as issue_queue
from _ax_trust import format_ax_status, probe_ax_trust
from _activation_lease import runtime_id_from_env
from _helpers import (
    InboxScanLimitExceeded,
    ROOT,
    agent_ids,
    agent_identity_path,
    display_path,
    get_agent,
    get_unread_messages,
    is_human_relay,
    load_projects,
    utc_iso,
    watcher_enabled_agents,
)
from _session_autobridge import (
    CanonicalBindingNativeMismatch,
    UnreadableFile,
    iter_sessions,
    resolve_active_canonical_binding,
    session_is_dispatchable,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args():
    p = argparse.ArgumentParser(description="Bootstrap an agent session.")
    p.add_argument("--agent", required=True, help="Your agent ID")
    p.add_argument(
        "--limit",
        type=_positive_int,
        default=5,
        help="Inbox items to show (default: 5)",
    )
    p.add_argument("--no-watcher", action="store_true", help="Skip starting the inbox watcher")
    p.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON summary")
    p.add_argument(
        "--allow-stale-tooling",
        action="store_true",
        help=(
            "Proceed on a checkout that is missing merged work. The staleness is "
            "still reported; only the refusal is waived."
        ),
    )
    return p.parse_args()


TOOLING_CURRENT = "current"
TOOLING_STALE = "stale"
TOOLING_UNKNOWN = "unknown"


def _git(*args: str, timeout: int = 15) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def tooling_currency() -> dict:
    """Is this checkout missing work that is already on origin/main?

    Staleness is not a style question. A checkout pinned to a branch that predates
    a merged change runs that change's *absence* as if it were the contract: on
    2026-07-28 a checkout eight merges behind accepted `--session` on inbox.py and
    ignored it, so a watcher believed it was session-bound, was not, and lost five
    packets before anyone noticed.

    The test is ancestry, not equality — a lane branch ahead of main is current;
    one that cannot reach origin/main is missing merged work.
    """
    if not (ROOT / ".git").exists():
        return {"state": TOOLING_UNKNOWN, "reason": "not a git checkout"}

    fetched = _git("fetch", "origin", "main", "--quiet", timeout=20)
    fetch_ok = bool(fetched and fetched.returncode == 0)

    base = _git("rev-parse", "origin/main")
    if base is None or base.returncode != 0:
        return {
            "state": TOOLING_UNKNOWN,
            "reason": "no origin/main ref to compare against",
            "fetched": fetch_ok,
        }

    ancestor = _git("merge-base", "--is-ancestor", "origin/main", "HEAD")
    if ancestor is None:
        return {"state": TOOLING_UNKNOWN, "reason": "ancestry check failed", "fetched": fetch_ok}

    # git reserves exit 1 for "not an ancestor" and uses other codes (128 and up)
    # for "the question could not be answered". Folding those together would block
    # bootstrap on a broken repository while claiming it is behind — an answer the
    # command never gave.
    if ancestor.returncode == 0:
        state = TOOLING_CURRENT
    elif ancestor.returncode == 1:
        state = TOOLING_STALE
    else:
        return {
            "state": TOOLING_UNKNOWN,
            "reason": f"ancestry check failed (git exit {ancestor.returncode})",
            "fetched": fetch_ok,
        }

    head = _git("rev-parse", "--short", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    detail = {
        "state": state,
        "head": head.stdout.strip() if head and head.returncode == 0 else "unknown",
        "branch": branch.stdout.strip() if branch and branch.returncode == 0 else "unknown",
        "origin_main": base.stdout.strip()[:7],
        "fetched": fetch_ok,
    }
    if detail["state"] == TOOLING_STALE:
        behind = _git("rev-list", "--count", "HEAD..origin/main")
        if behind and behind.returncode == 0:
            detail["commits_behind"] = int(behind.stdout.strip() or 0)
    return detail


def announce_tooling(currency: dict, *, allowed: bool) -> None:
    state = currency["state"]
    if state == TOOLING_CURRENT:
        if currency.get("fetched"):
            print(f"[tooling] checkout {currency['head']} has origin/main — current")
        else:
            # A pass computed against a cached ref is not the same assurance as one
            # computed against the remote, and printing them identically is how a
            # checkout proceeds unknowingly behind main again.
            print(
                f"[tooling] checkout {currency['head']} has the last fetched "
                f"origin/main ({currency['origin_main']}) — current as of that ref"
            )
            print("[tooling] origin was unreachable, so remote main may have moved since")
        return
    if state == TOOLING_UNKNOWN:
        print(f"[tooling] currency UNKNOWN: {currency.get('reason')}")
        print("[tooling] treat inbox, watcher, task, queue and delivery results as unverified")
        return

    behind = currency.get("commits_behind")
    behind_text = f", {behind} commit(s) behind" if behind else ""
    if not currency.get("fetched"):
        behind_text += " (origin unreachable; compared against the last fetched ref)"
    print("━" * 60)
    print("⚠️  STALE TOOLING — this checkout is missing merged work")
    print("━" * 60)
    print(f"  branch      {currency['branch']} @ {currency['head']}{behind_text}")
    print(f"  origin/main {currency['origin_main']}")
    print()
    print("  Inbox, watcher, task, queue and delivery commands run from here")
    print("  execute an older contract. A flag this checkout does not implement")
    print("  is accepted and ignored rather than refused, so the failure looks")
    print("  like working software. See docs/workflows/session-startup.md")
    print("  → 'Keep The Tooling Current'.")
    print()
    if allowed:
        print("  Proceeding: --allow-stale-tooling was passed. Every result below")
        print("  is bound to the older tooling, including anything you report.")
        print("━" * 60)
        return
    print("  Fix the checkout, or run tooling from one that is current, or pass")
    print("  --allow-stale-tooling to proceed deliberately.")
    print("━" * 60)


def start_watcher(agent_id: str) -> dict:
    watcher_script = ROOT / "bin" / "pm2_watchers.py"
    if not watcher_script.exists():
        return {"status": "skipped", "reason": "pm2_watchers.py not found"}
    try:
        result = subprocess.run(
            [sys.executable, str(watcher_script), "restart", "--agent", agent_id],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return {"status": "ok"}
        return {"status": "error", "stderr": result.stderr.strip()}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def queue_summaries() -> list[dict]:
    summaries: list[dict] = []
    for project in load_projects():
        project_id = project.get("id")
        if not isinstance(project_id, str) or not issue_queue.queue_exists(project_id):
            continue
        backlog_status = "clean"
        missing_issues: list[int] = []
        backlog_error = None
        result = issue_queue.reconcile_queue(project_id)
        if result.get("backlog") == "unknown":
            backlog_status = "unknown"
            backlog_error = str(result.get("reason"))
            payload = issue_queue.load_queue(project_id)
        else:
            payload = result["projection"]
            if issue_queue.projection_input_changed(project_id, payload):
                issue_queue.sync_markdown(project_id, payload)
            missing_issues = [
                int(item["issue"])
                for item in result.get("needs_materialization", [])
                if isinstance(item, dict) and isinstance(item.get("issue"), int)
            ]
            if missing_issues or result.get("duplicate_mirrors"):
                backlog_status = "drift"
        ready_lane = issue_queue.next_ready_lane(payload)
        summaries.append(
            {
                "project_id": project_id,
                "queue_path": display_path(issue_queue.queue_markdown_path(project_id)),
                "queue_empty": not bool(payload.get("lanes")),
                "ready_lane": ready_lane,
                "backlog_status": backlog_status,
                "missing_issues": missing_issues,
                "duplicate_mirrors": result.get("duplicate_mirrors", []),
                "backlog_error": backlog_error,
            }
        )
    return summaries


# The CONTRACT_VERSION marker lives in the leading HTML comment of AGENTS.md.
# The shared bounded reader (bin/_bounded_io.py) deliberately REFUSES a file
# larger than its limit, which fits markers but not a deliberate prefix scan of
# a tracked file that is legitimately tens of KB — so the bound is local: read
# only this many bytes, then apply the same 200-character window as before.
CONTRACT_HEADER_READ_BYTES = 4096


def contract_version(path=None) -> str:
    """The CONTRACT_VERSION marker from AGENTS.md, or "unknown" when unreadable.

    The one read of the marker: session_bootstrap's announcement and the
    SessionStart hook (bin/session_gate.py) both go through here, so the read
    is bounded — the hook makes it automatic at every session start.
    """
    target = path if path is not None else ROOT / "AGENTS.md"
    try:
        with open(target, "rb") as handle:
            head = handle.read(CONTRACT_HEADER_READ_BYTES).decode("utf-8", errors="replace")[:200]
    except OSError:
        return "unknown"
    marker = re.search(r"CONTRACT_VERSION:\s*(\S+)", head)
    return marker.group(1) if marker else "unknown"


def announce_contract(agent_id: str) -> None:
    """Print the contract version and this agent's own drifted instruction copies.

    Bootstrap is the one command every worker runs, so it is where drift has to surface.
    A canonical document does not help on its own: nobody re-reads a document they
    believe they already know, which is how eight agent memory files ended up teaching a
    deliver.py invocation that silently dropped packets.
    """
    contract = ROOT / "AGENTS.md"
    version = contract_version()
    if version == "unknown":
        # An unreadable contract used to print nothing; silence looked like a
        # section that ran. UNKNOWN is not a pass.
        print(f"[contract] AGENTS.md version UNKNOWN — could not read {contract}")
    else:
        print(f"[contract] AGENTS.md version {version} — canonical worker contract")
        print(f"[contract] if your last session predates it, read "
              f"'Recent contract changes' in {contract}")

    checker = ROOT / "bin" / "contract_drift.py"
    if not checker.exists():
        return
    try:
        done = subprocess.run(
            [sys.executable, str(checker), "--agent", agent_id],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if done.returncode != 0 and done.stdout:
        first = done.stdout.splitlines()[0]
        print(f"[contract] {first}")
        print(f"[contract] run: python bin/contract_drift.py --agent {agent_id}")
    print()


def binding_drifts(agent_id: str) -> dict:
    """Classify this runtime's canonical binding without mutating or guessing scope."""
    current_runtime_id = runtime_id_from_env()
    if not current_runtime_id:
        return {"status": "not_applicable", "reason": "runtime_id_unavailable"}
    try:
        sessions = iter_sessions(agent_id=agent_id, strict=True)
    except Exception as error:
        return {
            "status": "unavailable",
            "reason": f"{type(error).__name__}: {error}",
        }

    scopes: dict[tuple[str, str], list[dict]] = {}
    for session in sessions:
        if not session_is_dispatchable(session)[0]:
            continue
        project_id = session.get("project_id")
        chat_id = session.get("chat_id")
        runtime = session.get("runtime") or {}
        if not project_id or not chat_id or not runtime.get("family"):
            continue
        scopes.setdefault((str(project_id), str(chat_id)), []).append(session)

    current_scopes = {
        scope
        for scope, records in scopes.items()
        if any(
            str((record.get("runtime") or {}).get("session_id") or "")
            == current_runtime_id
            for record in records
        )
    }
    logical_session_id = os.environ.get("LLM_COLLAB_SESSION_ID", "").strip()
    logical_scopes = {
        scope
        for scope, records in scopes.items()
        if logical_session_id
        and any(str(record.get("session_id") or "") == logical_session_id for record in records)
    }
    correlated_scopes = current_scopes or logical_scopes or set(scopes)
    if len(correlated_scopes) > 1:
        return {
            "status": "ambiguous",
            "current_runtime_id": current_runtime_id,
            "candidate_scope_count": len(correlated_scopes),
            "question": "Which project/chat owns this restarting session?",
        }
    if not correlated_scopes:
        return {"status": "clear", "current_runtime_id": current_runtime_id}

    project_id, chat_id = next(iter(correlated_scopes))
    try:
        canonical = resolve_active_canonical_binding(
            project_id, chat_id, agent_id, current_runtime_id, strict=True
        )
    except CanonicalBindingNativeMismatch as mismatch:
        return {
            "status": "detected",
            "project_id": project_id,
            "chat_id": chat_id,
            "bound_runtime_id": mismatch.canonical_native_session_id,
            "current_runtime_id": current_runtime_id,
            "repair_available": False,
        }
    except Exception as error:
        return {
            "status": "unavailable",
            "reason": f"{type(error).__name__}: {error}",
        }
    return {
        "status": "clear",
        "current_runtime_id": current_runtime_id,
        "canonical_binding_resolved": canonical is not None,
    }


def announce_binding_drifts(report: dict) -> None:
    status = report.get("status")
    if status in {"clear", "not_applicable"}:
        return
    print("━" * 60)
    if status == "unavailable":
        print("⚠️  BINDING DRIFT CHECK UNAVAILABLE")
        print("━" * 60)
        print(f"  {report.get('reason', 'session scan could not be completed')}")
        print("  Bootstrap will continue, but no claim about binding drift was made.")
    elif status == "ambiguous":
        print("⚠️  BINDING DRIFT CHECK AMBIGUOUS")
        print("━" * 60)
        print(f"  {report['question']}")
        print("  No repair command was generated; peer sessions were not targeted.")
    else:
        print("⚠️  BINDING DRIFT — this native session is not the active binding")
        print("━" * 60)
        print(f"  scope           {report['project_id']}/{report['chat_id']}")
        print(f"  active binding  {report['bound_runtime_id']}")
        print(f"  current runtime {report['current_runtime_id']}")
        print("  No self-service repair exists yet: non-Pi registration does not")
        print("  update the canonical binding. Bootstrap did not mutate any lease.")
    print("━" * 60)
    print()


# (filename, test_critical). The file is the semantic boundary: requirements-dev
# holds what the suite needs to run truthfully — the schema-validator pins whose
# absence makes the conformance validators raise instead of skip, so the run
# reports conformance failures and collects fewer tests than exist. A missing one
# of those falsifies the suite. requirements-runtime holds the file-events pin,
# which ObservationEngine.start() catches on ImportError and
# tests/test_collabd_observe.py proves reconciliation continues without; its absence
# degrades the runtime, it does not falsify a test result. So they are reported, but
# not under the same banner. (Package names are deliberately not spelled here: the
# pins are read from the files, and a runtime file that named the dev validator
# would be a dependency on it — see test_runtime_directories_do_not_consume_dev.)
REQUIREMENTS = (
    ("requirements-dev.txt", True),
    ("requirements-runtime.txt", False),
)
MAX_REQUIREMENTS_BYTES = 256 * 1024


class RequirementsUnreadable(RuntimeError):
    """A requirements file exists but could not be read, or is over-sized.

    Distinct from absence: an unknown pin set must never silently become an empty
    one, because an empty set reports the environment as complete.
    """


def _read_requirements_bounded(path: Path, remaining: int) -> str | None:
    """Read one requirements file under a cumulative byte budget.

    Returns None when the file is legitimately absent — a workspace may carry only
    one — and raises RequirementsUnreadable when the file is over-budget, unreadable,
    or not valid UTF-8.

    The bound is applied at the READ, not via stat-then-read: a `stat` size check
    can pass and the file grow before `read_text()` reaches EOF, which is unbounded
    again. Reading at most `remaining + 1` bytes is bounded regardless of what the
    file does after the open. Invalid UTF-8 becomes a read failure rather than an
    uncaught UnicodeDecodeError that would crash bootstrap on a corrupt file.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(remaining + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RequirementsUnreadable(f"cannot read {path.name}: {error}") from error
    if len(data) > remaining:
        raise RequirementsUnreadable(
            f"{path.name} exceeds the remaining {remaining} byte budget"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RequirementsUnreadable(f"{path.name} is not valid UTF-8: {error}") from error


# The interpreter the required suite actually runs on. AGENTS.md's command is
# `python3.11 -m unittest discover -s tests`, and the bin/llm-collab wrapper will
# happily launch bootstrap under a different 3.10+ interpreter — so asking our own
# importlib.metadata answers the wrong question. We probe python3.11.
TEST_INTERPRETER = "python3.11"


def parse_requirements() -> tuple[list[dict], list[dict]]:
    """Return (pins, read_failures).

    Each pin is {name, pinned_version|None, test_critical}. Each read_failure is
    {detail, test_critical}: a file that exists but cannot be read makes ITS pin set
    UNKNOWN, and the criticality has to travel with it — an unreadable degradable
    file must not be shouted under the test-critical banner, and an unreadable
    test-critical file must be. An UNKNOWN set must never silently become an empty
    (complete-looking) one.
    """
    pins: list[dict] = []
    read_failures: list[dict] = []
    seen: set[str] = set()
    remaining = MAX_REQUIREMENTS_BYTES
    for filename, test_critical in REQUIREMENTS:
        try:
            text = _read_requirements_bounded(ROOT / filename, remaining)
        except RequirementsUnreadable as error:
            read_failures.append({"detail": str(error), "test_critical": test_critical})
            continue
        if text is None:
            continue
        remaining -= len(text.encode("utf-8"))
        for line in text.splitlines():
            entry = line.split("#", 1)[0].strip()
            if not entry or entry.startswith("-"):
                continue
            name = re.split(r"[=<>!~\[; ]", entry, maxsplit=1)[0].strip()
            if not name or name in seen:
                continue
            seen.add(name)
            exact = re.search(r"==\s*([0-9][^\s;#]*)", entry)
            pins.append({
                "name": name,
                "pinned_version": exact.group(1) if exact else None,
                "test_critical": test_critical,
            })
    return pins, read_failures


def _installed_versions(
    names: list[str], interpreter: str = TEST_INTERPRETER
) -> dict[str, str | None] | None:
    """Ask the test interpreter which pins it has, and at what version.

    Returns {name: version | None(absent) | '?'(metadata unreadable)}, or None when
    the test interpreter cannot be probed at all — which is UNKNOWN, not complete.
    Runs one short subprocess under `interpreter` rather than reading our own
    importlib.metadata, because the suite's environment is the one that must be
    truthful and it may not be this process's.

    `interpreter` defaults to the declared TEST_INTERPRETER, which is the right
    answer at bootstrap. A caller that is about to launch the suite itself must
    pass the interpreter it will actually use: probing a different one reports on
    an environment the suite never runs in.
    """
    if not names:
        return {}
    probe = (
        "import json,sys\n"
        "from importlib.metadata import version, PackageNotFoundError\n"
        "out={}\n"
        "for n in sys.argv[1:]:\n"
        "    try: out[n]=version(n)\n"
        "    except PackageNotFoundError: out[n]=None\n"
        "    except Exception: out[n]='?'\n"
        "print(json.dumps(out))\n"
    )
    try:
        done = subprocess.run(
            [interpreter, "-c", probe, *names],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def dependency_report(interpreter: str = TEST_INTERPRETER) -> dict:
    """Classify the pinned environment as it exists for `interpreter`.

    Missing/mismatched split by criticality, because only a test-critical gap
    falsifies a test result — that earns the loud banner; a runtime gap is reported,
    not shouted (GH-357/#362 ruling). Version is enforced, not merely presence: a
    pin bumped on a long-lived interpreter that still has the old wheel is exactly
    how the suite runs outside its declared environment while looking complete.
    """
    pins, read_failures = parse_requirements()
    report = {
        "test_interpreter": interpreter,
        "interpreter_unprobeable": False,
        "critical_missing": [], "critical_mismatched": [],
        "runtime_missing": [], "runtime_mismatched": [],
        "read_failures": read_failures,
    }
    installed = _installed_versions([pin["name"] for pin in pins], interpreter)
    if installed is None:
        # Cannot verify the suite's environment at all. That is UNKNOWN and
        # test-critical: proceeding as if complete is the exact silent-pass this
        # gate exists to stop.
        report["interpreter_unprobeable"] = True
        return report

    for pin in pins:
        missing_key = "critical_missing" if pin["test_critical"] else "runtime_missing"
        mism_key = "critical_mismatched" if pin["test_critical"] else "runtime_mismatched"
        found = installed.get(pin["name"])
        if found is None:
            report[missing_key].append(pin["name"])
            continue
        if found == "?":
            # Metadata unreadable is not the same as absent: reporting it as missing
            # would send a worker to install what is already installed (#370's
            # error-is-not-an-answer). Leave it out of both lists.
            continue
        if pin["pinned_version"] and found != pin["pinned_version"]:
            report[mism_key].append(f"{pin['name']} {found} != pinned {pin['pinned_version']}")
    return report


def announce_dependencies(report: dict) -> None:
    interpreter = report.get("test_interpreter", "the test interpreter")
    critical_reads = [f["detail"] for f in report["read_failures"] if f["test_critical"]]
    runtime_reads = [f["detail"] for f in report["read_failures"] if not f["test_critical"]]
    critical = (report["critical_missing"] + report["critical_mismatched"]
                + critical_reads)
    runtime = report["runtime_missing"] + report["runtime_mismatched"] + runtime_reads

    if report.get("interpreter_unprobeable"):
        print("━" * 60)
        print(f"⚠️  CANNOT VERIFY {interpreter} — test results here are not real")
        print("━" * 60)
        print(f"  {interpreter} could not be run to check its installed pins, so")
        print(f"  the environment the required suite runs in is UNKNOWN — which is")
        print(f"  not the same as complete. Install {interpreter} and its pins")
        print("  before running or quoting any test result.")
        print("━" * 60)
        return

    if critical:
        print("━" * 60)
        print("⚠️  TEST-CRITICAL DEPENDENCIES WRONG — test results here are not real")
        print("━" * 60)
        print(f"  {interpreter} (the interpreter the required suite runs on):")
        for item in report["critical_missing"]:
            print(f"    missing     {item}")
        for item in report["critical_mismatched"]:
            print(f"    wrong ver   {item}")
        for failure in critical_reads:
            print(f"    unreadable  {failure} (pin set is UNKNOWN, not empty)")
        print()
        print("  The suite does not skip what it cannot import. The runtime-adapter")
        print("  conformance validators raise instead, so the run reports failures")
        print("  in conformance rather than a missing package, and silently collects")
        print("  fewer tests than exist. A worker who reads that output concludes")
        print("  main is broken; one who reports it hands a collaborator a false")
        print("  baseline. Both happened on 2026-07-28: 1700 tests with 131 failures")
        print("  and 31 errors, against a main that is 1856 and green.")
        print()
        print("  Install the declared pins before running or quoting any test result:")
        print("    requirements-dev.txt")
        print("━" * 60)

    if runtime:
        # Reported, not shouted: a degradable pin's absence (or an unreadable
        # degradable file) is caught and tested, and does not falsify a test result.
        print("[deps] runtime pins not satisfied (degradable, not test-critical):")
        for item in runtime:
            print(f"[deps]   {item}")


def main():
    args = parse_args()

    known = agent_ids()
    if args.agent not in known:
        print(f"[error] Unknown agent: {args.agent!r}", file=sys.stderr)
        print(f"       Known agents: {', '.join(known)}", file=sys.stderr)
        sys.exit(1)

    agent = get_agent(args.agent)

    # Before anything reads the inbox or starts a watcher: is this checkout even
    # allowed to speak for the workspace? A stale one answers every later question
    # with an older contract, so the refusal belongs ahead of the first answer.
    currency = tooling_currency()
    if not args.json_output:
        announce_tooling(currency, allowed=getattr(args, "allow_stale_tooling", False))
    if currency["state"] == TOOLING_STALE and not getattr(args, "allow_stale_tooling", False):
        if args.json_output:
            print(json.dumps({"tooling": currency, "bootstrap": "refused"}, sort_keys=True))
        sys.exit(1)

    dependencies = dependency_report()
    if not args.json_output:
        announce_dependencies(dependencies)

    if not args.json_output:
        announce_contract(args.agent)

    # ── 1. Identity (FIRST — the LLM must read this before anything else) ──
    identity_file = agent_identity_path(args.agent)
    if identity_file.exists():
        identity_content = identity_file.read_text().strip()
        if not args.json_output:
            print("\n" + "═" * 60)
            print("IDENTITY")
            print("═" * 60)
            print(identity_content)
            print("═" * 60 + "\n")
    else:
        if not args.json_output:
            print(f"[warn] No identity file at {identity_file}", file=sys.stderr)
            print(f"       Run: python scripts/init.py to generate identity files.\n", file=sys.stderr)
        identity_content = None

    drift_info = binding_drifts(args.agent)
    if not args.json_output:
        announce_binding_drifts(drift_info)

    # Keep the potentially five-second optional probe behind identity output.
    ax_result = probe_ax_trust(agent)

    # ── 2. Inbox ──
    try:
        messages = get_unread_messages(args.agent, limit=args.limit)
    except InboxScanLimitExceeded as error:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "bootstrap": "refused",
                        "error": "inbox_scan_limit_exceeded",
                        "detail": str(error),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"[inbox] {error}", file=sys.stderr)
        sys.exit(75)
    inbox_summary = {
        "unread_count": len(messages),
        "messages": [
            {
                "path": m["path"],
                "from": m["frontmatter"].get("from"),
                "title": m["frontmatter"].get("title"),
                "priority": m["frontmatter"].get("priority"),
                "project_id": m["frontmatter"].get("project_id"),
            }
            for m in messages
        ],
    }

    if not args.json_output:
        if messages:
            print(f"[inbox] {len(messages)} unread message(s):\n")
            for i, m in enumerate(messages, 1):
                fm = m["frontmatter"]
                proj = f"  [{fm['project_id']}]" if fm.get("project_id") else ""
                print(f"  {i}. [{fm.get('priority','normal').upper()}]{proj} {fm.get('title','(no title)')} (from: {fm.get('from','?')})")
            print(f"\nRun: python bin/inbox.py --me {args.agent}   to read messages\n")
        else:
            print(f"[inbox] No unread messages for {args.agent}.\n")

    # ── 2b. Queue summaries ──
    queue_info = queue_summaries()
    if not args.json_output and queue_info:
        print("[queue]")
        for item in queue_info:
            if item["backlog_status"] == "unknown":
                print(
                    f"  - {item['project_id']}: backlog unknown; GitHub unavailable ({item['backlog_error']}) "
                    f"({item['queue_path']})"
                )
                continue
            if item["backlog_status"] == "error":
                print(
                    f"  - {item['project_id']}: backlog error: {item['backlog_error']} "
                    f"({item['queue_path']})"
                )
                continue
            if item["backlog_status"] == "drift":
                issues = ", ".join(f"GH-{issue}" for issue in item["missing_issues"])
                duplicate_issues = ", ".join(
                    f"GH-{entry['issue']}"
                    for entry in item.get("duplicate_mirrors", [])
                    if isinstance(entry, dict) and isinstance(entry.get("issue"), int)
                )
                details = []
                if issues:
                    details.append(f"missing eligible GitHub issue(s): {issues}")
                if duplicate_issues:
                    details.append(f"duplicate task mirror(s): {duplicate_issues}")
                detail = "; ".join(details) or "queue projection drift"
                print(
                    f"  - {item['project_id']}: DRIFT {detail} "
                    f"({item['queue_path']})"
                )
                continue
            if item["queue_empty"]:
                print(f"  - {item['project_id']}: queue empty confirmed against GitHub ({item['queue_path']})")
                continue
            ready = item["ready_lane"]
            if ready:
                print(
                    f"  - {item['project_id']}: next ready GH-{ready['issue']} / {ready['task_id']} / {ready['owner']} "
                    f"({issue_queue.lane_next_action(ready)}) "
                    f"({item['queue_path']})"
                )
            else:
                print(f"  - {item['project_id']}: queue has no ready lane ({item['queue_path']})")
        print("")

    if not args.json_output:
        print(format_ax_status(ax_result))

    # ── 3. Watcher ──
    watcher_result = {"status": "skipped"}
    activation = agent.get("activation", {})
    should_start_watcher = (
        activation.get("watcher_enabled", False)
        and not args.no_watcher
        and not is_human_relay(agent)
    )

    if should_start_watcher:
        watcher_result = start_watcher(args.agent)
        if not args.json_output:
            status = watcher_result.get("status", "?")
            print(f"[watcher] {status}")

    if args.json_output:
        print(json.dumps({
            "agent": args.agent,
            "bootstrapped_utc": utc_iso(),
            "tooling": currency,
            "dependencies": dependencies,
            "identity_loaded": identity_content is not None,
            "binding_drift": drift_info,
            "inbox": inbox_summary,
            "queues": queue_info,
            "watcher": watcher_result,
            "ax": ax_result.as_dict(),
        }, indent=2))


if __name__ == "__main__":
    main()
