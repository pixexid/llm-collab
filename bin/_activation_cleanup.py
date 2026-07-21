"""Fixture-safe activation poller audit for lease claims.

The lease authority stays in _activation_lease. This module owns only the
runtime cleanup precondition: an activation claim may not proceed while an
unregistered recurring poller for the same target identity is still present.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from shutil import which
from typing import Any, Callable

from _activation_lease import LeaseRefused, claim_lease, owner_summary, process_alive

POLLER_SHAPE_MARKERS = ("while true", "watch_inbox.py")
UNPROVEN_ACTIONS = {"reported_only", "terminate_denied", "termination_unverified"}


class PollerAuditUnavailable(Exception):
    """The process/PM2 audit could not prove the cleanup condition."""


def ps_fixture_path() -> Path | None:
    value = os.environ.get("LLM_COLLAB_PS_FIXTURE")
    return Path(value) if value else None


def poller_process_rows(ps_output: str | None = None) -> list[dict[str, Any]]:
    if ps_output is None:
        fixture = ps_fixture_path()
        if fixture is not None:
            try:
                ps_output = fixture.read_text()
            except OSError as exc:
                raise PollerAuditUnavailable(f"ps fixture unreadable: {exc}") from exc
    if ps_output is None:
        try:
            result = subprocess.run(
                ["ps", "eww", "-axo", "pid=,ppid=,command="],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise PollerAuditUnavailable(f"ps unavailable: {exc}") from exc
        if result.returncode != 0 or not result.stdout.strip():
            raise PollerAuditUnavailable(
                f"ps failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        ps_output = result.stdout
    if not ps_output.strip():
        raise PollerAuditUnavailable("empty process listing")
    rows: list[dict[str, Any]] = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "command": parts[2]})
    return rows


def pm2_registered_pids() -> set[int]:
    pm2_bin = os.environ.get("LLM_COLLAB_PM2_BIN") or which("pm2")
    if not pm2_bin:
        raise PollerAuditUnavailable("pm2 binary unavailable")
    try:
        result = subprocess.run(
            [pm2_bin, "jlist"], text=True, capture_output=True, check=False, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PollerAuditUnavailable(f"pm2 jlist unavailable: {exc}") from exc
    if result.returncode != 0:
        raise PollerAuditUnavailable(f"pm2 jlist failed (rc={result.returncode})")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PollerAuditUnavailable("pm2 jlist emitted invalid JSON") from exc
    pids: set[int] = set()
    if not isinstance(payload, list):
        raise PollerAuditUnavailable("pm2 jlist emitted non-list JSON")
    for process in payload:
        pid = process.get("pid") if isinstance(process, dict) else None
        if isinstance(pid, int) and pid > 0:
            pids.add(pid)
    return pids


def _agent_bound(command: str, target_agent: str) -> bool:
    return bool(re.search(rf"--me(?:=|\s+){re.escape(target_agent)}(?:\s|$)", command))


def matches_activation_identity(command: str, identity: dict[str, str]) -> str | None:
    if not any(marker in command for marker in POLLER_SHAPE_MARKERS):
        return None
    if identity["chat"] in command and _agent_bound(command, identity["target_agent"]):
        return f"chat:{identity['chat']}:agent:{identity['target_agent']}"
    if "watch_inbox.py" in command and _agent_bound(command, identity["target_agent"]):
        return f"watch_inbox:{identity['target_agent']}"
    return None


def ancestor_pids(rows: list[dict[str, Any]], start_pid: int) -> set[int]:
    parents = {row["pid"]: row.get("ppid") for row in rows}
    chain: set[int] = set()
    current: int | None = start_pid
    while current is not None and current not in chain:
        chain.add(current)
        current = parents.get(current)
    return chain


def default_wait_for_exit(pid: int, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process_alive(pid) is False:
            return True
        time.sleep(0.1)
    return process_alive(pid) is False


def terminate_verified(
    pid: int,
    *,
    kill: Callable[[int, int], None] = os.kill,
    wait_for_exit: Callable[[int], bool] = default_wait_for_exit,
) -> str:
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_exited"
    except PermissionError:
        return "terminate_denied"
    if wait_for_exit(pid):
        return "terminated"
    try:
        kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "terminate_denied"
    if wait_for_exit(pid):
        return "terminated_sigkill"
    return "termination_unverified"


def audit_activation_pollers(
    identity: dict[str, str],
    *,
    rows: list[dict[str, Any]] | None = None,
    registered_pids: set[int] | None = None,
    clean: bool = False,
    kill: Callable[[int, int], None] = os.kill,
    wait_for_exit: Callable[[int], bool] = default_wait_for_exit,
    self_pid: int | None = None,
) -> list[dict[str, Any]]:
    simulated = False
    if rows is None:
        simulated = ps_fixture_path() is not None
        rows = poller_process_rows()
    if registered_pids is None:
        registered_pids = pm2_registered_pids()
    if self_pid is None:
        self_pid = os.getpid()
    excluded = ancestor_pids(rows, self_pid)
    findings: list[dict[str, Any]] = []
    for row in rows:
        matched = matches_activation_identity(row["command"], identity)
        if matched is None or row["pid"] in excluded:
            continue
        finding = {
            "pid": row["pid"],
            "command": row["command"],
            "matched": matched,
            "registered": row["pid"] in registered_pids,
        }
        if finding["registered"]:
            finding["action"] = "preserved_registered_watch"
        elif not clean:
            finding["action"] = "reported_only"
        elif simulated:
            finding["action"] = "terminated"
            finding["simulated"] = True
        else:
            finding["action"] = terminate_verified(
                row["pid"], kill=kill, wait_for_exit=wait_for_exit
            )
        findings.append(finding)
    return findings


def audit_proves_clean(findings: list[dict[str, Any]]) -> bool:
    return not any(finding["action"] in UNPROVEN_ACTIONS for finding in findings)


def claim_activation_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    owner_pid: int | None = None,
    claimant_runtime_id: str | None = None,
    ttl_seconds: int = 3600,
    takeover: bool = True,
    clean_pollers: bool = True,
) -> dict[str, Any]:
    """Claim a lease only after stale pollers are proven absent or cleaned."""
    try:
        poller_audit = audit_activation_pollers(identity, clean=clean_pollers)
    except PollerAuditUnavailable as exc:
        raise LeaseRefused(
            "poller_audit_unavailable",
            {"detail": str(exc)},
        ) from exc
    if not audit_proves_clean(poller_audit):
        raise LeaseRefused(
            "poller_cleanup_unproven",
            {"poller_audit": poller_audit},
        )
    lease = claim_lease(
        identity,
        owner_session_id=owner_session_id,
        owner_pid=owner_pid,
        claimant_runtime_id=claimant_runtime_id,
        ttl_seconds=ttl_seconds,
        takeover=takeover,
    )
    return {
        "identity": identity,
        "lease": owner_summary(lease),
        "fence_token": lease["fence_token"],
        "owner_session_id": lease["owner_session_id"],
        "owner_runtime_session_id": lease.get("owner_runtime_session_id"),
        "owner_pid": lease.get("owner_pid"),
        "poller_audit": poller_audit,
    }
