"""Fenced one-writer activation lease authority.

This module owns only the activation lease grant/assert/release authority. It
does not consume inbox packets, dispatch sessions, terminate pollers, or mutate
PM2 state.
"""

from __future__ import annotations

import contextvars
import errno
import fcntl
import functools
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from _activation_identity import IDENTITY_FIELDS, lease_identity, lease_key
from _helpers import get_project, project_state_dir, utc_iso, write_file
from _session_autobridge import AUTOBRIDGE_ROOT, load_session, parse_iso8601

# Legacy workspace-global lease root (pre-#160). New leases live per project under
# project_state_dir(project)/activation_leases; the code never reads, moves, or deletes
# this root — while it holds records the authority fails closed (_refuse_if_legacy_present).
LEGACY_ACTIVATION_LEASES_DIR = AUTOBRIDGE_ROOT / "activation_leases"
_LEASE_SUBDIR = "activation_leases"
_GRANT_LOCK_NAME = ".claim-grant.lock"


def _project_scope(identity_or_project: object) -> str:
    project = (
        identity_or_project.get("project")
        if isinstance(identity_or_project, dict)
        else identity_or_project
    )
    # The project becomes a path component under project_state_root, so it must be a
    # single safe segment AND a registered project (#160 requires a validated registered
    # project before any read, write, enumeration, or lock — path safety alone would let
    # a direct read/release/assert caller create authority under an unregistered project).
    if (
        not isinstance(project, str)
        or not project
        or project in (".", "..")
        or "/" in project
        or "\\" in project
        or "\x00" in project
    ):
        raise LeaseRefused("invalid_project_scope", {"project": project})
    if get_project(project) is None:
        raise LeaseRefused("unregistered_project_scope", {"project": project})
    return project


def lease_dir(identity_or_project: object) -> Path:
    # Single chokepoint for every read, write, enumeration, and lock. Validate the
    # registered project FIRST — an unregistered caller must be refused before the legacy
    # guard ever reads/enumerates the legacy authority — THEN run the fail-closed legacy
    # guard, THEN resolve the per-project directory.
    project = _project_scope(identity_or_project)
    _refuse_if_legacy_present()
    return project_state_dir(project) / _LEASE_SUBDIR


MAX_LEASE_DIRECTORY_ENTRIES = 4096

# One cumulative enumeration budget per lease operation. A single operation resolves the
# lease dir many times (legacy guard + grant lock + claim lock + alias scan + write), so a
# per-scan budget would let it scan MAX * N entries. The budget is set once at the operation
# boundary and every scan in that operation draws from it.
_scan_budget: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "activation_lease_scan_budget", default=None
)


@contextmanager
def _operation_scan_budget():
    """Establish (or share) the one cumulative scan budget for this lease operation."""
    if _scan_budget.get() is not None:
        yield  # already inside an operation budget; nested calls share it
        return
    token = _scan_budget.set([MAX_LEASE_DIRECTORY_ENTRIES])
    try:
        yield
    finally:
        _scan_budget.reset(token)


def _bounded_operation(func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _operation_scan_budget():
            return func(*args, **kwargs)

    return wrapper


def _scan_lease_records(directory: Path) -> list[Path]:
    """Bounded scan of a lease directory: the sorted ``*.json`` record paths, or fail
    closed when the operation's cumulative entry budget is exhausted. Every entry is
    counted (json or not) and the cap is enforced mid-scan, so an oversized directory
    refuses before a partial result is ever returned (the repository's bounded-work
    rule). Outside a lease operation a single-scan budget applies. Absent dir -> empty.
    """
    budget = _scan_budget.get()
    if budget is None:
        budget = [MAX_LEASE_DIRECTORY_ENTRIES]
    records: list[Path] = []
    try:
        scanner = os.scandir(directory)
    except FileNotFoundError:
        return []
    with scanner:
        for entry in scanner:
            budget[0] -= 1
            if budget[0] < 0:
                raise LeaseRefused(
                    "lease_directory_too_large",
                    {"directory": str(directory), "limit": MAX_LEASE_DIRECTORY_ENTRIES},
                )
            if entry.name.endswith(".json"):
                records.append(Path(entry.path))
    records.sort()
    return records


def _refuse_if_legacy_present() -> None:
    """Fail closed while pre-GH-160 workspace-global records still exist (#160 cutover).

    The per-project path is the sole authority. This module never reads, moves, or
    deletes the legacy root — it only refuses, so a read path (e.g. lease-assert) stays
    read-only and mutates nothing. The explicit one-owner cutover is an operator step:
    with no other activation-lease writer running, review the legacy records for any
    still-authoritative lease and archive or relocate the root (not a destructive
    delete) before the per-project authority resumes.
    """
    if _scan_lease_records(LEGACY_ACTIVATION_LEASES_DIR):
        raise LeaseRefused(
            "legacy_lease_migration_required",
            {"legacy_root": str(LEGACY_ACTIVATION_LEASES_DIR)},
        )


LIVE_SESSION_STATUSES = {"active", "parked"}
CONTENTION_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}
T = TypeVar("T")


