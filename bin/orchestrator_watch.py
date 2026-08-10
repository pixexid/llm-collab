#!/usr/bin/env python3.11
"""Run one standard project-scoped orchestrator watcher."""

from __future__ import annotations

import argparse
import ipaddress
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

from _bounded_io import read_regular_file_bounded  # noqa: E402
from _helpers import (  # noqa: E402
    get_project,
    project_state_dir,
    utc_iso,
    write_file_durably,
)
from _python_runtime import require_python  # noqa: E402
import _watcher_liveness  # noqa: E402
import pr_watch  # noqa: E402
from llm_collab.bb_client import (  # noqa: E402
    MAX_RESPONSE_CHARS,
    PINNED_BB_VERSION,
    BbExecutableRefused,
    BbProjectIdRefused,
    bb_executable_from_project,
    bb_project_id_from_project,
    subprocess_transport,
)

require_python()

PR_ENUM_CAP = 200
HEARTBEAT_ENUM_CAP = 1000
TERMINAL_CYCLES = 30
MAX_STATE_BYTES = 1 << 20

# Successful worker and PR cycles repeat after 40s and 45s; heartbeat reports
# remain 10 minutes apart but refresh liveness every 60s between reports. Every
# mode also has one 300s cumulative cycle deadline. Thus a healthy watcher's
# marker is at most 60s + 300s = 360s old, leaving 240s inside the external 600s
# staleness bound. A cycle that cannot finish within that margin fails and
# correctly lets its marker go stale.
WORKER_REFRESH_SECONDS = 40.0
PR_REFRESH_SECONDS = 45.0
HEARTBEAT_REPORT_SECONDS = 600.0
HEARTBEAT_MARKER_REFRESH_SECONDS = 60.0
WATCHER_CYCLE_DEADLINE_SECONDS = 300.0
SUPPORTED_PR_STATES = frozenset({"open", "closed"})
PR_HEAD_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
THREAD_ENUM_MAX_RESPONSE_CHARS = MAX_RESPONSE_CHARS
TLS_CAPTURE_TIMEOUT_SECONDS = 5.0
TLS_CAPTURE_MAX_RESPONSE_CHARS = 1 << 20
TLS_RECORD_RETENTION_LIMIT = 200
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
TLS_CERTIFICATE_PATTERN = re.compile(
    r"-----BEGIN CERTIFICATE-----\r?\n.*?-----END CERTIFICATE-----", re.DOTALL
)
TLS_HOST_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63})"
    r"(?::\d{1,5})?"
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
    # GH-728: the executable comes from the one resolver seam; an absent or
    # malformed bb.executable refuses rather than falling back to PATH bb.
    try:
        executable = bb_executable_from_project(project)
    except BbExecutableRefused as error:
        raise ProbeError(str(error)) from error
    try:
        bb_project_id = bb_project_id_from_project(project, project_id)
    except BbProjectIdRefused as error:
        if not error.raw_nonempty:
            raise ProbeError("bb.project_id must be non-empty text") from error
        raise ProbeError(
            f"bb.project_id {error.value!r} has surrounding whitespace; "
            "refusing (match raw, reject padded)"
        ) from error
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


