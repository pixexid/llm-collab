"""Fenced one-writer activation lease authority.

This module owns only the activation lease grant/assert/release authority. It
does not consume inbox packets, dispatch sessions, terminate pollers, or mutate
PM2 state.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from _activation_identity import lease_identity, lease_key
from _helpers import utc_iso, write_file
from _session_autobridge import AUTOBRIDGE_ROOT, load_session, parse_iso8601

ACTIVATION_LEASES_DIR = AUTOBRIDGE_ROOT / "activation_leases"
ACTIVATION_GRANT_LOCK = ACTIVATION_LEASES_DIR / ".claim-grant.lock"
LIVE_SESSION_STATUSES = {"active", "parked"}
CONTENTION_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}


class LeaseRefused(Exception):
    def __init__(self, reason: str, owner: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.owner = owner or {}


def _now() -> datetime:
    from _helpers import now_utc

    return now_utc()


def _expires_at(ttl_seconds: int) -> str:
    now = _now()
    return datetime.fromtimestamp(
        now.timestamp() + ttl_seconds, tz=now.tzinfo
    ).isoformat(timespec="seconds")


def lease_path(identity: dict[str, str]) -> Path:
    return ACTIVATION_LEASES_DIR / f"{lease_key(identity)}.json"


def load_lease(identity: dict[str, str]) -> dict[str, Any] | None:
    path = lease_path(identity)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _malformed_lease_state(path: Path, field: str, reason: str) -> LeaseRefused:
    return LeaseRefused(
        "corrupt_lease_state",
        {"lease_file": path.name, "field": field, "reason": reason},
    )


def _field_problem(payload: dict[str, Any], field: str) -> str | None:
    if field not in payload:
        return "missing"
    if payload[field] is None:
        return "null"
    if not isinstance(payload[field], str):
        return "wrong_type"
    return None


def _validate_active_lease_state(path: Path, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise _malformed_lease_state(path, "record", "wrong_type")
    status_problem = _field_problem(payload, "status")
    if status_problem is not None:
        if lease_is_expired(payload):
            return
        raise _malformed_lease_state(path, "status", status_problem)
    if payload["status"] != "active" or lease_is_expired(payload):
        return
    for field in ("worktree_realpath", "lease_key", "owner_session_id"):
        problem = _field_problem(payload, field)
        if problem is not None:
            raise _malformed_lease_state(path, field, problem)


def iter_leases() -> list[dict[str, Any]]:
    if not ACTIVATION_LEASES_DIR.exists():
        return []
    leases: list[dict[str, Any]] = []
    for path in sorted(ACTIVATION_LEASES_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise LeaseRefused(
                "corrupt_lease_state",
                {"lease_file": path.name, "field": "json", "reason": exc.__class__.__name__},
            ) from exc
        _validate_active_lease_state(path, payload)
        leases.append(payload)
    return leases


def save_lease(payload: dict[str, Any]) -> None:
    path = lease_path(payload["identity"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_utc"] = utc_iso()
    tmp = path.with_suffix(".tmp")
    write_file(tmp, json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


class _ClaimLock:
    """Stable never-unlinked flock for the per-identity critical section."""

    def __init__(self, identity: dict[str, str]):
        self.path = lease_path(identity).with_suffix(".lock")
        self.fd: int | None = None

    def __enter__(self) -> "_ClaimLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in CONTENTION_ERRNOS:
                raise LeaseRefused("claim_in_progress") from exc
            raise
        self.fd = fd
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


class _BlockingLock:
    """Stable never-unlinked flock for cross-identity grant serialization."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_BlockingLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in CONTENTION_ERRNOS:
                raise LeaseRefused("claim_in_progress") from exc
            raise
        self.fd = fd
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def _claim_grant_lock() -> _BlockingLock:
    return _BlockingLock(ACTIVATION_GRANT_LOCK)


def runtime_id_from_env() -> str | None:
    for name in (
        "LLM_COLLAB_READER_RUNTIME_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "GEMINI_SESSION_ID",
    ):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def pid_from_env() -> int | None:
    value = os.environ.get("LLM_COLLAB_READER_PID")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def valid_process_pid(pid: int | None) -> bool:
    return pid is not None and int(pid) > 0


def process_alive(pid: int | None) -> bool | None:
    if pid is None:
        return None
    if int(pid) <= 0:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        return None
    return True


def owner_session_record(owner_session_id: str) -> dict[str, Any] | None:
    try:
        return load_session(owner_session_id)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _session_expires_dead(record: dict[str, Any]) -> bool:
    expires_at = parse_iso8601(record.get("lease_expires_utc"))
    return expires_at is not None and expires_at <= _now()


def _session_is_live(record: dict[str, Any]) -> bool:
    return (
        record.get("status") in LIVE_SESSION_STATUSES
        and not _session_expires_dead(record)
    )


def _session_not_live_owner(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner_session_status": record.get("status"),
        "owner_session_lease_expires_utc": record.get("lease_expires_utc"),
    }