class LeaseRefused(Exception):
    def __init__(self, reason: str, owner: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.owner = owner or {}


CAPABILITY_BINDING_VERSION = 1
CAPABILITY_BINDING_SCHEME = "injected_verifier_sha256"
_CAPABILITY_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CAPABILITY_VERIFIER_ORIGIN = "trusted_verifier"
_CAPABILITY_CONSTRUCTOR_TOKEN = object()
_CAPABILITY_BINDING_FIELDS = frozenset(
    {"version", "scheme", "lease_key", "fence_token", "proof_digest"}
)


class CallerCapabilityVerification:
    """Opaque result seam for a future trusted caller-capability verifier.

    This type is deliberately not constructible through a public authority
    path. The private fixture factory below exists only to exercise this
    default-disabled contract until a trusted transport is approved. The
    object is a verifier result, not proof that this module can establish
    caller possession by itself.
    """

    __slots__ = ("_origin", "_proof_digest")

    def __init__(self, proof_digest: str, *, origin: str, _token: object):
        if _token is not _CAPABILITY_CONSTRUCTOR_TOKEN:
            raise TypeError("caller capability verification is verifier-owned")
        self._origin = origin
        self._proof_digest = proof_digest

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def proof_digest(self) -> str:
        return self._proof_digest


# Private by design: current tests need a deterministic injected verifier seam,
# while no CLI or runtime path receives a caller-supplied token or digest.
def _injected_capability_verification(
    proof_digest: str, *, origin: str = _CAPABILITY_VERIFIER_ORIGIN
) -> CallerCapabilityVerification:
    return CallerCapabilityVerification(
        proof_digest, origin=origin, _token=_CAPABILITY_CONSTRUCTOR_TOKEN
    )


@dataclass(frozen=True)
class CallerCapabilityBindingV1:
    """Persisted, non-secret binding metadata for an injected verification."""

    version: int
    scheme: str
    lease_key: str
    fence_token: int
    proof_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "scheme": self.scheme,
            "lease_key": self.lease_key,
            "fence_token": self.fence_token,
            "proof_digest": self.proof_digest,
        }


def _valid_capability_digest(value: Any) -> bool:
    return isinstance(value, str) and _CAPABILITY_DIGEST_RE.fullmatch(value) is not None


def _binding_from_verification(
    identity: dict[str, str],
    fence_token: int,
    verification: Any,
) -> CallerCapabilityBindingV1:
    if not isinstance(verification, CallerCapabilityVerification):
        raise LeaseRefused("caller_capability_verification_required")
    if verification.origin != _CAPABILITY_VERIFIER_ORIGIN:
        raise LeaseRefused("untrusted_caller_capability_verifier")
    if not _valid_capability_digest(verification.proof_digest):
        raise LeaseRefused("invalid_caller_capability_digest")
    if type(fence_token) is not int or fence_token < 0:
        raise LeaseRefused("invalid_caller_capability_fence")
    return CallerCapabilityBindingV1(
        version=CAPABILITY_BINDING_VERSION,
        scheme=CAPABILITY_BINDING_SCHEME,
        lease_key=lease_key(identity),
        fence_token=fence_token,
        proof_digest=verification.proof_digest,
    )


