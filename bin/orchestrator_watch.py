#!/usr/bin/env python3.11
"""Run one standard project-scoped orchestrator watcher."""

from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT))

from _bounded_io import read_regular_file_bounded  # noqa: E402
from _helpers import get_project, write_file_durably  # noqa: E402
from _python_runtime import require_python  # noqa: E402
import _watcher_liveness  # noqa: E402
import pr_watch  # noqa: E402
from llm_collab.bb_client import (  # noqa: E402
    PINNED_BB_VERSION,
    subprocess_transport,
)

require_python()

PR_ENUM_CAP = 200
HEARTBEAT_ENUM_CAP = 1000
TERMINAL_CYCLES = 30
MAX_STATE_BYTES = 1 << 20


class ProbeError(RuntimeError):
    """A watcher check did not produce one complete sample."""


@dataclass(frozen=True)
class WatcherConfig:
    bb_executable: tuple[str, ...]
    bb_project_id: str
    github_repo: str
    timeout_seconds: float


def project_config(project_id: str, mode: str) -> WatcherConfig:
    project = get_project(project_id)
    if project is None:
        raise ProbeError(f"unregistered project {project_id!r}")
    bb = project.get("bb")
    if not isinstance(bb, Mapping):
        raise ProbeError(f"project {project_id!r} has no bb configuration")
    executable = bb.get("executable", ["bb"])
    if (
        not isinstance(executable, list)
        or not executable
        or any(not isinstance(token, str) or not token for token in executable)
    ):
        raise ProbeError("bb.executable must be a non-empty list of strings")
    bb_project_id = bb.get("project_id", project_id)
    if not isinstance(bb_project_id, str) or not bb_project_id:
        raise ProbeError("bb.project_id must be non-empty text")
    github = project.get("github")
    repo = github.get("repo") if isinstance(github, Mapping) else None
    if mode != "worker-lifecycle" and (not isinstance(repo, str) or not repo):
        raise ProbeError(f"project {project_id!r} has no github.repo")
    timeout = bb.get("timeout_seconds", 30.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ProbeError("bb.timeout_seconds must be positive")
    return WatcherConfig(
        bb_executable=tuple(executable),
        bb_project_id=bb_project_id,
        github_repo=repo if isinstance(repo, str) else "",
        timeout_seconds=float(timeout),
    )


def probe_json(executable: Sequence[str], argv: Sequence[str], timeout: float):
    """Run one bounded command and require one complete JSON response."""
    try:
        result = subprocess_transport(executable)(argv, timeout)
    except Exception as error:
        raise ProbeError(str(error) or type(error).__name__) from error
    if result.exit_code != 0:
        raise ProbeError(result.stderr.strip() or f"exit {result.exit_code}")
    try:
        return json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ProbeError(f"malformed JSON: {error}") from error


def thread_rows(payload) -> list[dict]:
    """Accept only bb's observed list or {threads: list} response shapes."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("threads"), list):
        rows = payload["threads"]
    else:
        raise ProbeError("bb thread list response is not a list or {threads: list}")
    if any(not isinstance(row, dict) for row in rows):
        raise ProbeError("bb thread list contains a non-object row")
    for row in rows:
        if not isinstance(row.get("id"), str) or not isinstance(row.get("status"), str):
            raise ProbeError("bb thread row has no text id/status")
        if row.get("title") is not None and not isinstance(row["title"], str):
            raise ProbeError("bb thread row title is not text")
    return rows


def open_numbers(
    kind: str,
    repo: str,
    cap: int,
    *,
    call: Callable[[Sequence[str], Sequence[str], float], object] = probe_json,
    timeout: float = 30.0,
) -> list[int]:
    payload = call(
        ("gh",),
        (
            kind,
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(cap + 1),
            "--json",
            "number",
        ),
        timeout,
    )
    if not isinstance(payload, list) or any(
        not isinstance(row, dict)
        or isinstance(row.get("number"), bool)
        or not isinstance(row.get("number"), int)
        or row["number"] <= 0
        for row in payload
    ):
        raise ProbeError(f"gh {kind} list returned an invalid response shape")
    if len(payload) > cap:
        raise ProbeError(
            f"gh {kind} enumeration exceeded cap {cap}; refusing a partial sample"
        )
    return sorted({row["number"] for row in payload})


def worker_cycle(
    config: WatcherConfig,
    statuses: dict[str, str],
    *,
    call: Callable[[Sequence[str], Sequence[str], float], object] = probe_json,
    emit: Callable[[str], None] = print,
) -> bool:
    rows = thread_rows(
        call(
            config.bb_executable,
            (
                "thread",
                "list",
                "--project",
                config.bb_project_id,
                "--include-hidden",
                "--json",
            ),
            config.timeout_seconds,
        )
    )
    updated = dict(statuses)
    for row in rows:
        thread_id = row["id"]
        status = row["status"]
        previous = statuses.get(thread_id)
        if previous == "active" and status != previous:
            title = (row.get("title") or "")[:40].replace(" ", "_")
            emit(
                f"WORKER LEFT ACTIVE {thread_id} ({title}): active -> {status} — "
                "go look (thread output AND log); idle does not mean finished"
            )
        updated[thread_id] = status
    statuses.clear()
    statuses.update(updated)
    return True


def pr_signature(repo: str, number: int) -> dict:
    signature, _ = pr_watch.snapshot(repo, str(number))
    if (
        not isinstance(signature, dict)
        or not isinstance(signature.get("state"), str)
        or not isinstance(signature.get("merged"), bool)
        or not isinstance(signature.get("head"), str)
    ):
        raise ProbeError(f"PR #{number} returned an invalid signature")
    return signature


def pr_cycle(
    config: WatcherConfig,
    state: dict,
    *,
    enumerate_prs: Callable[[str, str, int], list[int]] = open_numbers,
    signature: Callable[[str, int], dict] = pr_signature,
    emit: Callable[[str], None] = print,
) -> bool:
    open_prs = enumerate_prs("pr", config.github_repo, PR_ENUM_CAP)
    signatures = state.get("signatures", {})
    terminal_left = state.get("terminal_left", {})
    if not isinstance(signatures, dict) or not isinstance(terminal_left, dict):
        raise ProbeError("PR watcher state has invalid signatures/terminal_left")
    watched = sorted(set(open_prs) | {int(number) for number in signatures})

    # Poll the complete set before mutating state. One failed PR makes the whole
    # cycle incomplete, so earlier results must not masquerade as a full sample.
    snapshots = {number: signature(config.github_repo, number) for number in watched}
    updated = deepcopy(state)
    next_signatures = updated.setdefault("signatures", {})
    next_terminal = updated.setdefault("terminal_left", {})
    for number in watched:
        key = str(number)
        sample = snapshots[number]
        encoded = json.dumps(sample, sort_keys=True, separators=(",", ":"))
        previous = signatures.get(key)
        if previous is None:
            next_signatures[key] = encoded
            emit(f"PR #{number} armed (baseline captured)")
            continue
        if encoded != previous:
            next_signatures[key] = encoded
            emit(
                f"PR #{number} TIMELINE CHANGED — inspect the complete reviewed "
                f"artifact set at head {sample['head'][:7]}"
            )
        terminal = sample["merged"] or sample["state"] == "closed"
        if not terminal:
            next_terminal.pop(key, None)
            continue
        remaining = next_terminal.get(key, TERMINAL_CYCLES) - 1
        if remaining <= 0:
            next_signatures.pop(key, None)
            next_terminal.pop(key, None)
            emit(f"PR #{number} retired from the watch set after the post-merge window")
        else:
            next_terminal[key] = remaining
    state.clear()
    state.update(updated)
    return True


def heartbeat_cycle(
    config: WatcherConfig,
    *,
    call: Callable[[Sequence[str], Sequence[str], float], object] = probe_json,
    enumerate_open: Callable[[str, str, int], list[int]] = open_numbers,
    emit: Callable[[str], None] = print,
) -> bool:
    complete = True
    current = "?"
    workers: int | str = "?"
    counts: dict[str, int | str] = {"pr": "?", "issue": "?"}
    try:
        version = call(
            config.bb_executable,
            ("settings", "version", "--json"),
            config.timeout_seconds,
        )
        current_value = version.get("currentVersion") if isinstance(version, dict) else None
        if not isinstance(current_value, str) or not current_value:
            raise ProbeError("settings version response has no currentVersion")
        current = current_value
    except Exception as error:
        complete = False
        emit(
            f"BB VERSION CHECK FAILED (pin={PINNED_BB_VERSION!r} installed='?') — "
            f"{error}; later quiet cycles prove nothing until this is fixed"
        )
    if current != "?" and current != PINNED_BB_VERSION:
        emit(
            f"BB VERSION MISMATCH pin={PINNED_BB_VERSION} installed={current} — "
            "bin/bb_spawn.py will refuse bb_version_mismatch; run the bb-update "
            "procedure before starting lanes"
        )
    try:
        rows = thread_rows(
            call(
                config.bb_executable,
                ("thread", "list", "--project", config.bb_project_id, "--json"),
                config.timeout_seconds,
            )
        )
        workers = sum(
            row["status"] in {"active", "starting"} and not row.get("archivedAt")
            for row in rows
        )
    except Exception as error:
        complete = False
        emit(f"HEARTBEAT WORKER PROBE FAILED — {error}")
    for kind in ("pr", "issue"):
        try:
            counts[kind] = len(enumerate_open(kind, config.github_repo, HEARTBEAT_ENUM_CAP))
        except Exception as error:
            complete = False
            emit(f"HEARTBEAT {kind.upper()} ENUMERATION FAILED — {error}")
    emit(
        f"HEARTBEAT openPRs={counts['pr']} liveWorkers={workers} "
        f"openIssues={counts['issue']} — NEITHER number is the writing-lane count; "
        "derive that from your own lane list. If writing lanes<2 AND a startable "
        "issue exists (not blocked-on-external, not parked-by-decision, not an epic) "
        "start it; a drained queue is a status, not an order; never invent work"
    )
    return complete


def run_once(
    name: str,
    project_id: str,
    session_id: str,
    check: Callable[[], bool],
    *,
    emit: Callable[[str], None] = print,
) -> bool:
    try:
        complete = check()
    except Exception as error:
        emit(f"{name.upper()} CHECK FAILED — {error}")
        return False
    if not complete:
        return False
    try:
        _watcher_liveness.write_marker(project_id, name, session_id)
    except Exception as error:
        emit(f"{name.upper()} MARKER WRITE FAILED — {error}")
        return False
    return True


def load_state(path: Path, default: dict) -> dict:
    try:
        value = json.loads(read_regular_file_bounded(path, MAX_STATE_BYTES).decode("utf-8"))
    except FileNotFoundError:
        return deepcopy(default)
    except Exception as error:
        raise ProbeError(f"cannot read watcher state {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProbeError(f"watcher state {path} is not a JSON object")
    return value


def save_state(path: Path, state: dict) -> None:
    write_file_durably(path, json.dumps(state, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=_watcher_liveness.WATCHER_NAMES)
    parser.add_argument("--project", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = project_config(args.project, args.mode)
        state_path = args.state_dir / f"{args.mode}.json"
        state = load_state(
            state_path,
            {"statuses": {}}
            if args.mode == "worker-lifecycle"
            else {"signatures": {}, "terminal_left": {}},
        )
    except ProbeError as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1

    cycles = 0
    while True:
        cycles += 1
        if cycles % 20 == 0:
            print(f"WATCHER LIVE ({args.mode}) cycle {cycles}")

        def check() -> bool:
            if args.mode == "worker-lifecycle":
                statuses = state.setdefault("statuses", {})
                if not isinstance(statuses, dict):
                    raise ProbeError("worker watcher statuses state is not an object")
                complete = worker_cycle(config, statuses)
                if complete:
                    save_state(state_path, state)
                return complete
            if args.mode == "pr-artifacts":
                complete = pr_cycle(config, state)
                if complete:
                    save_state(state_path, state)
                return complete
            return heartbeat_cycle(config)

        completed = run_once(
            args.mode,
            args.project,
            args.session,
            check,
        )
        if args.mode == "worker-lifecycle":
            time.sleep(40 if completed else 45)
        elif args.mode == "pr-artifacts":
            time.sleep(45)
        else:
            time.sleep(600)


if __name__ == "__main__":
    raise SystemExit(main())
