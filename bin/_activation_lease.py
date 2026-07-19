"""Fenced one-writer activation leases on the session_autobridge seam.

One durable activation packet must never produce two writers for the same
exact activation identity. A lease record is keyed by the canonicalized
(project, chat, task, worktree, branch, target_agent) identity; claims are
serialized through a POSIX advisory flock held on a stable, never-unlinked
lock file (crash of the holder releases it via kernel fd close) and fenced
with a monotonically
increasing token.

This is an extension of the EXISTING sessions/ lease seam, not a parallel
authority: a lease owner IS a registered autobridge session. Owner liveness
derives from that session record's status (plus an optional recorded owner
pid), `deactivate` releases the sessions' activation leases, and
`dispatch_session` refuses to wake a writer for an activation-shaped packet
without holding the lease.

Cleanup is registry-based and fail closed: PM2's process list (`pm2 jlist`)
is the authoritative registry of purpose watches to preserve; identity-matched
unregistered pollers must be provably terminated or the claim is refused.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from _helpers import now_utc, utc_iso
from _session_autobridge import AUTOBRIDGE_ROOT, load_session, parse_iso8601

ACTIVATION_LEASES_DIR = AUTOBRIDGE_ROOT / "activation_leases"

IDENTITY_FIELDS = ("project", "chat", "task", "worktree", "branch", "target_agent")

LIVE_SESSION_STATUSES = {"active", "parked"}

# Bare inbox.py is deliberately NOT a marker: a one-shot reader (the emitted
# activation claim command itself mentions the chat id via its --packet path)
# must never be a cleanup target. Recurring shape means an explicit loop or
# the persistent watcher; a `while true; do inbox.py ...` wrapper still
# matches via its loop marker.
POLLER_SHAPE_MARKERS = ("while true", "watch_inbox.py")


class LeaseRefused(Exception):
    def __init__(self, reason: str, owner: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.owner = owner or {}


class PollerAuditUnavailable(Exception):
    """The stale-poller audit could not run or could not prove cleanup."""


def canonical_worktree(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def lease_identity(args_or_mapping: Any) -> dict[str, str]:
    identity: dict[str, str] = {}
    for field in IDENTITY_FIELDS:
        value = (
            args_or_mapping.get(field)
            if isinstance(args_or_mapping, dict)
            else getattr(args_or_mapping, field, None)
        )
        if value is not None:
            value = str(value).strip()
        if not value:
            raise ValueError(f"activation lease identity requires --{field.replace('_', '-')}")
        identity[field] = value
    identity["worktree"] = canonical_worktree(identity["worktree"])
    return identity


def lease_key(identity: dict[str, str]) -> str:
    canonical = "\x1f".join(identity[field] for field in IDENTITY_FIELDS)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def lease_path(identity: dict[str, str]) -> Path:
    return ACTIVATION_LEASES_DIR / f"{lease_key(identity)}.json"


def load_lease(identity: dict[str, str]) -> dict[str, Any] | None:
    path = lease_path(identity)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def iter_leases() -> list[dict[str, Any]]:
    if not ACTIVATION_LEASES_DIR.exists():
        return []
    leases: list[dict[str, Any]] = []
    for path in sorted(ACTIVATION_LEASES_DIR.glob("*.json")):
        try:
            leases.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    return leases


def save_lease(payload: dict[str, Any]) -> None:
    path = lease_path(payload["identity"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_utc"] = utc_iso()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def process_alive(pid: int | None) -> bool | None:
    """True/False when determinable; None when liveness is unknown."""
    if pid is None:
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


def owner_is_live(lease: dict[str, Any]) -> bool | None:
    """Owner liveness from the authoritative sessions/ record, with the
    recorded owner pid as an additional positive proof.

    True  -> provably live (refuse takeover)
    False -> provably gone (takeover-eligible)
    None  -> unknown (fail closed: refuse takeover)
    """
    pid_alive = process_alive(lease.get("owner_pid"))
    if pid_alive is True:
        return True

    record = owner_session_record(str(lease.get("owner_session_id")))
    if record is None:
        return None if pid_alive is None else False
    if record.get("status") not in LIVE_SESSION_STATUSES:
        return False
    if record.get("ephemeral_reader"):
        # An auto-created mailbox reader is only as alive as its bound
        # process; a crashed reader must not block takeover forever, and its
        # session record expires as a backstop.
        if pid_alive is False:
            return False
        expires_at = parse_iso8601(record.get("lease_expires_utc"))
        if expires_at is not None and expires_at <= now_utc():
            return False
        return True
    # Regular sessions: the record governs until its own expiry. A provably
    # live pid already returned True above; without one, an expired
    # active/parked record must not block takeover forever — dispatch already
    # refuses to wake it (session_is_dispatchable), so treat it as gone.
    expires_at = parse_iso8601(record.get("lease_expires_utc"))
    if expires_at is not None and expires_at <= now_utc():
        return False
    return True


def lease_is_expired(lease: dict[str, Any]) -> bool:
    expires_at = parse_iso8601(lease.get("lease_expires_utc"))
    if expires_at is None:
        return False
    return expires_at <= now_utc()


class _ClaimLock:
    """Serialize claims per identity via a POSIX advisory lock.

    The lock is `flock(LOCK_EX | LOCK_NB)` on a stable, never-unlinked
    `.lock` file held for the whole claim critical section. The kernel
    releases it when the holder's fd closes — including process crash/kill —
    so a dead claimant can never block the identity and there is no
    unlink/path-replacement race. Contention maps to
    LeaseRefused("claim_in_progress")."""

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
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                # Platform contention errnos only: another live claimant
                # holds the lock. Permission/filesystem/I/O failures are NOT
                # contention and must surface as themselves.
                raise LeaseRefused("claim_in_progress") from exc
            raise
        self.fd = fd
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
            self.fd = None


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


def _owner_runtime_session_id(record: dict[str, Any]) -> str | None:
    runtime = record.get("runtime")
    if isinstance(runtime, dict) and runtime.get("session_id"):
        return str(runtime["session_id"])
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
    """Claim the activation lease or raise LeaseRefused naming the live owner.

    The owner MUST be a registered autobridge session in a live status; lease
    ownership and liveness are bound to that sessions/ record (plus the
    optional recorded pid). Fencing rules, all fail closed:

    - owner provably live and different session -> refused
    - same session but the runtime identity or recorded pid changed while the
      previous owner process is still live -> refused (a second process reusing
      --session cannot bypass ownership)
    - owner liveness unknown -> refused
    - owner provably gone -> refused without explicit --takeover; with it, the
      claim succeeds and increments fence_token so the stale owner's token is
      detectably old.
    """
    record = owner_session_record(owner_session_id)
    if record is None:
        raise LeaseRefused("owner_session_not_registered")
    if record.get("status") not in LIVE_SESSION_STATUSES:
        raise LeaseRefused(
            "owner_session_not_live", {"owner_session_status": record.get("status")}
        )
    # The claimant session must be BOUND to the requested activation identity:
    # exact agent, project, and chat. Activation identities carry concrete
    # values, so a null session field is unbound — not a wildcard — and
    # refuses like any other mismatch.
    for record_field, identity_field in (
        ("agent_id", "target_agent"),
        ("project_id", "project"),
        ("chat_id", "chat"),
    ):
        session_value = record.get(record_field)
        if session_value != identity[identity_field]:
            raise LeaseRefused(
                "owner_session_identity_mismatch",
                {"field": record_field, "session_value": session_value,
                 "identity_value": identity[identity_field]},
            )
    # The claimant's own identity wins over the (shared) session record: a
    # second process reusing the session id must not inherit the recorded
    # runtime identity.
    claim_runtime_id = claimant_runtime_id or _owner_runtime_session_id(record)

    with _ClaimLock(identity):
        existing = load_lease(identity)
        fence_token = 1
        previous_owner: str | None = None
        if existing is not None:
            fence_token = int(existing.get("fence_token", 0)) + 1
            previous_owner = existing.get("previous_owner_session_id")
            if existing.get("status") == "active":
                same_session = existing.get("owner_session_id") == owner_session_id
                same_runtime = (
                    existing.get("owner_runtime_session_id") == claim_runtime_id
                )
                recorded_pid = existing.get("owner_pid")
                same_pid = (
                    owner_pid is not None
                    and recorded_pid is not None
                    and int(recorded_pid) == int(owner_pid)
                )
                # Idempotent reclaim requires the same session AND runtime
                # identity, and — when a process was bound — the SAME live
                # process or a provably dead predecessor. Two concurrent live
                # dispatcher processes sharing one session must never both
                # hold the claim.
                if same_session and same_runtime and (
                    recorded_pid is None
                    or same_pid
                    or process_alive(recorded_pid) is False
                ):
                    fence_token = int(existing.get("fence_token", 1))
                else:
                    alive = owner_is_live(existing)
                    if alive is True:
                        reason = (
                            "same_session_different_process"
                            if same_session
                            else "lease_held_by_active_owner"
                        )
                        raise LeaseRefused(reason, owner_summary(existing))
                    if alive is None:
                        raise LeaseRefused("owner_liveness_unknown", owner_summary(existing))
                    if not takeover:
                        reason = (
                            "lease_expired_requires_takeover"
                            if lease_is_expired(existing)
                            else "dead_owner_requires_takeover"
                        )
                        raise LeaseRefused(reason, owner_summary(existing))
                    previous_owner = existing.get("owner_session_id")

        now = now_utc()
        expires = __import__("datetime").datetime.fromtimestamp(
            now.timestamp() + ttl_seconds, tz=now.tzinfo
        ).isoformat(timespec="seconds")
        payload = {
            "identity": identity,
            "lease_key": lease_key(identity),
            "owner_session_id": owner_session_id,
            "owner_runtime_session_id": claim_runtime_id,
            "owner_pid": owner_pid,
            "status": "active",
            "fence_token": fence_token,
            "claimed_utc": utc_iso(),
            "lease_expires_utc": expires,
            "previous_owner_session_id": previous_owner,
        }
        save_lease(payload)
        return payload


def assert_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    fence_token: int | None = None,
) -> dict[str, Any]:
    """Verify current write authority; raise LeaseRefused when absent/stale."""
    lease = load_lease(identity)
    if lease is None:
        raise LeaseRefused("no_lease_for_identity")
    if lease.get("status") != "active":
        raise LeaseRefused("lease_not_active", owner_summary(lease))
    if lease.get("owner_session_id") != owner_session_id:
        raise LeaseRefused("lease_owned_by_other_session", owner_summary(lease))
    if fence_token is not None and int(lease.get("fence_token", -1)) != int(fence_token):
        raise LeaseRefused("stale_fence_token", owner_summary(lease))
    if lease_is_expired(lease):
        # Expired authority must not keep passing the pre-mutation assertion
        # while a replacement can take over: the owner re-claims (idempotent,
        # TTL refreshed) before continuing to write.
        raise LeaseRefused("lease_expired", owner_summary(lease))
    return lease


def release_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    fence_token: int,
    status: str = "released",
) -> dict[str, Any]:
    with _ClaimLock(identity):
        existing = load_lease(identity)
        if existing is None:
            raise LeaseRefused("no_lease_for_identity")
        if existing.get("owner_session_id") != owner_session_id:
            raise LeaseRefused("release_requires_current_owner", owner_summary(existing))
        if int(existing.get("fence_token", -1)) != int(fence_token):
            raise LeaseRefused("stale_fence_token", owner_summary(existing))
        existing["status"] = status
        existing["released_utc"] = utc_iso()
        save_lease(existing)
        return existing


def release_session_leases(owner_session_id: str) -> list[dict[str, Any]]:
    """Release every active lease owned by a session (deactivate integration).

    Each release retakes the per-identity claim lock and re-reads the record
    so a concurrent claim/takeover can never be overwritten with a stale
    released payload.
    """
    released: list[dict[str, Any]] = []
    for stale_view in iter_leases():
        if stale_view.get("owner_session_id") != owner_session_id:
            continue
        if stale_view.get("status") != "active":
            continue
        identity = stale_view.get("identity")
        if not isinstance(identity, dict):
            continue
        try:
            with _ClaimLock(identity):
                current = load_lease(identity)
                if current is None:
                    continue
                if current.get("owner_session_id") != owner_session_id:
                    continue
                if current.get("status") != "active":
                    continue
                current["status"] = "released"
                current["released_utc"] = utc_iso()
                save_lease(current)
                released.append(owner_summary(current))
        except LeaseRefused:
            continue
    return released


# ---------------------------------------------------------------------------
# Stale activation-poller audit/cleanup (fail closed)
# ---------------------------------------------------------------------------


def ps_fixture_path() -> Path | None:
    """Test-isolation seam: when LLM_COLLAB_PS_FIXTURE is set, process rows
    come from that file and NO real signal is ever sent — cleanup is
    simulated. Live activation must never set it."""
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
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "command": parts[2].strip()})
    return rows


def pm2_registered_pids() -> set[int]:
    """Authoritative preservation registry: pids managed by PM2.

    No PM2 binary on the host means no registry exists (empty set). A present
    but failing PM2 is indistinguishable from a hidden registry, so it fails
    the audit closed.
    """
    from shutil import which

    pm2_bin = os.environ.get("LLM_COLLAB_PM2_BIN") or which("pm2")
    if not pm2_bin:
        return set()
    try:
        result = subprocess.run(
            [pm2_bin, "jlist"], text=True, capture_output=True, check=False, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PollerAuditUnavailable(f"pm2 jlist unavailable: {exc}") from exc
    if result.returncode != 0:
        raise PollerAuditUnavailable(f"pm2 jlist failed (rc={result.returncode})")
    try:
        processes = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PollerAuditUnavailable("pm2 jlist emitted invalid JSON") from exc
    pids: set[int] = set()
    for proc in processes if isinstance(processes, list) else []:
        pid = proc.get("pid") if isinstance(proc, dict) else None
        if isinstance(pid, int) and pid > 0:
            pids.add(pid)
    return pids


def matches_activation_identity(command: str, identity: dict[str, str]) -> str | None:
    """Return the matched marker when the process is a mailbox POLLER for this
    activation identity, else None.

    Only poller-shaped commands (recurring loops or inbox watchers) are ever
    matched. A one-shot command that merely mentions the chat id — a deliver,
    an editor, this very lease-claim invocation — is never a cleanup target.
    """
    if not any(marker in command for marker in POLLER_SHAPE_MARKERS):
        return None
    if identity["chat"] in command:
        return f"chat:{identity['chat']}"
    if "watch_inbox.py" in command and re.search(
        rf"--me[=\s]+{re.escape(identity['target_agent'])}(\s|$)", command
    ):
        return f"watch_inbox:{identity['target_agent']}"
    return None


def ancestor_pids(rows: list[dict[str, Any]], start_pid: int) -> set[int]:
    """The pid set of start_pid and its ancestors as far as the rows describe."""
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
    """SIGTERM, verify exit, escalate to SIGKILL, verify again.

    Returns the proven action label; `termination_unverified` means the
    process is still (or possibly still) alive and the caller must fail
    closed.
    """
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


# reported_only is unproven by definition: an identity-matched unregistered
# poller that was observed but not terminated is still a live duplicate-wake
# source, so a claim must never be granted over it.
UNPROVEN_ACTIONS = {"terminate_denied", "termination_unverified", "reported_only"}


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
    """List every poller-shaped process for this identity with the action taken.

    PM2-registered pids (the authoritative registry) are always preserved.
    Unregistered matches are terminated (with verified exit) only when
    clean=True. Nothing is ever killed by bare process name — only
    identity-matched, unregistered rows outside the caller's own ancestor
    chain. Raises PollerAuditUnavailable when ps or the registry cannot be
    consulted.
    """
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
        if matched is None:
            continue
        if row["pid"] in excluded:
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
            # Fixture rows describe processes that do not exist on this host;
            # signaling their pids could hit unrelated real processes. Never
            # signal outside the real process table.
            finding["action"] = "terminated"
            finding["simulated"] = True
        else:
            finding["action"] = terminate_verified(
                row["pid"], kill=kill, wait_for_exit=wait_for_exit
            )
        findings.append(finding)
    return findings


def audit_proves_clean(findings: list[dict[str, Any]]) -> bool:
    return not any(f["action"] in UNPROVEN_ACTIONS for f in findings)


def gated_claim(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    owner_pid: int | None = None,
    claimant_runtime_id: str | None = None,
    ttl_seconds: int = 3600,
    takeover: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """Audit-first, fail-closed claim used by every activation boundary
    (mailbox read, autobridge dispatch). Returns (authorized, detail).

    takeover=True here still only ever replaces a PROVABLY dead owner —
    a live or unknown-liveness owner always refuses."""
    try:
        pollers = audit_activation_pollers(identity, clean=True)
    except PollerAuditUnavailable as exc:
        return False, {"reason": "poller_audit_unavailable", "detail": str(exc)}
    if not audit_proves_clean(pollers):
        return False, {"reason": "stale_poller_not_proven_gone", "poller_audit": pollers}
    try:
        lease = claim_lease(
            identity,
            owner_session_id=owner_session_id,
            owner_pid=owner_pid,
            claimant_runtime_id=claimant_runtime_id,
            ttl_seconds=ttl_seconds,
            takeover=takeover,
        )
    except LeaseRefused as refusal:
        return False, {
            "reason": refusal.reason,
            "owner": refusal.owner,
            "poller_audit": pollers,
        }
    return True, {
        "lease": owner_summary(lease),
        "identity": identity,
        "fence_token": lease.get("fence_token"),
        "poller_audit": pollers,
    }