def caller_capability_binding(
    identity: dict[str, str],
    *,
    fence_token: int,
    verification: Any,
) -> dict[str, Any]:
    """Create binding metadata from an injected verifier result only."""
    return _binding_from_verification(identity, fence_token, verification).as_dict()


def validate_caller_capability_binding(
    identity: dict[str, str],
    *,
    fence_token: int,
    binding: Any,
    verification: Any,
) -> CallerCapabilityBindingV1:
    """Validate exact binding shape and the same-fence verifier result.

    This is an inert seam: without a trusted transport producing
    ``CallerCapabilityVerification``, labels, PIDs, runtime IDs, session IDs,
    raw tokens, and caller-supplied digests cannot satisfy it.
    """
    expected = _binding_from_verification(identity, fence_token, verification)
    if not isinstance(binding, dict):
        raise LeaseRefused("malformed_caller_capability_binding")
    if set(binding) != _CAPABILITY_BINDING_FIELDS:
        raise LeaseRefused("malformed_caller_capability_binding")
    if binding != expected.as_dict():
        raise LeaseRefused("caller_capability_binding_mismatch")
    return expected


def _validate_stored_capability_binding(
    path: Path, payload: Any, *, expected_lease_key: str, expected_fence: int | None = None
) -> None:
    if not isinstance(payload, dict):
        raise _malformed_lease_state(path, "caller_capability_binding", "wrong_type")
    if set(payload) != _CAPABILITY_BINDING_FIELDS:
        raise _malformed_lease_state(path, "caller_capability_binding", "wrong_shape")
    if type(payload.get("version")) is not int or payload["version"] != CAPABILITY_BINDING_VERSION:
        raise _malformed_lease_state(path, "caller_capability_binding.version", "mismatch")
    if type(payload.get("scheme")) is not str or payload["scheme"] != CAPABILITY_BINDING_SCHEME:
        raise _malformed_lease_state(path, "caller_capability_binding.scheme", "mismatch")
    if type(payload.get("lease_key")) is not str or payload["lease_key"] != expected_lease_key:
        raise _malformed_lease_state(path, "caller_capability_binding.lease_key", "mismatch")
    fence = payload.get("fence_token")
    if type(fence) is not int or fence < 0:
        raise _malformed_lease_state(path, "caller_capability_binding.fence_token", "malformed")
    if expected_fence is not None and fence != expected_fence:
        raise _malformed_lease_state(path, "caller_capability_binding.fence_token", "mismatch")
    if not _valid_capability_digest(payload.get("proof_digest")):
        raise _malformed_lease_state(path, "caller_capability_binding.proof_digest", "malformed")


def _now() -> datetime:
    from _helpers import now_utc

    return now_utc()


def _expires_at(ttl_seconds: int) -> str:
    now = _now()
    return datetime.fromtimestamp(
        now.timestamp() + ttl_seconds, tz=now.tzinfo
    ).isoformat(timespec="seconds")


def lease_path(identity: dict[str, str]) -> Path:
    return lease_dir(identity) / f"{lease_key(identity)}.json"


@_bounded_operation
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


def _validate_active_lease_identity(
    path: Path, payload: dict[str, Any], expected_lease_key: str
) -> None:
    if payload.get("lease_key") != expected_lease_key:
        raise _malformed_lease_state(path, "lease_key", "mismatch")
    payload_identity = payload.get("identity")
    if payload_identity is None:
        reason = "missing" if "identity" not in payload else "null"
        raise _malformed_lease_state(path, "identity", reason)
    if not isinstance(payload_identity, dict):
        raise _malformed_lease_state(path, "identity", "wrong_type")
    for field in IDENTITY_FIELDS:
        problem = _field_problem(payload_identity, field)
        if problem is not None:
            raise _malformed_lease_state(path, f"identity.{field}", problem)
    try:
        identity = lease_identity(payload_identity)
    except ValueError as exc:
        raise _malformed_lease_state(path, "identity", "malformed") from exc
    if lease_key(identity) != expected_lease_key:
        raise _malformed_lease_state(path, "identity", "mismatch")


