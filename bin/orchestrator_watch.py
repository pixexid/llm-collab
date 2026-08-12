#!/usr/bin/env python3.11
"""Run one standard project-scoped orchestrator watcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT))

from _helpers import (  # noqa: E402
    get_project,
    project_state_dir,
    utc_iso,
    write_file_durably,
)
from _python_runtime import require_python  # noqa: E402
import _watcher_liveness  # noqa: E402
import pr_watch  # noqa: E402
from _bounded_io import read_regular_file_bounded  # noqa: E402
from llm_collab.bb_client import (  # noqa: E402
    MAX_RESPONSE_CHARS,
    PINNED_BB_VERSION,
    BbExecutableRefused,
    BbProjectIdRefused,
    BbResponseTooLarge,
    bb_executable_from_project,
    bb_project_id_from_project,
    subprocess_transport,
)

require_python()

PR_ENUM_CAP = 200
HEARTBEAT_ENUM_CAP = 1000
TERMINAL_CYCLES = 30
MAX_STATE_BYTES = 1 << 20

# Successful PR cycles repeat after 45s; heartbeat reports
# remain 10 minutes apart but refresh liveness every 60s between reports. Every
# mode also has one 300s cumulative cycle deadline. Thus a healthy watcher's
# marker is at most 60s + 300s = 360s old, leaving 240s inside the external 600s
# staleness bound. A cycle that cannot finish within that margin fails and
# correctly lets its marker go stale.
PR_REFRESH_SECONDS = 45.0
HEARTBEAT_REPORT_SECONDS = 600.0
HEARTBEAT_MARKER_REFRESH_SECONDS = 60.0
WATCHER_CYCLE_DEADLINE_SECONDS = 300.0
SUPPORTED_PR_STATES = frozenset({"open", "closed"})
PR_HEAD_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
THREAD_ENUM_MAX_RESPONSE_CHARS = MAX_RESPONSE_CHARS
TLS_CAPTURE_TIMEOUT_SECONDS = 5.0
TLS_CAPTURE_MAX_RESPONSE_CHARS = 1 << 20
TLS_CAPTURE_MTIME_FLOOR_SECONDS = 30.0
PROXY_ENV_KEYS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
TLS_HOST_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63})"
    r"(?::(\d{1,5}))?"
)
TLS_FAILURE_HINT_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:tls|ssl|certificate|cert|x509)(?![a-z0-9])|verif",
    re.IGNORECASE,
)


class ProbeError(RuntimeError):
    """A watcher check did not produce one complete sample."""


class HeartbeatProbeFailure(ProbeError):
    """Heartbeat probes already emitted their exact failures but need one seam."""

    def __init__(self, failures: Sequence[Exception]):
        self.failures = tuple(failures)
        super().__init__(f"{len(self.failures)} heartbeat probe(s) failed")


def emit_event(line: str) -> bool:
    """Make every watcher event immediately visible to a pipe-backed Monitor."""
    try:
        print(line, flush=True)
    except Exception:
        # The Monitor pipe is the reporting channel; if it is gone there is no
        # second channel to report through, but its failure must not kill the
        # watcher.
        return False
    return True


def require_event_delivery(emit: Callable[[str], object], line: str) -> None:
    if emit(line) is False:
        raise ProbeError("watcher event was not delivered to the Monitor")


@dataclass(frozen=True)
class WatcherConfig:
    bb_executable: tuple[str, ...]
    bb_project_ids: tuple[str, ...]
    github_repo: str
    timeout_seconds: float
    project_id: str = "project-a"


def project_config(project_id: str, mode: str) -> WatcherConfig:
    project = get_project(project_id)
    if project is None:
        raise ProbeError(f"unregistered project {project_id!r}")
    bb = project.get("bb")
    if not isinstance(bb, Mapping):
        raise ProbeError(f"project {project_id!r} has no bb configuration")
    # GH-728: the executable comes from the one resolver seam; an absent or
    # malformed bb.executable refuses rather than falling back to PATH bb.
    try:
        executable = bb_executable_from_project(project)
    except BbExecutableRefused as error:
        raise ProbeError(str(error)) from error
    repos = project.get("repos")
    repo_targets = (
        sorted(key for key in repos if isinstance(key, str) and key)
        if isinstance(repos, Mapping)
        else []
    )
    if not repo_targets:
        raise ProbeError(f"project {project_id!r} has no repositories")
    try:
        bb_project_ids = tuple(dict.fromkeys(
            bb_project_id_from_project(project, project_id, target)
            for target in repo_targets
        ))
    except BbProjectIdRefused as error:
        if not error.raw_nonempty:
            raise ProbeError(f"{error.field} must be non-empty text") from error
        raise ProbeError(
            f"{error.field} {error.value!r} has surrounding whitespace; "
            "refusing (match raw, reject padded)"
        ) from error
    github = project.get("github")
    repo = github.get("repo") if isinstance(github, Mapping) else None
    if not isinstance(repo, str) or not repo:
        raise ProbeError(f"project {project_id!r} has no github.repo")
    timeout = bb.get("timeout_seconds", 30.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ProbeError("bb.timeout_seconds must be positive")
    return WatcherConfig(
        project_id=project_id,
        bb_executable=tuple(executable),
        bb_project_ids=bb_project_ids,
        github_repo=repo if isinstance(repo, str) else "",
        timeout_seconds=float(timeout),
    )


def probe_json(
    executable: Sequence[str],
    argv: Sequence[str],
    timeout: float,
    *,
    max_response_chars: int = MAX_RESPONSE_CHARS,
):
    """Run one bounded command and require one complete JSON response."""
    try:
        result = subprocess_transport(
            executable, max_response_chars=max_response_chars
        )(argv, timeout)
    except Exception as error:
        raise ProbeError(str(error) or type(error).__name__) from error
    if result.exit_code != 0:
        raise ProbeError(result.stderr.strip() or f"exit {result.exit_code}")
    try:
        return json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ProbeError(f"malformed JSON: {error}") from error


def probe_thread_json(
    executable: Sequence[str], argv: Sequence[str], timeout: float
):
    return probe_json(
        executable,
        argv,
        timeout,
        max_response_chars=THREAD_ENUM_MAX_RESPONSE_CHARS,
    )


def cycle_deadline(deadline: float | None, monotonic: Callable[[], float]) -> float:
    return (
        monotonic() + WATCHER_CYCLE_DEADLINE_SECONDS
        if deadline is None
        else deadline
    )


def remaining_cycle_seconds(deadline: float, monotonic: Callable[[], float]) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ProbeError("watcher cycle exceeded its cumulative deadline")
    return remaining


def request_silent_wake(
    config: WatcherConfig,
    producer: str,
    semantic: str,
    deadline: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Use the plugin's one durable coalescer; host watchers keep no wake state."""
    try:
        result = subprocess_transport(config.bb_executable)(
            (
                "silent-wake",
                "emit",
                "--project",
                config.project_id,
                "--producer",
                producer,
                "--semantic",
                semantic,
            ),
            min(
                config.timeout_seconds,
                remaining_cycle_seconds(deadline, monotonic),
            ),
        )
    except Exception as error:
        raise ProbeError(f"silent wake command failed: {error}") from error
    if result.exit_code != 0:
        raise ProbeError(
            "silent wake command refused: "
            + (result.stderr.strip() or f"exit {result.exit_code}")
        )


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
        if (
            not isinstance(row.get("id"), str)
            or not row["id"].strip()
            or not isinstance(row.get("status"), str)
            or not row["status"].strip()
        ):
            raise ProbeError("bb thread row has no non-empty text id/status")
        if row.get("title") is not None and not isinstance(row["title"], str):
            raise ProbeError("bb thread row title is not text")
        archived_at = row.get("archivedAt")
        if archived_at is not None and (
            isinstance(archived_at, bool) or not isinstance(archived_at, int)
        ):
            raise ProbeError("bb thread row archivedAt is not null or integer timestamp")
    return rows