def owner_is_live(lease: dict[str, Any]) -> bool | None:
    pid_alive = process_alive(lease.get("owner_pid"))

    record = owner_session_record(str(lease.get("owner_session_id")))
    if record is None:
        return False if pid_alive is False else None
    if not _session_is_live(record):
        return False
    if pid_alive is True:
        return True
    if pid_alive is False:
        return False
    return True


def lease_is_expired(lease: dict[str, Any]) -> bool:
    expires_at = parse_iso8601(lease.get("lease_expires_utc"))
    return expires_at is not None and expires_at <= _now()


def owner_summary(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        "lease_key": lease.get("lease_key"),
        "owner_session_id": lease.get("owner_session_id"),
        "owner_runtime_session_id": lease.get("owner_runtime_session_id"),
        "owner_pid": lease.get("owner_pid"),
        "status": lease.get("status"),
        "fence_token": lease.get("fence_token"),
        "lease_expires_utc": lease.get("lease_expires_utc"),
        "claimed_utc": lease.get("claimed_utc"),
        "previous_owner_session_id": lease.get("previous_owner_session_id"),
    }


def _resolve_claimant(
    *,
    claimant_runtime_id: str | None,
    owner_pid: int | None,
) -> tuple[str | None, int | None]:
    runtime_id = claimant_runtime_id or runtime_id_from_env()
    pid = owner_pid if owner_pid is not None else pid_from_env()
    if pid is not None and not valid_process_pid(pid):
        raise LeaseRefused(
            "invalid_owner_pid",
            {"detail": "--owner-pid must be a positive process id"},
        )
    pid_live = process_alive(pid)
    if pid is not None and pid_live is False:
        raise LeaseRefused(
            "owner_pid_not_live",
            {"detail": "--owner-pid must name a live process"},
        )
    if runtime_id:
        return runtime_id, pid
    if pid is not None and pid_live is True:
        return None, pid
    raise LeaseRefused(
        "claimant_identity_required",
        {
            "detail": "lease claim requires --claimant-runtime-id, reader runtime env, or a live --owner-pid"
        },
    )


def _assert_claimant_matches(
    lease: dict[str, Any],
    *,
    claimant_runtime_id: str | None,
    owner_pid: int | None,
) -> None:
    runtime_id = claimant_runtime_id or runtime_id_from_env()
    pid = owner_pid if owner_pid is not None else pid_from_env()
    if pid is not None and not valid_process_pid(pid):
        raise LeaseRefused(
            "invalid_owner_pid",
            {"detail": "--owner-pid must be a positive process id"},
        )
    lease_runtime = lease.get("owner_runtime_session_id")
    lease_pid = lease.get("owner_pid")

    if lease_runtime is not None:
        if runtime_id is None:
            raise LeaseRefused("claimant_runtime_identity_required", owner_summary(lease))
        if str(lease_runtime) != str(runtime_id):
            raise LeaseRefused("claimant_runtime_mismatch", owner_summary(lease))
    if lease_pid is not None:
        if pid is None:
            raise LeaseRefused("claimant_pid_required", owner_summary(lease))
        if int(lease_pid) != int(pid):
            raise LeaseRefused("claimant_pid_mismatch", owner_summary(lease))


def _require_bound_session(record: dict[str, Any], identity: dict[str, str]) -> None:
    for record_field, identity_field in (
        ("agent_id", "target_agent"),
        ("project_id", "project"),
        ("chat_id", "chat"),
    ):
        if record.get(record_field) != identity[identity_field]:
            raise LeaseRefused(
                "owner_session_identity_mismatch",
                {
                    "field": record_field,
                    "session_value": record.get(record_field),
                    "identity_value": identity[identity_field],
                },
            )