def _validate_active_lease_state(
    path: Path, payload: Any, *, expected_lease_key: str | None = None
) -> None:
    if not isinstance(payload, dict):
        raise _malformed_lease_state(path, "record", "wrong_type")
    status_problem = _field_problem(payload, "status")
    if status_problem is not None:
        if lease_is_expired(payload):
            return
        raise _malformed_lease_state(path, "status", status_problem)
    if payload["status"] != "active" or _active_lease_expired_or_corrupt(payload, path):
        return
    for field in ("worktree_realpath", "lease_key", "owner_session_id"):
        problem = _field_problem(payload, field)
        if problem is not None:
            raise _malformed_lease_state(path, field, problem)
    if expected_lease_key is not None:
        _validate_active_lease_identity(path, payload, expected_lease_key)
    binding = payload.get("caller_capability_binding")
    if binding is not None:
        binding_lease_key = payload.get("lease_key")
        binding_fence = payload.get("fence_token")
        _validate_stored_capability_binding(
            path,
            binding,
            expected_lease_key=binding_lease_key,
            expected_fence=binding_fence if type(binding_fence) is int else None,
        )


def _validate_loaded_lease_state(identity: dict[str, str], payload: Any) -> None:
    path = lease_path(identity)
    _validate_active_lease_state(path, payload)
    if not isinstance(payload, dict) or payload.get("status") != "active":
        return
    if payload.get("lease_key") != lease_key(identity):
        raise _malformed_lease_state(path, "lease_key", "mismatch")
    payload_identity = payload.get("identity")
    if payload_identity is None:
        reason = "missing" if "identity" not in payload else "null"
        raise _malformed_lease_state(path, "identity", reason)
    if not isinstance(payload_identity, dict):
        raise _malformed_lease_state(path, "identity", "wrong_type")
    for field, expected in identity.items():
        if field not in payload_identity:
            raise _malformed_lease_state(path, f"identity.{field}", "missing")
        if payload_identity[field] is None:
            raise _malformed_lease_state(path, f"identity.{field}", "null")
        if not isinstance(payload_identity[field], str):
            raise _malformed_lease_state(path, f"identity.{field}", "wrong_type")
        if payload_identity[field] != expected:
            raise _malformed_lease_state(path, f"identity.{field}", "mismatch")


@_bounded_operation
def load_authority_lease(identity: dict[str, str]) -> dict[str, Any] | None:
    path = lease_path(identity)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise LeaseRefused(
            "corrupt_lease_state",
            {"lease_file": path.name, "field": "json", "reason": exc.__class__.__name__},
        ) from exc
    _validate_loaded_lease_state(identity, payload)
    return payload


@_bounded_operation
def validate_lease_and_claimant(
    identity: dict[str, str],
    *,
    owner_session_id: str | None = None,
    fence_token: int | None = None,
    owner_pid: int | None = None,
    claimant_runtime_id: str | None = None,
    owner_mismatch_reason: str = "lease_owned_by_other_session",
    caller_capability_verification: CallerCapabilityVerification | None = None,
) -> dict[str, Any] | None:
    lease = load_authority_lease(identity)
    if lease is None or owner_session_id is None:
        return lease

    record = owner_session_record(owner_session_id)
    if record is None:
        raise LeaseRefused("owner_session_not_registered")
    if not _session_is_live(record):
        raise LeaseRefused("owner_session_not_live", _session_not_live_owner(record))
    _require_bound_session(record, identity)
    if lease.get("status") != "active":
        raise LeaseRefused("lease_not_active", owner_summary(lease))
    if lease.get("owner_session_id") != owner_session_id:
        raise LeaseRefused(owner_mismatch_reason, owner_summary(lease))
    _assert_claimant_matches(
        lease,
        claimant_runtime_id=claimant_runtime_id,
        owner_pid=owner_pid,
    )
    if fence_token is not None and int(lease.get("fence_token", -1)) != int(fence_token):
        raise LeaseRefused("stale_fence_token", owner_summary(lease))
    if _active_lease_expired_or_corrupt(lease, lease_path(identity)):
        raise LeaseRefused("lease_expired", owner_summary(lease))
    if caller_capability_verification is not None:
        binding = lease.get("caller_capability_binding")
        if binding is None:
            raise LeaseRefused("caller_capability_binding_required", owner_summary(lease))
        try:
            validate_caller_capability_binding(
                identity,
                fence_token=int(lease["fence_token"]),
                binding=binding,
                verification=caller_capability_verification,
            )
        except LeaseRefused as exc:
            raise LeaseRefused(exc.reason, owner_summary(lease)) from exc
    return lease


