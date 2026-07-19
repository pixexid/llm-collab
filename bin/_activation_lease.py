"""Fenced one-writer activation leases for the session autobridge seam.

One durable activation packet must never produce two writers for the same
exact activation identity. A lease record is keyed by
(project, chat, task, worktree, branch, target_agent); claims are serialized
through an O_EXCL lock file and fenced with a monotonically increasing token
so a stale owner can never regain write authority after a takeover.

Cleanup is registration-based: PM2-managed watchers (the registry) are always
preserved; only unregistered ad-hoc mailbox pollers matching the activation
identity are terminated, and every candidate is reported with its identity and
the exact action taken.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

from _helpers import now_utc, utc_iso
from _session_autobridge import AUTOBRIDGE_ROOT, parse_iso8601

ACTIVATION_LEASES_DIR = AUTOBRIDGE_ROOT / "activation_leases"

IDENTITY_FIELDS = ("project", "chat", "task", "worktree", "branch", "target_agent")

# Env markers PM2 injects into managed processes; their presence in a
# `ps eww` command row marks the process as registry-owned.
PM2_ENV_MARKERS = ("PM2_HOME=", "pm2_env=", "PM2_USAGE=")


class LeaseRefused(Exception):
    def __init__(self, reason: str, owner: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.owner = owner or {}


def lease_identity(args_or_mapping: Any) -> dict[str, str]:
    identity: dict[str, str] = {}
    for field in IDENTITY_FIELDS:
        value = (
            args_or_mapping.get(field)
            if isinstance(args_or_mapping, dict)
            else getattr(args_or_mapping, field, None)
        )
        if not value:
            raise ValueError(f"activation lease identity requires --{field.replace('_', '-')}")
        identity[field] = str(value)
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


def save_lease(payload: dict[str, Any]) -> None:
    path = lease_path(payload["identity"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_utc"] = utc_iso()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def owner_process_alive(pid: int | None) -> bool | None:
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


def lease_is_expired(lease: dict[str, Any]) -> bool:
    expires_at = parse_iso8601(lease.get("lease_expires_utc"))
    if expires_at is None:
        return False
    return expires_at <= now_utc()


class _ClaimLock:
    """Serialize claims per identity via an O_EXCL lock file."""

    def __init__(self, identity: dict[str, str]):
        self.path = lease_path(identity).with_suffix(".lock")

    def __enter__(self) -> "_ClaimLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise LeaseRefused("claim_in_progress") from exc
            raise
        os.close(fd)
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def owner_summary(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        "lease_key": lease.get("lease_key"),
        "owner_session_id": lease.get("owner_session_id"),
        "owner_pid": lease.get("owner_pid"),
        "status": lease.get("status"),
        "fence_token": lease.get("fence_token"),
        "lease_expires_utc": lease.get("lease_expires_utc"),
        "claimed_utc": lease.get("claimed_utc"),
    }


def claim_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    owner_pid: int | None = None,
    ttl_seconds: int = 3600,
    takeover: bool = False,
) -> dict[str, Any]:
    """Claim the activation lease or raise LeaseRefused naming the live owner.

    Fencing rules, all fail closed:
    - active + unexpired lease held by another session -> refused
    - expired lease without --takeover -> refused
    - expired lease with --takeover but owner process still alive -> refused
    - released/superseded lease, or takeover of a dead/expired owner ->
      claimed with an incremented fence token, so any writer still holding the
      old token can be rejected by downstream consumers.
    """
    with _ClaimLock(identity):
        existing = load_lease(identity)
        fence_token = 1
        if existing is not None:
            fence_token = int(existing.get("fence_token", 0)) + 1
            status = existing.get("status")
            if status == "active":
                same_owner = existing.get("owner_session_id") == owner_session_id
                if same_owner:
                    fence_token = int(existing.get("fence_token", 1))
                elif not lease_is_expired(existing):
                    raise LeaseRefused("lease_held_by_active_owner", owner_summary(existing))
                elif not takeover:
                    raise LeaseRefused("lease_expired_requires_takeover", owner_summary(existing))
                else:
                    alive = owner_process_alive(existing.get("owner_pid"))
                    if alive is True:
                        raise LeaseRefused("expired_owner_still_active", owner_summary(existing))
                    if alive is None and existing.get("owner_pid") is not None:
                        raise LeaseRefused("owner_liveness_unknown", owner_summary(existing))

        now = now_utc()
        expires = __import__("datetime").datetime.fromtimestamp(
            now.timestamp() + ttl_seconds, tz=now.tzinfo
        ).isoformat(timespec="seconds")
        payload = {
            "identity": identity,
            "lease_key": lease_key(identity),
            "owner_session_id": owner_session_id,
            "owner_pid": owner_pid,
            "status": "active",
            "fence_token": fence_token,
            "claimed_utc": utc_iso(),
            "lease_expires_utc": expires,
            "previous_owner_session_id": (existing or {}).get("owner_session_id")
            if existing and existing.get("owner_session_id") != owner_session_id
            else (existing or {}).get("previous_owner_session_id"),
        }
        save_lease(payload)
        return payload


def release_lease(
    identity: dict[str, str],
    *,
    owner_session_id: str,
    status: str = "released",
) -> dict[str, Any]:
    with _ClaimLock(identity):
        existing = load_lease(identity)
        if existing is None:
            raise LeaseRefused("no_lease_for_identity")
        if existing.get("owner_session_id") != owner_session_id:
            raise LeaseRefused("release_requires_current_owner", owner_summary(existing))
        existing["status"] = status
        existing["released_utc"] = utc_iso()
        save_lease(existing)
        return existing


# ---------------------------------------------------------------------------
# Stale activation-poller audit/cleanup
# ---------------------------------------------------------------------------


def poller_process_rows(ps_output: str | None = None) -> list[dict[str, Any]]:
    if ps_output is None:
        result = subprocess.run(
            ["ps", "eww", "-axo", "pid=,ppid=,command="],
            text=True,
            capture_output=True,
            check=False,
        )
        ps_output = result.stdout
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


def is_registered_watch(command: str) -> bool:
    return any(marker in command for marker in PM2_ENV_MARKERS)


POLLER_SHAPE_MARKERS = ("while true", "watch_inbox.py", "inbox.py")


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


def audit_activation_pollers(
    identity: dict[str, str],
    *,
    rows: list[dict[str, Any]] | None = None,
    clean: bool = False,
    terminate=os.kill,
    self_pid: int | None = None,
) -> list[dict[str, Any]]:
    """List every poller-shaped process for this identity with the action taken.

    Registered (PM2-managed) watches are always preserved. Unregistered
    matches are terminated only when clean=True. Nothing is ever killed by
    bare process name — only identity-matched, unregistered rows.
    """
    if rows is None:
        rows = poller_process_rows()
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
            "registered": is_registered_watch(row["command"]),
        }
        if finding["registered"]:
            finding["action"] = "preserved_registered_watch"
        elif not clean:
            finding["action"] = "reported_only"
        else:
            try:
                terminate(row["pid"], signal.SIGTERM)
                finding["action"] = "terminated"
            except ProcessLookupError:
                finding["action"] = "already_exited"
            except PermissionError:
                finding["action"] = "terminate_denied"
        findings.append(finding)
    return findings