def project_thread_rows(
    config: WatcherConfig,
    *,
    include_hidden: bool,
    call: Callable[[Sequence[str], Sequence[str], float], object],
    deadline: float,
    monotonic: Callable[[], float],
) -> list[dict]:
    """Return one complete aggregate or raise before exposing a partial sample."""
    rows: list[dict] = []
    remaining_chars = MAX_RESPONSE_CHARS
    for project_id in config.bb_project_ids:
        argv = ["thread", "list", "--project", project_id]
        if include_hidden:
            argv.append("--include-hidden")
        argv.append("--json")
        payload = call(
            config.bb_executable,
            tuple(argv),
            min(config.timeout_seconds, remaining_cycle_seconds(deadline, monotonic)),
        )
        project_rows = thread_rows(payload)
        response_chars = len(json.dumps(payload, separators=(",", ":")))
        if response_chars > remaining_chars:
            raise ProbeError(
                f"bb thread list aggregate exceeds {MAX_RESPONSE_CHARS} chars"
            )
        remaining_chars -= response_chars
        rows.extend(project_rows)
        remaining_cycle_seconds(deadline, monotonic)
    return rows


def open_numbers(
    kind: str,
    repo: str,
    cap: int,
    deadline: float | None = None,
    *,
    call: Callable[[Sequence[str], Sequence[str], float], object] = probe_json,
    timeout: float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[int]:
    deadline = cycle_deadline(deadline, monotonic)
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
        min(timeout, remaining_cycle_seconds(deadline, monotonic)),
    )
    remaining_cycle_seconds(deadline, monotonic)
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