def worker_cycle(
    config: WatcherConfig,
    statuses: dict[str, str],
    *,
    call: Callable[[Sequence[str], Sequence[str], float], object] = probe_thread_json,
    emit: Callable[[str], None] = emit_event,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = cycle_deadline(deadline, monotonic)
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
            min(
                config.timeout_seconds,
                remaining_cycle_seconds(deadline, monotonic),
            ),
        )
    )
    updated = dict(statuses)
    for row in rows:
        thread_id = row["id"]
        status = row["status"]
        previous = statuses.get(thread_id)
        if previous == "active" and status != previous:
            title = (row.get("title") or "")[:40].replace(" ", "_")
            require_event_delivery(
                emit,
                f"WORKER LEFT ACTIVE {thread_id} ({title}): active -> {status} — "
                "go look (thread output AND log); idle does not mean finished"
            )
        updated[thread_id] = status
    remaining_cycle_seconds(deadline, monotonic)
    statuses.clear()
    statuses.update(updated)
    return True


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
            require_event_delivery(
                emit,
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
        require_event_delivery(
            emit,
            f"BB VERSION MISMATCH pin={PINNED_BB_VERSION} installed={current} — "
            "bin/bb_spawn.py will refuse bb_version_mismatch; run the bb-update "
            "procedure before starting lanes"
        )
    try:
        rows = thread_rows(
            call(
                config.bb_executable,
                ("thread", "list", "--project", config.bb_project_id, "--json"),
                min(
                    config.timeout_seconds,
                    remaining_cycle_seconds(deadline, monotonic),
                ),
            )
        )
        remaining_cycle_seconds(deadline, monotonic)
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


def endpoint_host_from_error(error: BaseException) -> str | None:
    text = str(error)
    url = re.search(r"https?://[^\s\"']+", text, re.IGNORECASE)
    if url:
        return urlsplit(url.group(0)).hostname
    host = TLS_HOST_PATTERN.search(text)
    return host.group(1) if host else None


def dns64_synthesis_matches(
    system_a: Sequence[str], system_aaaa: Sequence[str]
) -> list[dict[str, str]]:
    ipv4 = {str(ipaddress.IPv4Address(address)) for address in system_a}
    matches = []
    for address in system_aaaa:
        ipv6 = ipaddress.IPv6Address(address)
        embedded = str(ipaddress.IPv4Address(int(ipv6) & 0xFFFFFFFF))
        if embedded in ipv4:
            matches.append({"aaaa": str(ipv6), "embedded_ipv4": embedded})
    return matches


def sanitized_proxy_environment() -> dict[str, str | None]:
    values = {}
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value and key.lower() != "no_proxy":
            value = re.sub(
                r"^([a-z][a-z0-9+.-]*://)?[^/@\s]+@",
                lambda match: f"{match.group(1) or ''}<redacted>@",
                value,
                flags=re.IGNORECASE,
            )
        values[key] = value
    return values


def fetch_tls_evidence(host: str, timeout_seconds: float) -> dict:
    """Fetch chain, route, proxy, and DNS evidence within one hard deadline."""
    deadline = time.monotonic() + timeout_seconds

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("TLS forensic capture timed out")
        return value

    def run(executable: Sequence[str], argv: Sequence[str], cap: float):
        # Despite its bb-named parameter, subprocess_transport is the shared
        # bounded-process primitive. This coupling gives every forensic probe
        # one launch deadline, bounded reads, and the existing child cleanup.
        return subprocess_transport(
            executable, max_response_chars=TLS_CAPTURE_MAX_RESPONSE_CHARS
        )(argv, min(cap, remaining()))

    dns_snapshot = {
        "best_effort": True,
        "captured": False,
        "system_resolver": {"a": [], "aaaa": [], "error": None},
        "public_resolver": {
            "server": "1.1.1.1",
            "a": [],
            "aaaa": [],
            "error": None,
        },
        "dns64_synthesis_signature_present": None,
        "dns64_matches": [],
        "error": None,
    }
    resolver_script = (
        "import json,socket,sys; "
        "print(json.dumps(sorted({row[4][0] for row in "
        "socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))"
    )
    try:
        result = run((sys.executable,), ("-c", resolver_script, host), 0.6)
        if result.exit_code != 0:
            raise RuntimeError(result.stderr.strip() or f"exit {result.exit_code}")
        addresses = json.loads(result.stdout)
        if not isinstance(addresses, list) or any(
            not isinstance(address, str) for address in addresses
        ):
            raise ValueError("system resolver returned an invalid address list")
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            dns_snapshot["system_resolver"]["a" if parsed.version == 4 else "aaaa"].append(
                str(parsed)
            )
    except Exception as error:
        dns_snapshot["system_resolver"]["error"] = str(error) or type(error).__name__

    dig = shutil.which("dig")
    public_errors = []
    if dig is None:
        public_errors.append("dig executable not found")
    else:
        for record_type, key in (("A", "a"), ("AAAA", "aaaa")):
            try:
                result = run(
                    (dig,),
                    (
                        "@1.1.1.1",
                        "+time=1",
                        "+tries=1",
                        "+short",
                        host,
                        record_type,
                    ),
                    0.6,
                )
                if result.exit_code != 0:
                    raise RuntimeError(result.stderr.strip() or f"exit {result.exit_code}")
                for token in result.stdout.split():
                    try:
                        parsed = ipaddress.ip_address(token.rstrip("."))
                    except ValueError:
                        continue
                    if (record_type == "A" and parsed.version == 4) or (
                        record_type == "AAAA" and parsed.version == 6
                    ):
                        dns_snapshot["public_resolver"][key].append(str(parsed))
            except Exception as error:
                public_errors.append(
                    f"{record_type}: {str(error) or type(error).__name__}"
                )
    if public_errors:
        dns_snapshot["public_resolver"]["error"] = "; ".join(public_errors)

    system = dns_snapshot["system_resolver"]
    public = dns_snapshot["public_resolver"]
    if system["error"] is None:
        matches = dns64_synthesis_matches(system["a"], system["aaaa"])
        dns_snapshot["dns64_matches"] = matches
        dns_snapshot["dns64_synthesis_signature_present"] = bool(matches)
    dns_errors = [
        error
        for error in (system["error"], public["error"])
        if isinstance(error, str) and error
    ]
    dns_snapshot["captured"] = not dns_errors
    dns_snapshot["error"] = "; ".join(dns_errors) if dns_errors else None

    proxy_environment = sanitized_proxy_environment()
    proxy_configuration = {
        "best_effort": True,
        "captured": False,
        "environment": proxy_environment,
        "explicit_environment_proxy_present": any(
            value
            for key, value in proxy_environment.items()
            if key.lower() != "no_proxy"
        ),
        "system": {"command": None, "output": "", "error": None},
    }
    scutil = shutil.which("scutil")
    if scutil is None:
        proxy_configuration["system"]["error"] = "scutil executable not found"
    else:
        proxy_configuration["system"]["command"] = [scutil, "--proxy"]
        try:
            result = run((scutil,), ("--proxy",), 0.4)
            output = result.stdout.strip() or result.stderr.strip()
            proxy_configuration["system"].update(
                output=output,
                error=None if result.exit_code == 0 else f"exit {result.exit_code}",
            )
            proxy_configuration["captured"] = result.exit_code == 0
        except Exception as error:
            proxy_configuration["system"]["error"] = (
                str(error) or type(error).__name__
            )

    route_context = {
        "best_effort": True,
        "captured": False,
        "command": None,
        "output": "",
        "error": None,
    }
    route = shutil.which("route")
    ip = shutil.which("ip") if route is None else None
    route_command = (
        (route, "-n", "get", host)
        if route is not None
        else (ip, "route", "get", host)
        if ip is not None
        else ()
    )
    if not route_command:
        route_context["error"] = "no route or ip executable found"
    else:
        route_context["command"] = list(route_command)
        try:
            result = run((route_command[0],), route_command[1:], 0.4)
            output = result.stdout.strip() or result.stderr.strip()
            route_context.update(
                captured=bool(output),
                output=output,
                error=None if result.exit_code == 0 else f"exit {result.exit_code}",
            )
        except Exception as error:
            route_context["error"] = str(error) or type(error).__name__

    chain: list[str] = []
    chain_error: str | None = None
    target = f"[{host}]:443" if ":" in host else f"{host}:443"
    openssl_command = [
        "openssl",
        "s_client",
        "-connect",
        target,
        "-servername",
        host,
        "-showcerts",
    ]
    openssl_result = {"command": openssl_command, "exit_code": None, "stderr": ""}
    openssl = shutil.which("openssl")
    if openssl is None:
        chain_error = "openssl executable not found"
    else:
        shell_command = (
            "/bin/sh",
            "-c",
            'exec "$1" s_client -connect "$2" -servername "$3" '
            "-showcerts </dev/null",
            "tls-capture",
            openssl,
            target,
            host,
        )
        try:
            result = run(shell_command, (), remaining())
            openssl_result.update(exit_code=result.exit_code, stderr=result.stderr)
            chain = TLS_CERTIFICATE_PATTERN.findall(result.stdout)
            if not chain:
                chain_error = (
                    result.stderr.strip()
                    or f"openssl exited {result.exit_code} without a presented certificate"
                )
        except Exception as error:
            chain_error = f"openssl s_client failed: {str(error) or type(error).__name__}"

    return {
        "presented_certificate_chain_pem": chain,
        "chain_error": chain_error,
        "openssl": openssl_result,
        "network_route_context": route_context,
        "proxy_configuration": proxy_configuration,
        "route_fidelity": {
            "equivalent_to_failing_client": False,
            "reason": (
                "The OpenSSL probe used an independent direct connection; equivalence "
                "to the failing client's route was not established."
            ),
        },
        "dns_resolution_snapshot": dns_snapshot,
    }


def prune_tls_records(directory: Path, current: Path) -> None:
    records = sorted(
        (path for path in directory.glob("*.json") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    newest = [current, *(path for path in records if path != current)]
    keep = set(newest[:TLS_RECORD_RETENTION_LIMIT])
    for path in records:
        if path not in keep:
            path.unlink()


def capture_tls_failure(
    name: str,
    project_id: str,
    error: BaseException,
    timeout_seconds: float,
    *,
    fetcher: Callable[[str, float], Mapping] = fetch_tls_evidence,
    timestamp: Callable[[], str] = utc_iso,
) -> Path:
    captured_at = timestamp()
    host = endpoint_host_from_error(error)
    evidence: Mapping = {}
    fetch_error: str | None = None
    if host is None:
        fetch_error = "endpoint host could not be determined from the watcher error"
    elif timeout_seconds <= 0:
        fetch_error = "watcher cycle deadline exhausted before chain capture"
    else:
        try:
            evidence = fetcher(host, timeout_seconds)
            if not isinstance(evidence, Mapping):
                raise TypeError("TLS evidence fetcher returned a non-object")
        except Exception as capture_error:
            fetch_error = str(capture_error) or type(capture_error).__name__
            evidence = {}

    chain = evidence.get("presented_certificate_chain_pem", [])
    chain_captured = (
        isinstance(chain, list)
        and bool(chain)
        and all(isinstance(certificate, str) and certificate for certificate in chain)
    )
    if not chain_captured:
        chain = []
    chain_reason = None if chain_captured else (
        fetch_error
        or evidence.get("chain_error")
        or "no presented certificate chain was captured"
    )
    route_context = evidence.get("network_route_context")
    if not isinstance(route_context, Mapping):
        route_context = {
            "best_effort": True,
            "captured": False,
            "command": None,
            "output": "",
            "error": fetch_error or "route context was not returned",
        }

    route_context = dict(route_context)
    route_context["best_effort"] = True
    route_fidelity = evidence.get("route_fidelity")
    if not isinstance(route_fidelity, Mapping):
        route_fidelity = {
            "equivalent_to_failing_client": False,
            "reason": "Capture-route equivalence was not established.",
        }
    proxy_configuration = evidence.get("proxy_configuration")
    if not isinstance(proxy_configuration, Mapping):
        proxy_configuration = {
            "best_effort": True,
            "captured": False,
            "environment": {},
            "system": {"command": None, "output": "", "error": fetch_error},
        }
    proxy_configuration = dict(proxy_configuration)
    proxy_configuration["best_effort"] = True
    route_equivalent = route_fidelity.get("equivalent_to_failing_client") is True
    proxy_captured = proxy_configuration.get("captured") is True
    dns_snapshot = evidence.get("dns_resolution_snapshot")
    dns_captured = (
        isinstance(dns_snapshot, Mapping)
        and dns_snapshot.get("captured") is True
        and isinstance(dns_snapshot.get("dns64_synthesis_signature_present"), bool)
    )
    if not isinstance(dns_snapshot, Mapping):
        dns_snapshot = {
            "best_effort": True,
            "captured": False,
            "system_resolver": {"a": [], "aaaa": [], "error": fetch_error},
            "public_resolver": {
                "server": "1.1.1.1",
                "a": [],
                "aaaa": [],
                "error": fetch_error,
            },
            "dns64_synthesis_signature_present": None,
            "dns64_matches": [],
            "error": fetch_error or "DNS resolution snapshot was not returned",
        }
    dns_snapshot = dict(dns_snapshot)
    dns_snapshot["best_effort"] = True
    dns_reason = None if dns_captured else (
        fetch_error
        or dns_snapshot.get("error")
        or "DNS resolution snapshot was incomplete"
    )
    record = {
        "captured_at_utc": captured_at,
        "watcher_name": name,
        "error_text": str(error),
        "endpoint": {
            "host": host,
            "note": (
                "determined from watcher error text"
                if host is not None
                else "could not be determined from watcher error text"
            ),
        },
        "capture_timeout_seconds": timeout_seconds,
        "presented_certificate_chain_pem": chain,
        "openssl": evidence.get("openssl"),
        "network_route_context": route_context,
        "route_fidelity": dict(route_fidelity),
        "proxy_configuration": proxy_configuration,
        "dns_resolution_snapshot": dns_snapshot,
        "completeness": {
            "capture_complete": (
                chain_captured and dns_captured and route_equivalent and proxy_captured
            ),
            "chain_captured": chain_captured,
            "chain_reason": chain_reason,
            "route_equivalence_established": route_equivalent,
            "route_equivalence_reason": route_fidelity.get("reason"),
            "proxy_configuration_captured": proxy_captured,
            "proxy_configuration_reason": (
                None
                if proxy_captured
                else (
                    proxy_configuration.get("system", {}).get("error")
                    or "Proxy configuration was incomplete"
                )
                if isinstance(proxy_configuration.get("system"), Mapping)
                else "Proxy configuration was incomplete"
            ),
            "dns_snapshot_captured": dns_captured,
            "dns_snapshot_reason": dns_reason,
        },
    }
    stamp = re.sub(r"[^0-9A-Za-z]+", "-", captured_at).strip("-")
    safe_name = re.sub(r"[^0-9A-Za-z-]+", "-", name).strip("-")
    path = (
        project_state_dir(project_id)
        / "tls-forensics"
        / f"{stamp}-{safe_name}-{uuid.uuid4().hex[:8]}.json"
    )
    write_file_durably(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    try:
        prune_tls_records(path.parent, path)
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
        emit(f"{name.upper()} MARKER WRITE FAILED — {error}")
        return False
    return True


def run_once(
    name: str,
    project_id: str,
    session_id: str,
    check: Callable[[], bool],
    *,
    emit: Callable[[str], None] = emit_event,
    tls_fetcher: Callable[[str, float], Mapping] = fetch_tls_evidence,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = cycle_deadline(None, monotonic)
    try:
        complete = check()
    except Exception as error:
        already_emitted = isinstance(error, HeartbeatProbeFailure)
        forensic_error = error
        if already_emitted:
            forensic_error = next(
                (
                    failure
                    for failure in error.failures
                    if is_certificate_verification_failure(failure)
                ),
                error,
            )
        if is_certificate_verification_failure(forensic_error):
            try:
                capture_tls_failure(
                    name,
                    project_id,
                    forensic_error,
                    min(
                        TLS_CAPTURE_TIMEOUT_SECONDS,
                        max(0.0, deadline - monotonic()),
                    ),
                    fetcher=tls_fetcher,
                )
            except (KeyboardInterrupt, SystemExit):
                if not already_emitted:
                    emit(f"{name.upper()} CHECK FAILED — {error}")
                raise
            except Exception:
                pass
        if not already_emitted:
            emit(f"{name.upper()} CHECK FAILED — {error}")
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
            {"statuses": {}}
            if args.mode == "worker-lifecycle"
            else {"signatures": {}, "terminal_left": {}},
        )
    except ProbeError as error:
        print(f"REFUSED: {error}", file=sys.stderr, flush=True)
        return 1

    cycles = 0
    while True:
        cycles += 1
        periodic_delivered = True
        if cycles % 20 == 0:
            periodic_delivered = emit_event(
                f"WATCHER LIVE ({args.mode}) cycle {cycles}"
            )

        def check() -> bool:
            if not periodic_delivered:
                return False
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
            time.sleep(WORKER_REFRESH_SECONDS if completed else 45)
        elif args.mode == "pr-artifacts":
            time.sleep(PR_REFRESH_SECONDS)
        else:
            heartbeat_wait(args.project, args.session, completed)


if __name__ == "__main__":
    raise SystemExit(main())