@_bounded_operation
def iter_leases(identity_or_project: object) -> list[dict[str, Any]]:
    """Every lease for ONE project. Enumeration is scoped to that project's directory
    so alias detection can never cross projects."""
    directory = lease_dir(identity_or_project)
    leases: list[dict[str, Any]] = []
    for path in _scan_lease_records(directory):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise LeaseRefused(
                "corrupt_lease_state",
                {"lease_file": path.name, "field": "json", "reason": exc.__class__.__name__},
            ) from exc
        _validate_active_lease_state(path, payload, expected_lease_key=path.stem)
        leases.append(payload)
    return leases


@_bounded_operation
def save_lease(payload: dict[str, Any]) -> None:
    binding = payload.get("caller_capability_binding")
    if binding is not None:
        _validate_stored_capability_binding(
            lease_path(payload["identity"]),
            binding,
            expected_lease_key=payload.get("lease_key", lease_key(payload["identity"])),
            expected_fence=payload.get("fence_token"),
        )
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


def _claim_grant_lock(identity_or_project: object) -> _BlockingLock:
    directory = lease_dir(identity_or_project)
    directory.mkdir(parents=True, exist_ok=True)
    return _BlockingLock(directory / _GRANT_LOCK_NAME)


BB_THREAD_ID_ENV_VAR = "BB_THREAD_ID"
RUNTIME_ID_ENV_VARS = (
    BB_THREAD_ID_ENV_VAR,
    "LLM_COLLAB_READER_RUNTIME_ID",
    "CODEX_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "GEMINI_SESSION_ID",
)
BB_THREAD_ID_PATTERN = re.compile(r"thr_[a-z0-9]+")

# The reader's native id IS the worker's ordinary native, so native IDENTITY is
# (family, id) — the reader must carry its ACTUAL family, never a synthetic label.
# The generic watcher export pairs the id with LLM_COLLAB_READER_RUNTIME_FAMILY;
# the family-specific id vars imply their family directly.
RUNTIME_FAMILY_ENV_VAR = "LLM_COLLAB_READER_RUNTIME_FAMILY"
RUNTIME_ID_ENV_FAMILY = {
    "CODEX_SESSION_ID": "codex_app",
    "CLAUDE_CODE_SESSION_ID": "claude_app",
    "GEMINI_SESSION_ID": "gemini_cli",
}


def runtime_id_from_env(*, include_bb_thread: bool = False) -> str | None:
    # A BB thread can own watcher coverage without becoming an activation
    # family. Presence is authoritative: never fall through an invalid or
    # ambiguous native value into a legacy identity from the host environment.
    native = os.environ.get(BB_THREAD_ID_ENV_VAR)
    if native is not None:
        if not include_bb_thread:
            return None
        if native != native.strip() or BB_THREAD_ID_PATTERN.fullmatch(native) is None:
            return None
        if any(os.environ.get(name, "").strip() for name in RUNTIME_ID_ENV_VARS[1:]):
            return None
        return native
    for name in RUNTIME_ID_ENV_VARS[1:]:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def runtime_family_from_env() -> str | None:
    if BB_THREAD_ID_ENV_VAR in os.environ:
        return None
    explicit = os.environ.get(RUNTIME_FAMILY_ENV_VAR)
    if explicit and explicit.strip():
        return explicit.strip()
    for name, family in RUNTIME_ID_ENV_FAMILY.items():
        value = os.environ.get(name)
        if value and value.strip():
            return family
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
    identity = lease.get("identity")
    if not isinstance(identity, dict) or not _session_matches_identity(record, identity):
        return False
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