def pr_signature(repo: str, number: int, deadline: float) -> dict:
    signature, _ = pr_watch.snapshot(repo, str(number), deadline)
    if (
        not isinstance(signature, dict)
        or signature.get("state") not in SUPPORTED_PR_STATES
        or not isinstance(signature.get("merged"), bool)
        or not isinstance(signature.get("head"), str)
        or PR_HEAD_PATTERN.fullmatch(signature["head"]) is None
    ):
        raise ProbeError(f"PR #{number} returned an invalid signature")
    return signature


def pr_cycle(
    config: WatcherConfig,
    state: dict,
    *,
    enumerate_prs: Callable[..., list[int]] = open_numbers,
    signature: Callable[[str, int, float], dict] = pr_signature,
    emit: Callable[[str], None] = emit_event,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = cycle_deadline(deadline, monotonic)
    open_prs = enumerate_prs(
        "pr", config.github_repo, PR_ENUM_CAP, deadline, monotonic=monotonic
    )
    signatures = state.get("signatures", {})
    terminal_left = state.get("terminal_left", {})
    if not isinstance(signatures, dict) or not isinstance(terminal_left, dict):
        raise ProbeError("PR watcher state has invalid signatures/terminal_left")
    watched = sorted(set(open_prs) | {int(number) for number in signatures})

    # Poll the complete set before mutating state. One failed PR makes the whole
    # cycle incomplete, so earlier results must not masquerade as a full sample.
    snapshots = {}
    for number in watched:
        remaining_cycle_seconds(deadline, monotonic)
        snapshots[number] = signature(config.github_repo, number, deadline)
        remaining_cycle_seconds(deadline, monotonic)
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
            require_event_delivery(emit, f"PR #{number} armed (baseline captured)")
            continue
        if encoded != previous:
            next_signatures[key] = encoded
            line = (
                f"PR #{number} TIMELINE CHANGED — inspect the complete reviewed "
                f"artifact set at head {sample['head'][:7]}"
            )
            require_event_delivery(emit, line)
            request_silent_wake(
                config,
                "pr-artifacts",
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                deadline,
                monotonic=monotonic,
            )
        terminal = sample["merged"] or sample["state"] == "closed"
        if not terminal:
            next_terminal.pop(key, None)
            continue
        remaining = next_terminal.get(key, TERMINAL_CYCLES) - 1
        if remaining <= 0:
            next_signatures.pop(key, None)
            next_terminal.pop(key, None)
            require_event_delivery(
                emit,
                f"PR #{number} retired from the watch set after the post-merge window",
            )
        else:
            next_terminal[key] = remaining
    remaining_cycle_seconds(deadline, monotonic)
    state.clear()
    state.update(updated)
    return True


def heartbeat_cycle(
    config: WatcherConfig,
    *,
    call: Callable[[Sequence[str], Sequence[str], float], object] = probe_thread_json,
    enumerate_open: Callable[..., list[int]] = open_numbers,
    emit: Callable[[str], None] = emit_event,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = cycle_deadline(deadline, monotonic)
    failures: list[Exception] = []
    current = "?"
    workers: int | str = "?"
    counts: dict[str, int | str] = {"pr": "?", "issue": "?"}
    try:
        version = call(
            config.bb_executable,
            ("settings", "version", "--json"),
            min(
                config.timeout_seconds,
                remaining_cycle_seconds(deadline, monotonic),
            ),
        )
        remaining_cycle_seconds(deadline, monotonic)
        current_value = version.get("currentVersion") if isinstance(version, dict) else None
        if not isinstance(current_value, str) or not current_value.strip():
            raise ProbeError("settings version response has no currentVersion")
        current = current_value
    except Exception as error:
        failures.append(error)
        require_event_delivery(
            emit,
            f"BB VERSION CHECK FAILED (pin={PINNED_BB_VERSION!r} installed='?') — "
            f"{error}; later quiet cycles prove nothing until this is fixed"
        )
    if current != "?" and current != PINNED_BB_VERSION:
        line = (
            f"BB VERSION MISMATCH pin={PINNED_BB_VERSION} installed={current} — "
            "bin/bb_spawn.py will refuse bb_version_mismatch; run the bb-update "
            "procedure before starting lanes"
        )
        require_event_delivery(emit, line)
        request_silent_wake(
            config,
            "heartbeat",
            hashlib.sha256(current.encode("utf-8")).hexdigest(),
            deadline,
            monotonic=monotonic,
        )
    try:
        rows = project_thread_rows(
            config,
            include_hidden=False,
            call=call,
            deadline=deadline,
            monotonic=monotonic,
        )
        workers = sum(
            row["status"] in {"active", "starting"} and row.get("archivedAt") is None
            for row in rows
        )
    except Exception as error:
        failures.append(error)
        require_event_delivery(emit, f"HEARTBEAT WORKER PROBE FAILED — {error}")
    for kind in ("pr", "issue"):
        try:
            counts[kind] = len(
                enumerate_open(
                    kind,
                    config.github_repo,
                    HEARTBEAT_ENUM_CAP,
                    deadline,
                    monotonic=monotonic,
                )
            )
        except Exception as error:
            failures.append(error)
            require_event_delivery(
                emit, f"HEARTBEAT {kind.upper()} ENUMERATION FAILED — {error}"
            )
    require_event_delivery(
        emit,
        f"HEARTBEAT openPRs={counts['pr']} liveWorkers={workers} "
        f"openIssues={counts['issue']} — NEITHER number is the writing-lane count; "
        "derive that from your own lane list. If writing lanes<2 AND a startable "
        "issue exists (not blocked-on-external, not parked-by-decision, not an epic) "
        "start it; a drained queue is a status, not an order; never invent work"
    )
    try:
        remaining_cycle_seconds(deadline, monotonic)
    except ProbeError as error:
        failures.append(error)
        require_event_delivery(emit, f"HEARTBEAT CYCLE DEADLINE FAILED — {error}")
    if failures:
        raise HeartbeatProbeFailure(failures)
    return True


def is_certificate_verification_failure(error: BaseException) -> bool:
    # Deliberately over-inclusive across Go, Node, and future clients: an extra
    # artifact on an already-failed cycle is cheaper than silently missing the
    # incident because one runtime used an unenumerated certificate phrase.
    return TLS_FAILURE_HINT_PATTERN.search(str(error)) is not None


def endpoint_host_from_error(error: BaseException) -> tuple[str, int] | None:
    text = str(error)
    url = re.search(r"https?://[^\s\"']+", text, re.IGNORECASE)
    if url:
        parsed = urlsplit(url.group(0))
        try:
            return (parsed.hostname, parsed.port or 443) if parsed.hostname else None
        except ValueError:
            return None
    host = TLS_HOST_PATTERN.search(text)
    if host:
        port = int(host.group(2)) if host.group(2) else 443
        return (host.group(1), port) if port <= 65535 else None
    return None


def proxy_environment_section() -> str:
    lines = ["=== PROXY ENVIRONMENT ==="]
    configured = [
        f"{key}: {'empty' if os.environ[key] == '' else 'set (value redacted)'}"
        for key in PROXY_ENV_KEYS
        if key in os.environ
    ]
    lines.extend(configured or ["(none set)"])
    return "\n".join(lines)


def fetch_tls_evidence(endpoint: tuple[str, int] | None, timeout_seconds: float) -> str:
    """Capture raw command output within one shared hard deadline."""
    deadline = time.monotonic() + timeout_seconds
    remaining_chars = TLS_CAPTURE_MAX_RESPONSE_CHARS

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("TLS forensic capture timed out")
        return value

    def run(executable: Sequence[str], argv: Sequence[str]):
        nonlocal remaining_chars
        if remaining_chars <= 0:
            raise RuntimeError("TLS forensic output budget exhausted")
        # The transport bounds each stream independently, so split the shared
        # remainder between stdout and stderr before either stream is read.
        try:
            result = subprocess_transport(
                executable, max_response_chars=remaining_chars // 2
            )(argv, remaining())
        except BbResponseTooLarge as error:
            # GH-757: an overflowing probe spends the whole shared budget, so a
            # later probe cannot run against an unshrunk remainder.
            remaining_chars = 0
            raise RuntimeError("TLS forensic output budget exhausted") from error
        used = len(result.stdout) + len(result.stderr)
        if used > remaining_chars:
            remaining_chars = 0
            raise RuntimeError("TLS forensic output budget exhausted")
        remaining_chars -= used
        return result

    def command_section(
        label: str,
        display_command: str,
        executable: Sequence[str] | None,
        argv: Sequence[str] = (),
        *,
        unavailable: str | None = None,
    ) -> str:
        lines = [f"=== {label} ===", f"$ {display_command}"]
        if unavailable is not None:
            lines.append(f"FAILED: {unavailable}")
            return "\n".join(lines)
        try:
            assert executable is not None
            result = run(executable, argv)
            output = result.stdout + result.stderr
            if output:
                lines.append(output.rstrip("\n"))
            if result.exit_code != 0:
                lines.append(f"FAILED: exit {result.exit_code}")
            elif not output:
                lines.append("(command returned no output)")
        except Exception as error:
            lines.append(f"FAILED: {str(error) or type(error).__name__}")
        return "\n".join(lines)

    if endpoint is None:
        missing = "endpoint host could not be determined"
        openssl_section = command_section("OPENSSL S_CLIENT", "not run", None, unavailable=missing)
        system_dns_section = command_section(
            "SYSTEM DNS (A AND AAAA)", "not run", None, unavailable=missing
        )
        public_dns_section = command_section(
            "PUBLIC DNS 1.1.1.1 (A AND AAAA)", "not run", None, unavailable=missing
        )
    else:
        host, port = endpoint
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        openssl = shutil.which("openssl")
        openssl_display = (
            f"openssl s_client -connect {target} -servername {host} "
            "-showcerts </dev/null"
        )
        openssl_shell = (
            "/bin/sh",
            "-c",
            'exec "$1" s_client -connect "$2" -servername "$3" '
            "-showcerts </dev/null",
            "tls-capture",
            openssl or "openssl",
            target,
            host,
        )
        openssl_section = command_section(
            "OPENSSL S_CLIENT",
            openssl_display,
            openssl_shell if openssl is not None else None,
            unavailable=None if openssl is not None else "openssl executable not found",
        )

        dig = shutil.which("dig")
        dig_shell = (
            '"$1" +time=1 +tries=1 "$2" A; '
            '"$1" +time=1 +tries=1 "$2" AAAA'
        )
        system_dns_section = command_section(
            "SYSTEM DNS (A AND AAAA)",
            f"dig {host} A; dig {host} AAAA",
            (
                "/bin/sh",
                "-c",
                dig_shell,
                "tls-capture",
                dig or "dig",
                host,
            )
            if dig is not None
            else None,
            unavailable=None if dig is not None else "dig executable not found",
        )
        public_dns_section = command_section(
            "PUBLIC DNS 1.1.1.1 (A AND AAAA)",
            f"dig @1.1.1.1 {host} A; dig @1.1.1.1 {host} AAAA",
            (
                "/bin/sh",
                "-c",
                '"$1" @1.1.1.1 +time=1 +tries=1 "$2" A; '
                '"$1" @1.1.1.1 +time=1 +tries=1 "$2" AAAA',
                "tls-capture",
                dig or "dig",
                host,
            )
            if dig is not None
            else None,
            unavailable=None if dig is not None else "dig executable not found",
        )

    return "\n\n".join(
        (
            openssl_section,
            system_dns_section,
            public_dns_section,
            proxy_environment_section(),
        )
    )


def capture_tls_failure(
    name: str,
    project_id: str,
    error: BaseException,
    timeout_seconds: float,
    *,
    failure_count: int = 1,
    fetcher: Callable[[tuple[str, int] | None, float], str] = fetch_tls_evidence,
    timestamp: Callable[[], str] = utc_iso,
    wall_time: Callable[[], float] = time.time,
) -> Path | None:
    directory = project_state_dir(project_id) / "tls-forensics"
    sentinel = directory / ".last-capture"
    now = wall_time()
    try:
        if now - sentinel.stat().st_mtime < TLS_CAPTURE_MTIME_FLOOR_SECONDS:
            return None
    except FileNotFoundError:
        pass
    except OSError:
        pass

    captured_at = timestamp()
    endpoint = endpoint_host_from_error(error)
    try:
        sections = fetcher(endpoint, timeout_seconds)
        if not isinstance(sections, str):
            raise TypeError("TLS evidence fetcher returned non-text output")
    except Exception as capture_error:
        detail = str(capture_error) or type(capture_error).__name__
        sections = "\n\n".join(
            (
                f"=== OPENSSL S_CLIENT ===\nFAILED: {detail}",
                f"=== SYSTEM DNS (A AND AAAA) ===\nFAILED: {detail}",
                f"=== PUBLIC DNS 1.1.1.1 (A AND AAAA) ===\nFAILED: {detail}",
                proxy_environment_section(),
            )
        )

    header = [
        f"UTC timestamp: {captured_at}",
        f"Watcher: {name}",
        f"Error: {error}",
        f"Endpoint host: {f'{endpoint[0]}:{endpoint[1]}' if endpoint else 'could not be determined'}",
        f"Cycle failure count: {failure_count}",
    ]
    if failure_count > 1:
        header.append("Captured failure: first only")
    stamp = re.sub(r"[^0-9A-Za-z]+", "-", captured_at).strip("-")
    safe_name = re.sub(r"[^0-9A-Za-z-]+", "-", name).strip("-")
    path = directory / f"{stamp}-{safe_name}-{uuid.uuid4().hex[:8]}.txt"
    write_file_durably(path, "\n".join(header) + "\n\n" + sections + "\n")
    try:
        write_file_durably(sentinel, captured_at + "\n")
        os.utime(sentinel, (now, now))
    except Exception:
        pass
    return path


def refresh_marker(
    name: str,
    project_id: str,
    session_id: str,
    *,
    emit: Callable[[str], None] = emit_event,
) -> bool:
    try:
        _watcher_liveness.write_marker(project_id, name, session_id)
    except Exception as error:
        try:
            require_event_delivery(
                emit, f"{name.upper()} MARKER WRITE FAILED — {error}"
            )
        except WatcherEventDeliveryError:
            raise
        except ProbeError:
            pass
        return False
    return True


def run_once(
    name: str,
    project_id: str,
    session_id: str,
    check: Callable[[], bool],
    *,
    emit: Callable[[str], None] = emit_event,
    tls_fetcher: Callable[[tuple[str, int] | None, float], str] = fetch_tls_evidence,
    monotonic: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
) -> bool:
    deadline = cycle_deadline(deadline, monotonic)

    def report(line: str) -> None:
        try:
            require_event_delivery(emit, line)
        except ProbeError:
            pass

    try:
        complete = check()
    except Exception as error:
        already_emitted = isinstance(error, HeartbeatProbeFailure)
        failures = error.failures if isinstance(error, HeartbeatProbeFailure) else (error,)
        forensic_error = next(
            filter(is_certificate_verification_failure, failures), None
        )
        failure_count = len(failures)
        if forensic_error is not None:
            try:
                capture_tls_failure(
                    name,
                    project_id,
                    forensic_error,
                    min(
                        TLS_CAPTURE_TIMEOUT_SECONDS,
                        max(0.0, deadline - monotonic()),
                    ),
                    failure_count=failure_count,
                    fetcher=tls_fetcher,
                )
            except (KeyboardInterrupt, SystemExit):
                if not already_emitted:
                    report(f"{name.upper()} CHECK FAILED — {error}")
                raise
            except Exception as capture_error:
                report(
                    f"{name.upper()} TLS FORENSIC CAPTURE FAILED — "
                    f"{str(capture_error) or type(capture_error).__name__}"
                )
        if not already_emitted:
            report(f"{name.upper()} CHECK FAILED — {error}")
        return False
    if not complete:
        return False
    return refresh_marker(name, project_id, session_id, emit=emit)


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
    content = json.dumps(state, sort_keys=True) + "\n"
    size = len(content.encode("utf-8"))
    if size > MAX_STATE_BYTES:
        raise ProbeError(
            f"watcher state would be {size} bytes; limit is {MAX_STATE_BYTES}"
        )
    write_file_durably(path, content)


def heartbeat_wait(
    project_id: str,
    session_id: str,
    completed: bool,
    *,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = emit_event,
) -> None:
    """Wait to the next report, refreshing only after a successful cycle."""
    if not completed:
        sleep(HEARTBEAT_REPORT_SECONDS)
        return
    remaining = HEARTBEAT_REPORT_SECONDS
    while remaining > HEARTBEAT_MARKER_REFRESH_SECONDS:
        sleep(HEARTBEAT_MARKER_REFRESH_SECONDS)

        refresh_marker("heartbeat", project_id, session_id, emit=emit)
        remaining -= HEARTBEAT_MARKER_REFRESH_SECONDS
    sleep(remaining)


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
            {"signatures": {}, "terminal_left": {}},
        )
    except ProbeError as error:
        print(f"REFUSED: {error}", file=sys.stderr, flush=True)
        return 1

    cycles = 0
    while True:
        cycles += 1
        deadline = cycle_deadline(None, time.monotonic)
        periodic_delivered = True
        if cycles % 20 == 0:
            periodic_delivered = emit_event(
                f"WATCHER LIVE ({args.mode}) cycle {cycles}"
            )

        def check() -> bool:
            if not periodic_delivered:
                return False
            if args.mode == "pr-artifacts":
                complete = pr_cycle(
                    config,
                    state,
                    deadline=deadline,
                )
                if complete:
                    save_state(state_path, state)
                return complete
            return heartbeat_cycle(
                config,
                deadline=deadline,
            )

        completed = run_once(
            args.mode,
            args.project,
            args.session,
            check,
            deadline=deadline,
        )
        if args.mode == "pr-artifacts":
            time.sleep(PR_REFRESH_SECONDS)
        else:
            heartbeat_wait(args.project, args.session, completed)


if __name__ == "__main__":
    raise SystemExit(main())