def _claim_realpath(identity: dict[str, str]) -> str:
    try:
        resolved = Path(identity["worktree"]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LeaseRefused(
            "worktree_realpath_unavailable",
            {"detail": f"{identity['worktree']}: {exc.__class__.__name__}"},
        ) from exc
    if not resolved.is_dir():
        raise LeaseRefused(
            "worktree_realpath_unavailable",
            {"detail": f"{identity['worktree']} is not a directory"},
        )
    return str(resolved)


def _active_alias_collision(identity: dict[str, str], worktree_realpath: str) -> dict[str, Any] | None:
    this_key = lease_key(identity)
    for existing in iter_leases():
        if existing.get("lease_key") == this_key:
            continue
        if existing.get("status") != "active":
            continue
        if lease_is_expired(existing):
            continue
        if existing.get("worktree_realpath") != worktree_realpath:
            continue
        alive = owner_is_live(existing)
        if alive is True or alive is None:
            return existing
    return None


def claim_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    owner_pid: int | None = None,
    claimant_runtime_id: str | None = None,
    ttl_seconds: int = 3600,
    takeover: bool = False,
) -> dict[str, Any]:
    record = owner_session_record(owner_session_id)
    if record is None:
        raise LeaseRefused("owner_session_not_registered")
    if not _session_is_live(record):
        raise LeaseRefused("owner_session_not_live", _session_not_live_owner(record))
    _require_bound_session(record, identity)
    runtime_id, pid = _resolve_claimant(
        claimant_runtime_id=claimant_runtime_id, owner_pid=owner_pid
    )

    with _claim_grant_lock(), _ClaimLock(identity):
        worktree_realpath = _claim_realpath(identity)
        collision = _active_alias_collision(identity, worktree_realpath)
        if collision is not None:
            raise LeaseRefused("worktree_alias_collision", owner_summary(collision))

        existing = load_lease(identity)
        fence_token = 1
        previous_owner: str | None = None
        if existing is not None:
            fence_token = int(existing.get("fence_token", 0)) + 1
            previous_owner = existing.get("previous_owner_session_id")
            if existing.get("status") == "active":
                same_session = existing.get("owner_session_id") == owner_session_id
                same_runtime = existing.get("owner_runtime_session_id") == runtime_id
                existing_pid = existing.get("owner_pid")
                same_pid = (
                    existing_pid is not None
                    and pid is not None
                    and int(existing_pid) == int(pid)
                )
                runtime_only_reclaim = existing_pid is None and pid is None
                if lease_is_expired(existing):
                    if not takeover:
                        raise LeaseRefused(
                            "lease_expired_requires_takeover",
                            owner_summary(existing),
                        )
                    previous_owner = existing.get("owner_session_id")
                else:
                    alive = owner_is_live(existing)
                    if alive is False:
                        if not takeover:
                            raise LeaseRefused(
                                "dead_owner_requires_takeover",
                                owner_summary(existing),
                            )
                        previous_owner = existing.get("owner_session_id")
                    elif alive is None:
                        raise LeaseRefused("owner_liveness_unknown", owner_summary(existing))
                    elif same_session and same_runtime and (runtime_only_reclaim or same_pid):
                        fence_token = int(existing.get("fence_token", 1))
                        previous_owner = existing.get("previous_owner_session_id")
                    else:
                        reason = (
                            "same_session_different_claimant"
                            if same_session
                            else "lease_held_by_active_owner"
                        )
                        raise LeaseRefused(reason, owner_summary(existing))

        payload = {
            "identity": identity,
            "lease_key": lease_key(identity),
            "owner_session_id": owner_session_id,
            "owner_runtime_session_id": runtime_id,
            "owner_pid": pid,
            "status": "active",
            "fence_token": fence_token,
            "claimed_utc": utc_iso(),
            "lease_expires_utc": _expires_at(ttl_seconds),
            "previous_owner_session_id": previous_owner,
            "worktree_realpath": worktree_realpath,
        }
        save_lease(payload)
        return payload


def assert_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    fence_token: int,
    owner_pid: int | None = None,
    claimant_runtime_id: str | None = None,
) -> dict[str, Any]:
    record = owner_session_record(owner_session_id)
    if record is None:
        raise LeaseRefused("owner_session_not_registered")
    if not _session_is_live(record):
        raise LeaseRefused("owner_session_not_live", _session_not_live_owner(record))
    _require_bound_session(record, identity)
    lease = load_lease(identity)
    if lease is None:
        raise LeaseRefused("no_lease_for_identity")
    if lease.get("status") != "active":
        raise LeaseRefused("lease_not_active", owner_summary(lease))
    if lease.get("owner_session_id") != owner_session_id:
        raise LeaseRefused("lease_owned_by_other_session", owner_summary(lease))
    _assert_claimant_matches(
        lease,
        claimant_runtime_id=claimant_runtime_id,
        owner_pid=owner_pid,
    )
    if int(lease.get("fence_token", -1)) != int(fence_token):
        raise LeaseRefused("stale_fence_token", owner_summary(lease))
    if lease_is_expired(lease):
        raise LeaseRefused("lease_expired", owner_summary(lease))
    return lease


def release_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    fence_token: int,
    owner_pid: int | None = None,
    claimant_runtime_id: str | None = None,
    status: str = "released",
) -> dict[str, Any]:
    record = owner_session_record(owner_session_id)
    if record is None:
        raise LeaseRefused("owner_session_not_registered")
    if not _session_is_live(record):
        raise LeaseRefused("owner_session_not_live", _session_not_live_owner(record))
    _require_bound_session(record, identity)

    with _ClaimLock(identity):
        existing = load_lease(identity)
        if existing is None:
            raise LeaseRefused("no_lease_for_identity")
        if existing.get("owner_session_id") != owner_session_id:
            raise LeaseRefused("release_requires_current_owner", owner_summary(existing))
        _assert_claimant_matches(
            existing,
            claimant_runtime_id=claimant_runtime_id,
            owner_pid=owner_pid,
        )
        if int(existing.get("fence_token", -1)) != int(fence_token):
            raise LeaseRefused("stale_fence_token", owner_summary(existing))
        existing["status"] = status
        existing["released_utc"] = utc_iso()
        save_lease(existing)
        return existing