def _active_lease_expired_or_corrupt(lease: dict[str, Any], path: Path) -> bool:
    problem = _field_problem(lease, "lease_expires_utc")
    if problem is not None:
        raise _malformed_lease_state(path, "lease_expires_utc", problem)
    expires_at = parse_iso8601(lease["lease_expires_utc"])
    if expires_at is None:
        raise _malformed_lease_state(path, "lease_expires_utc", "malformed")
    return expires_at <= _now()


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
    if pid is not None and pid_live is not True:
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
        if process_alive(pid) is not True:
            raise LeaseRefused("owner_pid_not_live", owner_summary(lease))


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


def _session_matches_identity(record: dict[str, Any], identity: dict[str, str]) -> bool:
    try:
        return (
            record.get("agent_id") == identity["target_agent"]
            and record.get("project_id") == identity["project"]
            and record.get("chat_id") == identity["chat"]
        )
    except KeyError:
        return False


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
    for existing in iter_leases(identity):
        if existing.get("lease_key") == this_key:
            continue
        if existing.get("status") != "active":
            continue
        if lease_is_expired(existing):
            continue
        if existing.get("worktree_realpath") != worktree_realpath:
            continue
        return existing
    return None


@_bounded_operation
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

    with _claim_grant_lock(identity), _ClaimLock(identity):
        worktree_realpath = _claim_realpath(identity)
        collision = _active_alias_collision(identity, worktree_realpath)
        if collision is not None:
            raise LeaseRefused("worktree_alias_collision", owner_summary(collision))

        existing = validate_lease_and_claimant(identity)
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
    caller_capability_verification: CallerCapabilityVerification | None = None,
) -> dict[str, Any]:
    lease = validate_lease_and_claimant(
        identity,
        owner_session_id=owner_session_id,
        fence_token=fence_token,
        owner_pid=owner_pid,
        claimant_runtime_id=claimant_runtime_id,
        caller_capability_verification=caller_capability_verification,
    )
    if lease is None:
        raise LeaseRefused("no_lease_for_identity")
    return lease


@_bounded_operation
def with_lease_fence(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    fence_token: int,
    mutation: Callable[[], T],
    owner_pid: int | None = None,
    claimant_runtime_id: str | None = None,
    caller_capability_verification: CallerCapabilityVerification | None = None,
) -> T:
    """Hold the per-identity lease lock through one protected mutation.

    The @_bounded_operation is essential here, not just at the nested validation: the
    pre-lock _ClaimLock resolves lease_dir (a legacy scan) before validate_lease_and_claimant
    would start its own budget, so without a shared operation budget one protected mutation
    could consume two separate full scan budgets.
    """
    with _ClaimLock(identity):
        lease = validate_lease_and_claimant(
            identity,
            owner_session_id=owner_session_id,
            fence_token=fence_token,
            owner_pid=owner_pid,
            claimant_runtime_id=claimant_runtime_id,
            caller_capability_verification=caller_capability_verification,
        )
        if lease is None:
            raise LeaseRefused("no_lease_for_identity")
        return mutation()


@_bounded_operation
def release_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    fence_token: int,
    owner_pid: int | None = None,
    claimant_runtime_id: str | None = None,
    status: str = "released",
    caller_capability_verification: CallerCapabilityVerification | None = None,
) -> dict[str, Any]:
    with _ClaimLock(identity):
        existing = validate_lease_and_claimant(
            identity,
            owner_session_id=owner_session_id,
            fence_token=fence_token,
            owner_pid=owner_pid,
            claimant_runtime_id=claimant_runtime_id,
            owner_mismatch_reason="release_requires_current_owner",
            caller_capability_verification=caller_capability_verification,
        )
        if existing is None:
            raise LeaseRefused("no_lease_for_identity")
        existing["status"] = status
        existing["released_utc"] = utc_iso()
        save_lease(existing)
        return existing
