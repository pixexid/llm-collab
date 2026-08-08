from __future__ import annotations

import contextlib
import fcntl
import threading
import json
import importlib
import os
import stat
import tempfile
import re
import shlex
import signal
import base64
import hashlib
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROUTE_AMBIGUOUS_REASON = "route_ambiguous"
STALE_GENERATION_REASON = "stale_generation"
EXACT_BINDING_REQUIRED_REASON = "exact_binding_required"
EXACT_BINDING_NOT_DISPATCHABLE_REASON = "exact_binding_not_dispatchable"
EXACT_BINDING_AMBIGUOUS_REASON = "exact_binding_ambiguous"
EXACT_BINDING_MISMATCH_REASON = "exact_binding_mismatch"
HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON = "heuristic_runtime_discovery_refused"
HEURISTIC_RUNTIME_DISCOVERY_FAMILIES = frozenset(
    {"codex_app", "claude_app", "gemini_cli"}
)

from _helpers import (
    ROOT,
    STATE_ROOT,
    agent_inbox_path,
    build_handoff_prompt,
    build_packet_ring_prompt,
    config_get,
    get_agent,
    get_project,
    now_utc,
    parse_frontmatter,
    project_state_root,
    utc_iso,
    write_file,
    write_chat_note,
)
from _activation_identity import classify_activation

AUTOBRIDGE_ROOT = STATE_ROOT / "State" / "session_autobridge"
SESSIONS_DIR = AUTOBRIDGE_ROOT / "sessions"
EVENTS_DIR = AUTOBRIDGE_ROOT / "events"
PROMPTS_DIR = AUTOBRIDGE_ROOT / "prompts"
BINDINGS_DIR = AUTOBRIDGE_ROOT / "bindings"
THREAD_PAIRS_DIR = AUTOBRIDGE_ROOT / "thread_pairs"

SESSION_MODES = ("manual", "notify", "auto-read", "auto-reply")
SESSION_STATUSES = ("active", "parked", "stopping", "stopped", "superseded")
WAKE_STRATEGIES = ("none", "notify", "relay", "runtime_trigger")
MAX_SESSION_BYTES = 256 * 1024
MAX_SESSION_INBOX_BYTES = 16 * 1024 * 1024
MAX_SCANNED_SESSIONS = 5_000
MAX_SESSION_SCAN_BYTES = 16 * 1024 * 1024
MAX_DISPATCH_INBOX_ENTRIES = 5_000
MAX_DISPATCH_INBOX_BYTES = 16 * 1024 * 1024
MAX_DISPATCH_PACKET_BYTES = 256 * 1024
MAX_EVENT_LOG_BYTES = 1024 * 1024
EVENT_LOG_TRUNCATION_RESERVE_BYTES = 256


def bb_bootstrap_enabled(config_reader=None) -> bool:
    """Global kill switch; keep the adapter unreachable unless explicitly on."""
    try:
        return (config_reader or config_get)("bb_bootstrap_enabled", False) is True
    except SystemExit:
        return False


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def autobridge_session_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def autobridge_event_log_path(session_id: str) -> Path:
    return EVENTS_DIR / f"{session_id}.jsonl"


def autobridge_wake_log_path(session_id: str) -> Path:
    """Return the Pi-only wake stream; diagnostics must not wake a worker."""
    return EVENTS_DIR / "wake" / f"{session_id}.jsonl"


def autobridge_prompt_dir(session_id: str) -> Path:
    return PROMPTS_DIR / session_id


def autobridge_binding_path(project_id: str, chat_id: str, agent_id: str) -> Path:
    safe_project = project_id.replace("/", "_")
    safe_chat = chat_id.replace("/", "_")
    safe_agent = agent_id.replace("/", "_")
    return BINDINGS_DIR / safe_project / safe_chat / f"{safe_agent}.json"


def autobridge_thread_pair_path(project_id: str, chat_id: str, agent_a: str, agent_b: str) -> Path:
    safe_project = project_id.replace("/", "_")
    safe_chat = chat_id.replace("/", "_")
    left, right = sorted((agent_a.replace("/", "_"), agent_b.replace("/", "_")))
    return THREAD_PAIRS_DIR / safe_project / safe_chat / f"{left}__{right}.json"


def load_session(session_id: str) -> dict:
    path = autobridge_session_path(session_id)
    try:
        raw = read_regular_file_bounded(path, MAX_SESSION_BYTES)
    except FileNotFoundError:
        raise FileNotFoundError(f"Unknown session: {session_id}")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Malformed session: {session_id}")
    return payload


def iter_sessions(agent_id: str | None = None, *, strict: bool = False) -> list[dict]:
    try:
        scan = os.scandir(SESSIONS_DIR)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise UnreadableFile(f"cannot scan session records: {error}") from error

    paths = []
    with scan:
        for count, entry in enumerate(scan, start=1):
            if count > MAX_SCANNED_SESSIONS:
                raise UnreadableFile(
                    f"session records exceed the {MAX_SCANNED_SESSIONS} entry limit"
                )
            if entry.name.endswith(".json"):
                paths.append(Path(entry.path))

    sessions: list[dict] = []
    spent = 0
    for path in sorted(paths):
        try:
            raw = read_regular_file_bounded(
                path,
                min(MAX_SESSION_BYTES, MAX_SESSION_SCAN_BYTES - spent),
            )
            spent += len(raw)
            session = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as error:
            # Default: skip a corrupt record (a best-effort listing). strict:
            # fail closed — an authority scan (e.g. the GH-468 native-session
            # ownership guard) must not treat an unreadable record as absent and
            # let a duplicate active owner through.
            if strict:
                raise ValueError(f"malformed session record {path.stem}: {error}") from error
            continue
        if not isinstance(session, dict):
            raise ValueError(f"Malformed session: {path.stem}")
        if str(session.get("session_id") or "") != path.stem:
            raise ValueError(f"Session identity does not match filename: {path.stem}")
        if agent_id is not None and session.get("agent_id") != agent_id:
            continue
        sessions.append(session)
    return sessions


# The binding is the authoritative record every exact-dispatch decision is made from, and it lives
# in an untrusted workspace tree like everything around it. read_text() parsed it whole with no
# ceiling: a valid 4,194,591-byte binding was accepted and resolved. Bounded before the parse.
MAX_BINDING_BYTES = 256 * 1024


# A distinct refusal reason. It must never be exact_binding_required: that means "there is no such
# binding", and an oversized or permission-denied binding is a record that EXISTS and was refused.
BINDING_UNREADABLE_REASON = "binding_unreadable"


class BindingUnreadable(RuntimeError):
    """An oversized or I/O-failed binding.

    Deliberately NOT a FileNotFoundError. Callers catch that to mean "there is no such binding" and
    turn it into exact_binding_required or "no live session" -- so collapsing an oversized or
    permission-denied binding into it would report a present-but-unreadable record as an absent one,
    hiding the real fault. RuntimeError is outside every (FileNotFoundError, OSError, ValueError)
    tuple in this codebase, so it propagates instead of being absorbed.
    """


class CanonicalBindingNativeMismatch(RuntimeError):
    """An active canonical binding exists for the participant, but it is bound to a
    different native session than the one registering.

    Distinct from "no binding" (canonical_binding_required): here a binding IS
    present, so stamping it onto the registering runtime session would wake the
    wrong native session behind a fence that then reads as proven. Register must
    fail closed with its own reason.
    """

    def __init__(self, *, canonical_native_session_id, requested_runtime_session_id):
        self.canonical_native_session_id = canonical_native_session_id
        self.requested_runtime_session_id = requested_runtime_session_id
        super().__init__(
            f"canonical binding is bound to native session "
            f"{canonical_native_session_id!r}, not the registering runtime session "
            f"{requested_runtime_session_id!r}"
        )


def load_binding(project_id: str, chat_id: str, agent_id: str) -> dict:
    path = autobridge_binding_path(project_id, chat_id, agent_id)
    if not path.exists():
        raise FileNotFoundError(
            f"Unknown binding: project={project_id} chat={chat_id} agent={agent_id}"
        )
    # Through the shared helper like every other untrusted read. This one still used Path.open, so
    # a binding that was a FIFO would have blocked forever inside open() -- the same hazard the
    # registry and session reads were just fixed for, on the MOST authoritative record of the three.
    # Nobody reported it; it turned up because a test injection could not intercept Path.open.
    try:
        raw = read_regular_file_bounded(path, MAX_BINDING_BYTES)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Unknown binding: project={project_id} chat={chat_id} agent={agent_id}"
        )
    except UnreadableFile as exc:
        raise BindingUnreadable(str(exc)) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FileNotFoundError(
            f"Unreadable binding: project={project_id} chat={chat_id} agent={agent_id}"
        ) from exc
    if not isinstance(payload, dict):
        raise FileNotFoundError(
            f"Malformed binding: project={project_id} chat={chat_id} agent={agent_id}"
        )
    return payload


def load_thread_pair(project_id: str, chat_id: str, agent_a: str, agent_b: str) -> dict:
    path = autobridge_thread_pair_path(project_id, chat_id, agent_a, agent_b)
    if not path.exists():
        raise FileNotFoundError(
            f"Unknown thread pair: project={project_id} chat={chat_id} agents={agent_a},{agent_b}"
        )
    return json.loads(path.read_text())


def prepare_binding_write(payload: dict) -> tuple[dict, str]:
    candidate = dict(payload)
    candidate["updated_utc"] = utc_iso()
    content = json.dumps(candidate, indent=2, sort_keys=True)
    if len(content.encode("utf-8")) > MAX_BINDING_BYTES:
        raise BindingUnreadable(
            f"binding exceeds {MAX_BINDING_BYTES} bytes; refusing to write an unreadable record"
        )
    return candidate, content


def save_binding(payload: dict, prepared: tuple[dict, str] | None = None) -> None:
    path = autobridge_binding_path(
        str(payload["project_id"]),
        str(payload["chat_id"]),
        str(payload["agent_id"]),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate, content = prepared or prepare_binding_write(payload)
    candidate = dict(candidate)
    write_regular_file_atomically(path, content)
    payload.clear()
    payload.update(candidate)


def write_regular_file_atomically(path: Path, content: str) -> None:
    """Write `content` to `path` without ever blocking on it, replacing it atomically.

    path.write_text() OPENS the destination, so a binding pathname swapped for a writer-less FIFO
    blocked forever and a directory raised -- either way after the caller had already published
    other state. Reading the snapshot beforehand protects the VALUES; it does nothing for the
    write-side descriptor.

    A temporary file in the same directory plus os.replace() never opens the destination at all, so
    a swapped object cannot block the write, and the replacement is atomic: a reader sees the old
    record or the new one, never a partial one. An existing destination that is not a regular file
    is refused first, because silently replacing a FIFO or directory would hide that someone else
    is doing something unexpected with our authoritative record.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    except OSError as error:
        raise UnreadableFile(f"cannot inspect {path} before writing: {error}") from error
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise UnreadableFile(
            f"{path} exists and is not a regular file; refusing to replace it"
        )

    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory = path.parent
            while True:
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                if directory == ROOT or directory.parent == directory:
                    break
                directory = directory.parent
        except OSError as error:
            try:
                print(
                    f"[warning] {path} was replaced, but its directory did not fsync: {error}",
                    file=sys.stderr,
                )
            except Exception:
                pass
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_thread_pair(payload: dict) -> None:
    agents = payload.get("agents") or []
    if not isinstance(agents, list) or len(agents) != 2:
        raise ValueError("thread pair payload must contain exactly two agents")
    path = autobridge_thread_pair_path(
        str(payload["project_id"]),
        str(payload["chat_id"]),
        str(agents[0]),
        str(agents[1]),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_utc"] = utc_iso()
    write_file(path, json.dumps(payload, indent=2, sort_keys=True))


def resolve_thread_pair_session_id(
    project_id: str,
    chat_id: str,
    local_agent: str,
    remote_agent: str,
) -> str | None:
    try:
        pair = load_thread_pair(project_id, chat_id, local_agent, remote_agent)
    except FileNotFoundError:
        return None
    sessions = pair.get("sessions")
    if not isinstance(sessions, dict):
        return None
    session_id = sessions.get(local_agent)
    if not session_id:
        return None
    return str(session_id)


def update_thread_pair(
    project_id: str,
    chat_id: str,
    sender_agent: str,
    recipient_agent: str,
    *,
    sender_session_id: str | None = None,
    target_session_id: str | None = None,
) -> dict:
    try:
        pair = load_thread_pair(project_id, chat_id, sender_agent, recipient_agent)
    except FileNotFoundError:
        pair = {
            "project_id": project_id,
            "chat_id": chat_id,
            "agents": sorted([sender_agent, recipient_agent]),
            "sessions": {},
            "created_utc": utc_iso(),
        }

    sessions = pair.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    if sender_session_id:
        sessions[sender_agent] = str(sender_session_id)
    if target_session_id:
        sessions[recipient_agent] = str(target_session_id)
    pair["sessions"] = sessions
    pair["last_direction"] = {
        "from": sender_agent,
        "to": recipient_agent,
        "sender_session_id": str(sender_session_id) if sender_session_id else None,
        "target_session_id": str(target_session_id) if target_session_id else None,
        "observed_at_utc": utc_iso(),
    }
    save_thread_pair(pair)
    return pair


def prepare_session_write(payload: dict) -> tuple[dict, str]:
    candidate = {**payload, "updated_utc": utc_iso()}
    content = json.dumps(candidate, indent=2, sort_keys=True)
    if len(content.encode("utf-8")) > MAX_SESSION_BYTES:
        inbox_raw = read_regular_file_bounded(
            agent_inbox_path(str(candidate["agent_id"])),
            MAX_SESSION_INBOX_BYTES,
        )
        inbox = json.loads(inbox_raw.decode("utf-8"))
        if isinstance(inbox, dict) and inbox.get("_durability_pending") is True:
            raise UnreadableFile("agent inbox durability is unconfirmed")
        read = inbox.get("read") if isinstance(inbox, dict) else None
        unread = inbox.get("unread") if isinstance(inbox, dict) else None
        if (
            not isinstance(read, list)
            or not isinstance(unread, list)
            or any(not isinstance(path, str) for path in [*read, *unread])
        ):
            raise ValueError("agent inbox must contain unread and read path lists")
        if set(read) & set(unread):
            raise ValueError("agent inbox unread and read paths must not overlap")
        settled = set(read)
        processed = candidate.get("processed_messages")
        if isinstance(processed, list):
            candidate["processed_messages"] = [
                message_path
                for message_path in processed
                if message_path not in settled
            ]
        canonical = candidate.get("canonical_settled_messages")
        if isinstance(canonical, dict):
            candidate["canonical_settled_messages"] = {
                message_path: value
                for message_path, value in canonical.items()
                if message_path not in settled
            }
        content = json.dumps(candidate, indent=2, sort_keys=True)
    if len(content.encode("utf-8")) > MAX_SESSION_BYTES:
        raise ValueError(
            f"session payload exceeds the {MAX_SESSION_BYTES} byte limit"
        )
    return candidate, content


SESSION_WRITE_LOCK = AUTOBRIDGE_ROOT / ".session-write.lock"
_SESSION_WRITE_LOCK_DEPTH = threading.local()


@contextlib.contextmanager
def _session_write_lock():
    """Serialize the superseded-check and the write against other session writes.

    Cross-process serialization is one process-wide blocking flock. It is also
    REENTRANT within a process/thread (GH-468): a caller may hold it across a
    check-then-write critical section (e.g. the native-session ownership scan plus
    the register write) even though the inner save_session re-enters it. A plain
    second flock on a new fd in the same process would deadlock, so nested
    acquisitions just increment a thread-local depth and reuse the outer flock.

    ponytail: one process-wide blocking flock; per-session locks only if session
    write throughput ever matters (it is a handful of writes per dispatch here).
    """
    depth = getattr(_SESSION_WRITE_LOCK_DEPTH, "value", 0)
    if depth > 0:
        _SESSION_WRITE_LOCK_DEPTH.value = depth + 1
        try:
            yield
        finally:
            _SESSION_WRITE_LOCK_DEPTH.value -= 1
        return
    SESSION_WRITE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SESSION_WRITE_LOCK, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _SESSION_WRITE_LOCK_DEPTH.value = 1
        yield
    finally:
        _SESSION_WRITE_LOCK_DEPTH.value = 0
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save_session(
    payload: dict,
    prepared: tuple[dict, str] | None = None,
    *,
    allow_reactivation: bool = False,
) -> None:
    session_id = str(payload["session_id"])
    path = autobridge_session_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate, content = prepared or prepare_session_write(payload)
    # A stopped or superseded record is terminal except through explicit registration.
    # A dispatch that loaded the session before it was retired still holds a live
    # in-memory copy, and its processed-state save would otherwise write that copy
    # back — resurrecting the old session as active.
    # The check and the write must be one critical section: a bare check-then-write
    # lets a retirement land between them, so the guard sees `active`, allows, and
    # the stale write clobbers the superseded record. Holding the lock across both
    # forces every session write to serialize, so whichever order runs, the
    # superseded state wins or the guard fires.
    with _session_write_lock():
        try:
            on_disk = load_session(session_id)
        except (FileNotFoundError, ValueError, UnreadableFile):
            on_disk = None
        terminal = (on_disk or {}).get("status")
        candidate_status = candidate.get("status")
        if terminal == "superseded" and candidate_status != "superseded":
            raise ValueError(f"refusing to resurrect superseded session {session_id}")
        if (
            terminal == "stopped"
            and candidate_status not in {"stopped", "superseded"}
            and not allow_reactivation
        ):
            raise ValueError(f"refusing to resurrect stopped session {session_id}")
        write_regular_file_atomically(path, content)
    payload.clear()
    payload.update(candidate)


def reserve_message_result(
    session: dict,
    message: dict,
    *,
    include_canonical: bool,
) -> tuple[dict, str]:
    message_path = str(message["path"])
    processed = list(session.get("processed_messages", []))
    if message_path not in processed:
        processed.append(message_path)
    candidate = {**session, "processed_messages": processed}
    if include_canonical and message_needs_canonical_materialization(session, message):
        canonical = session.get("canonical_settled_messages", {})
        canonical = dict(canonical) if isinstance(canonical, dict) else {}
        canonical.setdefault(message_path, {"reason": "gate_disabled"})
        candidate["canonical_settled_messages"] = canonical
    return prepare_session_write(candidate)


def resolve_active_canonical_binding(
    project_id: str, chat_id: str, agent_id: str, runtime_session_id: str
) -> dict[str, Any] | None:
    """The current canonical binding fields for a participant's EXACT native session.

    Reader-only: the caller copies binding_id/binding_generation/endpoint_id from
    the ledger's active conversation_bindings row so the session, its file
    binding, and the delivered packet all assert the SAME canonical identity the
    materialization fence checks. The collabd layer owns assignment and the
    generation fence; this never writes.

    The active binding is resolved by participant, but a participant's binding is
    minted for one native session. Returning it for a different registering
    runtime session would stamp a fence a stale/wrong Pi thread then passes,
    waking the wrong native session. So None when there is no resolvable active
    binding, and CanonicalBindingNativeMismatch when one exists but is bound to a
    native session other than `runtime_session_id`; only an exact native match
    returns the canonical fields.
    """
    workspace_id = config_get("workspace_id")
    if not workspace_id:
        return None
    _repo_package_root()
    ledger_module = importlib.import_module(".".join(("llm_collab", "ledger")))
    try:
        paths = ledger_module.LedgerPaths.derive(project_state_root(), str(workspace_id))
        with ledger_module.LedgerStore.open_reader(paths) as store:
            resolved = store.resolve_conversation_binding(
                workspace_id=store.paths.workspace_id,
                scope_kind="project",
                scope_identity=str(project_id),
                conversation_id=str(chat_id),
                participant_id="participant_" + str(agent_id),
            )
    except Exception:
        return None
    if not resolved.get("resolved"):
        return None
    if resolved.get("native_session_id") != runtime_session_id:
        raise CanonicalBindingNativeMismatch(
            canonical_native_session_id=resolved.get("native_session_id"),
            requested_runtime_session_id=runtime_session_id,
        )
    return {
        "binding_id": resolved["binding_id"],
        "binding_generation": resolved["generation"],
        "endpoint_id": resolved["endpoint_id"],
    }


def resolve_session_receive_binding(
    session: dict,
) -> tuple[bool, dict[str, Any] | None]:
    """Resolve an exact receive binding without mutating the session record.

    The session file supplies the participant and native-session identity; the
    ledger remains the sole binding authority. A session that already carries a
    binding must still match the active ledger row. An otherwise-unbound session
    may receive a derived in-memory binding only when the ledger resolver proves
    the same exact native session. No binding means ``(True, None)`` so legacy
    generic routing remains available; target-bound matching rejects that session
    separately.
    """
    runtime = runtime_metadata(session)
    runtime_session_id = runtime.get("session_id")
    project_id = session.get("project_id")
    chat_id = session.get("chat_id")
    agent_id = session.get("agent_id")
    stored_binding_id = session.get("binding_id")
    stored_generation = session.get("binding_generation")

    if not (runtime_session_id and project_id and chat_id and agent_id):
        return (not stored_binding_id, None)
    try:
        canonical = resolve_active_canonical_binding(
            str(project_id), str(chat_id), str(agent_id), str(runtime_session_id)
        )
    except (CanonicalBindingNativeMismatch, SystemExit):
        canonical = None
    if stored_binding_id:
        if canonical is None:
            return False, None
        return (
            canonical.get("binding_id") == stored_binding_id
            and canonical.get("binding_generation") == stored_generation,
            canonical,
        )
    return True, canonical


def resolve_active_canonical_owner(
    project_id: str, chat_id: str, agent_id: str
) -> dict[str, Any] | None:
    """The current active canonical owner for a participant, native session included.

    Reader-only sibling of resolve_active_canonical_binding, but without the native
    fence: it returns the active binding whatever native session owns it, so the
    replacement authorization check (#319) can prove that a `--supersedes-session`
    predecessor is EXACTLY that owner before any rebind. None when nothing resolves.
    """
    workspace_id = config_get("workspace_id")
    if not workspace_id:
        return None
    _repo_package_root()
    ledger_module = importlib.import_module(".".join(("llm_collab", "ledger")))
    try:
        paths = ledger_module.LedgerPaths.derive(project_state_root(), str(workspace_id))
        with ledger_module.LedgerStore.open_reader(paths) as store:
            resolved = store.resolve_conversation_binding(
                workspace_id=store.paths.workspace_id,
                scope_kind="project",
                scope_identity=str(project_id),
                conversation_id=str(chat_id),
                participant_id="participant_" + str(agent_id),
            )
    except Exception:
        return None
    if not resolved.get("resolved"):
        return None
    return {
        "binding_id": resolved["binding_id"],
        "generation": resolved["generation"],
        "endpoint_id": resolved["endpoint_id"],
        "native_session_id": resolved["native_session_id"],
    }


class PiProvisioningRefused(RuntimeError):
    """A Pi register could not provision its canonical binding.

    Carries a stable `reason` token the CLI surfaces. Every precondition that can
    refuse (unresolved repo, unbindable runtime home, cwd not under the repo root,
    project absent from the latest registry snapshot) is checked BEFORE the first
    ledger write, so a refusal leaves the ledger untouched.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _require_registry_project(store, workspace_id: str, project_id: str) -> str:
    """The latest registry revision that lists this project, or refuse.

    The daemon registry import owns the snapshot; provisioning never mints it.
    Read-only, so it runs before any provisioning write.
    """
    row = store._connection.execute(
        "SELECT registry_revision FROM workspace_registry_snapshots "
        "WHERE workspace_id = ? ORDER BY captured_at_utc DESC, registry_revision DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise PiProvisioningRefused(
            "canonical_project_snapshot_required", "no workspace registry snapshot"
        )
    revision = str(row[0])
    if store.get_project_snapshot(
        workspace_id=workspace_id, project_id=project_id, registry_revision=revision
    ) is None:
        raise PiProvisioningRefused(
            "canonical_project_snapshot_required",
            f"project {project_id} is absent from the latest registry revision",
        )
    return revision


def _refuse_if_native_owned_elsewhere(
    store,
    *,
    workspace_id: str,
    provider_id: str,
    endpoint_id: str,
    owner_key: str,
    native_session_id: str,
    runtime_instance_id: str,
) -> None:
    """Refuse if this native session already owns a mutation-capable binding.

    Reuses the exact owner columns of
    conversation_bindings_one_mutation_owner_per_native_session (matching its
    COALESCE(owner_key, session_ref_id)), so a duplicate is refused before reserve
    writes a challenge — no second ownership rule, no accumulated challenge.
    """
    row = store._connection.execute(
        "SELECT scope_identity, conversation_id, participant_id FROM conversation_bindings "
        "WHERE workspace_id = ? AND provider_id = ? AND endpoint_id = ? "
        "AND COALESCE(owner_key, session_ref_id) = ? AND native_session_id = ? "
        "AND runtime_instance_id = ? AND mutation_capable = 1 "
        "AND state IN ('active', 'draining') LIMIT 1",
        (workspace_id, provider_id, endpoint_id, owner_key, native_session_id, runtime_instance_id),
    ).fetchone()
    if row is not None:
        raise PiProvisioningRefused(
            "canonical_native_session_already_bound",
            f"native session already owns an active binding in {row[0]}/{row[1]}/{row[2]}",
        )


def _ensure_lifecycle_prerequisites(store, subject, descriptor, created_at_utc: str) -> None:
    """Provision ONLY the two rows reserve() demands. Idempotent (INSERT OR IGNORE)."""
    store._connection.execute(
        "INSERT OR IGNORE INTO lifecycle_provider_registry "
        "(workspace_id, provider_id, provider_revision, trust_class, "
        "supported_operations_json, challenge_algorithm, challenge_ttl_seconds, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject.workspace_id,
            descriptor["provider_id"],
            descriptor["provider_revision"],
            descriptor["trust_class"],
            descriptor["supported_operations_json"],
            descriptor["challenge_algorithm"],
            descriptor["challenge_ttl_seconds"],
            created_at_utc,
        ),
    )
    store._connection.execute(
        "INSERT OR IGNORE INTO conversation_participants "
        "(workspace_id, scope_kind, scope_identity, conversation_id, participant_id, agent_id, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            subject.workspace_id,
            subject.scope_kind,
            subject.scope_identity,
            subject.conversation_id,
            subject.participant_id,
            subject.agent_id,
            created_at_utc,
        ),
    )


def provision_pi_canonical_binding(
    project_id: str,
    chat_id: str,
    agent_id: str,
    *,
    native_session_id: str,
    endpoint_id: str,
    runtime_instance_id: str,
    cwd: str,
    runtime_home_path: str,
    repo_target: str,
    predecessor: dict[str, object] | None = None,
    actor_id: str | None = None,
) -> dict[str, object]:
    """Mint the canonical Pi binding via the lifecycle reserve/consume seam.

    Register calls this only when no active binding exists and the native
    extension supplied its identity. Identity (endpoint, native session, runtime
    instance) is native-supplied and frozen by reserve/consume; no second identity
    is derived here. Only the two missing lifecycle prerequisites are provisioned;
    the registry snapshot is the daemon import's authority and must already exist.
    Ordering guarantees the ledger is untouched on any precondition refusal
    (autocommit connection: raw INSERTs commit immediately, so every validation
    precedes the first write).

    Native-session replacement (#319): when `predecessor` names the current active
    canonical owner ({binding_id, generation}), the successor is consumed pre-active
    (`reserved`) and swapped in atomically via the existing rebind authority
    (transition `rebind`, reason `native_session_replacement`). No prior deactivate:
    the predecessor stays active and usable until the swap supersedes it, so any
    reserve/consume refusal leaves it unchanged. `actor_id` identifies the
    registering session path, never operator approval.
    """
    import re
    import sqlite3
    from datetime import timedelta
    from _helpers import now_utc, resolve_project_repo_path

    if predecessor is not None and not actor_id:
        raise PiProvisioningRefused(
            "canonical_replacement_actor_required",
            "native-session replacement requires a registering actor_id",
        )
    workspace_id = config_get("workspace_id")
    if not workspace_id:
        raise PiProvisioningRefused("canonical_workspace_required", "workspace_id is unset")
    repo_root = resolve_project_repo_path(str(project_id), str(repo_target))
    if repo_root is None:
        raise PiProvisioningRefused(
            "canonical_repo_root_unresolved",
            f"repo-target {repo_target!r} is not a repos key of project {project_id!r}",
        )

    _repo_package_root()
    ledger_module = importlib.import_module(".".join(("llm_collab", "ledger")))
    store_module = importlib.import_module(".".join(("llm_collab", "ledger", "store")))
    lifecycle_module = importlib.import_module(".".join(("llm_collab", "session_lifecycle")))
    home_module = importlib.import_module(".".join(("llm_collab", "codex_runtime_home")))
    ref_module = importlib.import_module(".".join(("llm_collab", "codex_session_ref")))
    if predecessor is not None:
        try:
            actor_id = store_module._conversation_binding_text(actor_id, "actor_id", 128)
        except ValueError as error:
            raise PiProvisioningRefused("canonical_replacement_actor_invalid", str(error))

    provider = lifecycle_module.PiLifecycleProvider()
    descriptor = provider.descriptor()
    try:
        runtime_home = home_module.bind_runtime_home(runtime_home_path)
    except home_module.RuntimeHomeError as error:
        raise PiProvisioningRefused("canonical_runtime_home_unbound", str(error))

    subject = lifecycle_module.LifecycleSubject(
        workspace_id=str(workspace_id),
        scope_kind="project",
        scope_identity=str(project_id),
        conversation_id=str(chat_id),
        participant_id="participant_" + str(agent_id),
        agent_id="agent_" + str(agent_id),
        endpoint_id=str(endpoint_id),
        native_session_id=str(native_session_id),
        runtime_instance_id=str(runtime_instance_id),
    )
    trusted = lifecycle_module.TrustedProjectRoot(
        str(project_id), str(repo_target), str(repo_root), str(cwd)
    )
    core = lifecycle_module.SessionLifecycleCore(provider)

    now_dt = now_utc()
    created = now_dt.isoformat(timespec="seconds")
    expires = (
        now_dt + timedelta(seconds=int(descriptor["challenge_ttl_seconds"]))
    ).isoformat(timespec="seconds")
    # correlation_id must match the schema token grammar ([A-Za-z][A-Za-z0-9._-]{0,127}),
    # but a native session id is free-form, so fold anything else to '-'.
    safe_native = re.sub(r"[^A-Za-z0-9._-]", "-", str(native_session_id))
    correlation = ("pi_provision_" + safe_native)[:128]

    # Attest once as a read-only gate: this is where cwd-under-repo-root and the
    # runtime-home proof are checked, so a bad cwd/repo refuses before any write.
    # Capture the session_ref so the owner key is computed without re-attesting.
    try:
        session_ref = provider.attest(
            subject,
            runtime_home=runtime_home,
            observed_at_utc=created,
            correlation_id=correlation,
            trusted_project_root=trusted,
        )
    except (ref_module.SessionRefError, lifecycle_module.SessionLifecycleError) as error:
        raise PiProvisioningRefused("canonical_attestation_failed", str(error))
    owner_key = ref_module.derive_session_owner_key(
        workspace_id=str(workspace_id),
        endpoint_id=str(endpoint_id),
        native_session_id=str(native_session_id),
        runtime_home=runtime_home,
        authority=provider.authority(),
    )
    session_ref_id = str(session_ref["session_ref_id"])

    paths = ledger_module.LedgerPaths.derive(project_state_root(), str(workspace_id))
    with ledger_module.LedgerStore.open_writer(paths) as store:
        _require_registry_project(store, str(workspace_id), str(project_id))
        # A native session owns at most one mutation-capable binding
        # (conversation_bindings_one_mutation_owner_per_native_session). Refuse a
        # second scope BEFORE reserve writes a challenge, reusing that exact owner
        # key/index — no new ownership rule.
        _refuse_if_native_owned_elsewhere(
            store,
            workspace_id=str(workspace_id),
            provider_id=str(descriptor["provider_id"]),
            endpoint_id=str(endpoint_id),
            owner_key=owner_key,
            native_session_id=str(native_session_id),
            runtime_instance_id=str(runtime_instance_id),
        )
        _ensure_lifecycle_prerequisites(store, subject, descriptor, created)
        try:
            challenge = core.reserve(
                store,
                subject,
                runtime_home=runtime_home,
                created_at_utc=created,
                expires_at_utc=expires,
                correlation_id=correlation,
                trusted_project_root=trusted,
            )
            successor = core.consume(
                store,
                subject,
                challenge,
                runtime_home=runtime_home,
                consumed_at_utc=created,
                correlation_id=correlation,
                trusted_project_root=trusted,
                binding_state="active" if predecessor is None else "reserved",
            )
            if predecessor is not None:
                # The successor was minted `reserved` (excluded from the one-active
                # index), so the predecessor is still the sole active owner here.
                # rebind flips predecessor active->superseded and successor
                # reserved->active atomically; there is no window with two active
                # owners and none with none.
                core.rebind(
                    store,
                    subject,
                    predecessor,
                    successor,
                    transition_kind="rebind",
                    actor_id=str(actor_id),
                    reason="native_session_replacement",
                    evidence=(
                        "native_session_replacement"
                        f" predecessor={predecessor['binding_id']}:{predecessor['generation']}"
                        f" successor={successor['binding_id']}:{successor['generation']}"
                        f" native={native_session_id}"
                    ).encode("utf-8"),
                    created_at_utc=created,
                )
            return {
                "binding_id": successor["binding_id"],
                "binding_generation": successor["generation"],
                "endpoint_id": successor["endpoint_id"],
            }
        except sqlite3.IntegrityError as error:
            # A concurrent register took this owner between the prevention check
            # and consume. Convert the raw index violation to a bounded refusal;
            # the prevention check keeps repeated (sequential) attempts from
            # writing a challenge at all, so they cannot accumulate.
            raise PiProvisioningRefused("canonical_native_session_already_bound", str(error))
        except (store_module.CanonicalConflictError, lifecycle_module.SessionLifecycleError) as error:
            # The rebind swap found the predecessor no longer transitionable or the
            # successor not pre-active (e.g. a racing transition). Convert to a bounded
            # refusal instead of leaking a traceback; the swap is atomic, so a refused
            # transition leaves both bindings as they were.
            raise PiProvisioningRefused("canonical_replacement_conflict", str(error))


PI_SESSION_HEADER_VERSION = 3


def read_pi_session_fingerprint(
    source_path, expected_native_session_id
) -> dict[str, str] | None:
    """The current provider/model/thinking/cwd fingerprint of a native Pi session,
    or None (fail closed).

    Reads the EXACT registered session source — Pi's own append-only .jsonl history —
    and folds it: cwd from the single `session` header, provider + modelId from the
    latest `model_change`, thinkingLevel from the latest `thinking_level_change`.
    Returns None — meaning no wake, never a false wake — on anything that is not a
    clean, complete, identity-matched fingerprint: an empty/unset source, an
    unreadable or oversized file, a bad header version, malformed/partial JSONL, a
    second (spliced) header, a header id that is not the registered native session,
    or any missing field.
    """
    if not source_path:
        return None
    # ponytail: bounded full scan (fold to the latest change events). If a live Pi
    # session grows past MAX_SESSION_SCAN_BYTES, fail closed here (no false wake) and
    # switch to a tail read of the last N KiB.
    try:
        raw = read_regular_file_bounded(Path(str(source_path)), MAX_SESSION_SCAN_BYTES)
    except (UnreadableFile, OSError, ValueError):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    cwd = provider = model_id = thinking_level = None
    header_seen = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(record, dict):
            return None
        record_type = record.get("type")
        if record_type == "session":
            if header_seen:
                return None
            header_seen = True
            if record.get("version") != PI_SESSION_HEADER_VERSION:
                return None
            if str(record.get("id")) != str(expected_native_session_id):
                return None
            cwd = record.get("cwd")
        elif record_type == "model_change":
            provider = record.get("provider")
            model_id = record.get("modelId")
        elif record_type == "thinking_level_change":
            thinking_level = record.get("thinkingLevel")
    if not header_seen:
        return None
    fingerprint = {
        "cwd": cwd,
        "provider": provider,
        "model_id": model_id,
        "thinking_level": thinking_level,
    }
    if any(not isinstance(value, str) or not value for value in fingerprint.values()):
        return None
    return fingerprint


def binding_payload_from_session(
    session: dict,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Derive the binding record for `session`.

    `existing` lets a caller pass the snapshot it has ALREADY read and validated, so the payload is
    built from that exact bytes-in-hand version rather than reopening the path. Reopening is a TOCTOU:
    a validated-then-swapped binding, or a second read that fails where the first succeeded, lands
    between a caller's checks and its writes. Default None preserves the read-it-myself behaviour for
    every existing caller.
    """
    runtime = runtime_metadata(session)
    project_id = session.get("project_id")
    chat_id = session.get("chat_id")
    runtime_family = runtime.get("family")
    runtime_session_id = runtime.get("session_id")
    if not project_id or not chat_id or not runtime_family or not runtime_session_id:
        return None

    if existing is None:
        existing = {}
        try:
            existing = load_binding(str(project_id), str(chat_id), str(session["agent_id"]))
        except FileNotFoundError:
            existing = {}

    payload = {
        **existing,
        "project_id": project_id,
        "chat_id": chat_id,
        "agent_id": session["agent_id"],
        "session_id": session["session_id"],
        "runtime_family": runtime_family,
        "runtime_session_id": runtime_session_id,
        "runtime_session_source": runtime.get("session_source"),
        "runtime_home": runtime.get("home") or runtime_home_from_source(str(runtime_family), runtime.get("session_source")),
        "status": session.get("status"),
        "bound_at_utc": existing.get("bound_at_utc", utc_iso()),
        "last_seen_at_utc": utc_iso(),
        "supersedes_session_id": session.get("supersedes_session_id"),
    }
    # Carry the canonical identity onto the file binding so deliver.py reads a
    # target_binding_id from it and inbox.py's exact-read fence matches. Absent
    # on the session (unbound, or a non-pi family), these simply stay off.
    for key in ("binding_id", "binding_generation", "endpoint_id"):
        if session.get(key) is not None:
            payload[key] = session[key]
    for key in ("repo_targets", "pi_fingerprint"):
        if session.get(key) is not None:
            payload[key] = session[key]
    return payload


def update_binding_from_session(
    session: dict,
    existing: dict[str, Any] | None = None,
    prepared: tuple[dict, str] | None = None,
) -> dict[str, Any] | None:
    payload = (
        prepared[0]
        if prepared is not None
        else binding_payload_from_session(session, existing=existing)
    )
    if payload is None:
        return None
    save_binding(payload, prepared=prepared)
    return payload


def _append_event_path(
    path: Path,
    event: dict[str, Any],
    *,
    compact_repeats: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen_at = utc_iso()
    event_payload = {"ts": seen_at, **event}
    if not compact_repeats:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event_payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return

    reason = event_payload.get("reason")
    if isinstance(reason, str) and reason:
        event_payload.update(
            {
                "first_seen_at_utc": seen_at,
                "last_seen_at_utc": seen_at,
                "repeat_compacted": True,
                "repeat_count": 1,
            }
        )

    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        search_start = max(
            0,
            size - MAX_EVENT_LOG_BYTES - EVENT_LOG_TRUNCATION_RESERVE_BYTES,
        )
        handle.seek(search_start)
        search_window = handle.read(size - search_start)
        tail_start = 0
        cursor = search_start
        for raw_line in search_window.splitlines(keepends=True):
            try:
                candidate = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                candidate = None
            cursor += len(raw_line)
            if (
                isinstance(candidate, dict)
                and candidate.get("event") == "event_log_truncated"
                and candidate.get("retention") == "bounded_head_and_tail"
            ):
                tail_start = cursor

        last_start = size
        last_event = None
        if size:
            last_read_start = max(tail_start, size - MAX_EVENT_LOG_BYTES)
            handle.seek(last_read_start)
            tail = handle.read(size - last_read_start).removesuffix(b"\n")
            separator = tail.rfind(b"\n")
            if separator >= 0 or last_read_start == tail_start:
                last_start = last_read_start + separator + 1
                try:
                    last_event = json.loads(tail[separator + 1 :])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

        replace_last = (
            isinstance(last_event, dict)
            and last_event.get("repeat_compacted") is True
            and type(last_event.get("repeat_count")) is int
            and (
                last_event.get("event"),
                last_event.get("reason"),
                last_event.get("message_path"),
            )
            == (
                event_payload.get("event"),
                event_payload.get("reason"),
                event_payload.get("message_path"),
            )
        )
        if replace_last:
            event_payload = {
                **last_event,
                "last_seen_at_utc": seen_at,
                "repeat_count": last_event["repeat_count"] + 1,
            }
            encoded = (json.dumps(event_payload, sort_keys=True) + "\n").encode()
            projected_size = last_start + len(encoded)
        else:
            encoded = (json.dumps(event_payload, sort_keys=True) + "\n").encode()
            projected_size = size + len(encoded)

        if len(encoded) > MAX_EVENT_LOG_BYTES:
            encoded = (
                json.dumps(
                    {
                        "event": "event_log_event_truncated",
                        "message_path": event_payload.get("message_path"),
                        "original_bytes": len(encoded),
                        "original_event": event_payload.get("event"),
                        "original_reason": event_payload.get("reason"),
                        "truncated": True,
                        "ts": seen_at,
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            replace_last = False
            projected_size = size + len(encoded)

        region_limit = MAX_EVENT_LOG_BYTES - (
            0 if tail_start else EVENT_LOG_TRUNCATION_RESERVE_BYTES
        )
        projected_region_size = projected_size - tail_start
        if projected_region_size > region_limit and not tail_start:
            marker = (
                json.dumps(
                    {
                        "event": "event_log_truncated",
                        "head_retained_bytes": size,
                        "retention": "bounded_head_and_tail",
                        "tail_limit_bytes": MAX_EVENT_LOG_BYTES,
                        "truncated": True,
                        "ts": seen_at,
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            handle.seek(0, os.SEEK_END)
            handle.write(marker)
            handle.write(encoded)
        elif projected_region_size > region_limit:
            handle.seek(tail_start)
            retained = handle.read(size - tail_start)
            if replace_last:
                retained = retained[: last_start - tail_start] + encoded
            else:
                retained += encoded
            keep = []
            kept_bytes = 0
            for line in reversed(retained.splitlines(keepends=True)):
                if keep and kept_bytes + len(line) > MAX_EVENT_LOG_BYTES // 2:
                    break
                keep.append(line)
                kept_bytes += len(line)
            handle.truncate(tail_start)
            handle.seek(0, os.SEEK_END)
            handle.write(b"".join(reversed(keep)))
        else:
            if replace_last:
                handle.truncate(last_start)
            handle.seek(0, os.SEEK_END)
            handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def append_event(session_id: str, event: dict[str, Any]) -> None:
    _append_event_path(
        autobridge_event_log_path(session_id),
        event,
        compact_repeats=True,
    )


def append_wake_event(session_id: str, event: dict[str, Any]) -> None:
    _append_event_path(autobridge_wake_log_path(session_id), event)


def write_operator_turn_summary(
    session: dict,
    message: dict,
    *,
    event_name: str,
    body: str,
) -> Path | None:
    chat_id = message.get("frontmatter", {}).get("chat_id")
    if not chat_id:
        return None
    matches = sorted(STATE_ROOT.joinpath("Chats").glob(f"*__{chat_id}"))
    if not matches:
        return None
    resolved_chat_dir = matches[-1]
    fm = message.get("frontmatter", {})
    runtime = runtime_metadata(session)
    return write_chat_note(
        resolved_chat_dir,
        title=f"{session['agent_id']} {event_name}: {fm.get('title', '(no title)')}",
        body=body,
        sender=str(session["agent_id"]),
        recipient="operator",
        project_id=session.get("project_id"),
        extra_frontmatter={
            "informational_kind": "autobridge_turn_summary",
            "summary_event": event_name,
            "summary_sender": fm.get("sender_agent_id", fm.get("from")),
            "summary_recipient": session["agent_id"],
            "sender_session_id": fm.get("sender_session_id"),
            "target_session_id": fm.get("target_session_id"),
            "runtime_session_id": runtime.get("session_id"),
            "related_message_path": message.get("path"),
        },
    )


def runtime_metadata(session: dict) -> dict[str, Any]:
    runtime = session.get("runtime")
    if isinstance(runtime, dict):
        return runtime
    return {}


def livecraft_runtime_wake_available(session: dict) -> bool:
    runtime = runtime_metadata(session)
    return (
        runtime.get("family") == "pi"
        and session.get("endpoint_id") == "endpoint_pi_livecraft_local"
        and bool(runtime.get("command"))
        and bool(session.get("binding_id"))
        and bool(session.get("binding_generation"))
    )


def runtime_home_from_source(runtime_family: str, session_source: str | None) -> str | None:
    if not session_source:
        return None
    source_path = Path(session_source).expanduser()
    if runtime_family == "codex_app":
        return str(source_path.parent)
    if runtime_family == "claude_app":
        return str(source_path.parent.parent.parent)
    if runtime_family == "gemini_cli":
        current = source_path.parent
        while current != current.parent:
            if current.name == "tmp":
                return str(current.parent)
            current = current.parent
    return None


def runtime_binary_env_var(runtime_family: str) -> str | None:
    mapping = {
        "codex_app": "LLM_COLLAB_CODEX_BIN",
        "claude_app": "LLM_COLLAB_CLAUDE_BIN",
        "gemini_cli": "LLM_COLLAB_GEMINI_BIN",
    }
    return mapping.get(runtime_family)


def runtime_binary_default(runtime_family: str) -> str:
    mapping = {
        "codex_app": "codex",
        "claude_app": "claude",
        "gemini_cli": "gemini",
    }
    return mapping[runtime_family]


def runtime_binary(runtime_family: str) -> str:
    env_var = runtime_binary_env_var(runtime_family)
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return runtime_binary_default(runtime_family)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude"))).expanduser()


def gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_HOME", str(Path.home() / ".gemini"))).expanduser()


def _parse_jsonl_last_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    last_object: dict[str, Any] | None = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last_object = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last_object


def discover_codex_runtime_session() -> dict[str, Any]:
    index_path = codex_home() / "session_index.jsonl"
    payload = _parse_jsonl_last_object(index_path)
    if payload and payload.get("id"):
        return {
            "family": "codex_app",
            "session_id": str(payload["id"]),
            "session_source": str(index_path),
            "home": str(codex_home()),
            "seen_at": payload.get("updated_at"),
        }

    history_path = codex_home() / "history.jsonl"
    payload = _parse_jsonl_last_object(history_path)
    if payload and payload.get("session_id"):
        return {
            "family": "codex_app",
            "session_id": str(payload["session_id"]),
            "session_source": str(history_path),
            "home": str(codex_home()),
            "seen_at": payload.get("ts"),
        }
    raise FileNotFoundError("No Codex session index found")


def claude_project_slug(project_path: str | None) -> str | None:
    if not project_path:
        return None
    return str(Path(project_path).expanduser().resolve()).replace("/", "-")


MAX_CLAUDE_CANDIDATES = 128
MAX_CLAUDE_DISCOVERY_BYTES = 1024 * 1024
MAX_CLAUDE_RECORD_PREFIX_BYTES = 32 * 1024


def _claude_session_evidence(
    path: Path, prefix_bytes: int, resolved_project: str, expected_stem: str
) -> str:
    """Bound and classify one Claude session .jsonl candidate.

    Returns one of:
      * ``"match"`` -- a record asserts an ABSOLUTE cwd that canonicalizes to
        ``resolved_project`` AND, in that SAME record, a ``sessionId`` equal to
        ``expected_stem`` (project + identity proved together).
      * ``"other_project"`` -- at least one record asserted an absolute cwd, and
        none of them is this project (the artifact is demonstrably not ours).
      * ``"unprovable"`` -- the artifact is unreadable/non-regular, asserts no
        absolute cwd at all, or a record asserts our project but its identity is
        missing/conflicting. Unprovable candidates must fail closed: they may be
        a second exact session, so a readable sibling must never be returned as
        unique by silently treating them as absent.

    Only a bounded prefix is read and charged to the active cumulative budget
    (transcripts can be tens of MB; the cwd/sessionId metadata sit near the
    start). Mirrors ``read_regular_file_bounded``: a non-blocking open plus a
    same-descriptor regular-file guard. A relative cwd is never resolved against
    the caller's process cwd -- it is not absolute, so it asserts nothing.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except (FileNotFoundError, OSError):
        return "unprovable"
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return "unprovable"
        chunks: list[bytes] = []
        remaining = prefix_bytes
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return "unprovable"
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if _ACTIVE_READ_BUDGET:
        _ACTIVE_READ_BUDGET[-1].charge(len(raw), path)
    saw_absolute_cwd = False
    saw_project_cwd = False
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        cwd = record.get("cwd")
        # Only an ABSOLUTE cwd is project evidence; a relative cwd must never be
        # resolved against the discovery process cwd.
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
            continue
        saw_absolute_cwd = True
        if os.path.realpath(cwd) != resolved_project:
            continue
        saw_project_cwd = True
        # Project and identity must be asserted by the SAME record.
        session_id = record.get("sessionId")
        if isinstance(session_id, str) and session_id and session_id == expected_stem:
            return "match"
    if saw_project_cwd:
        return "unprovable"
    if saw_absolute_cwd:
        return "other_project"
    return "unprovable"


def discover_claude_runtime_session(project_path: str | None = None) -> dict[str, Any]:
    # Diagnostic only. Explicit ``register --runtime-session-id`` is the binding
    # authority; discovery never guesses. Current Claude Code writes per-session
    # .jsonl artifacts under ~/.claude/projects/<slug>/ and no longer maintains
    # the legacy sessions-index.json. The slug directory alone is NOT exact
    # project evidence -- path.replace("/", "-") is not injective
    # (/a-b/c and /a/b-c collide) -- so each candidate's canonical ``cwd`` field
    # must be an ABSOLUTE path that canonicalizes to the exact project and, in
    # that SAME record, ``sessionId`` must equal the artifact filename -- project
    # and identity are proved together, never accumulated across records or
    # derived from a relative (caller-cwd-dependent) cwd. Directory enumeration
    # is bounded at the scandir boundary. Unreadable/unprovable, zero, multiple,
    # or unscoped candidates all fail closed; there is never a newest/legacy
    # fallback.
    if not project_path:
        raise FileNotFoundError(
            "claude_app discovery requires --project-path; an unscoped read could "
            "select another project's session"
        )
    resolved_project = os.path.realpath(os.path.expanduser(project_path))
    project_slug = claude_project_slug(project_path)
    project_dir = claude_home() / "projects" / project_slug
    # Bound directory enumeration at the earliest boundary: count EVERY entry
    # (not just .jsonl), fail closed past the cap, then sort the retained
    # candidates. Mirrors iter_sessions()'s os.scandir pattern; a plain
    # glob("*.jsonl") would materialize the whole directory and ignore non-jsonl
    # entries, so a pathological directory would not be bounded during the scan.
    candidates: list[Path] = []
    try:
        scan = os.scandir(project_dir)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise FileNotFoundError(
            f"cannot read Claude project directory for {project_path}: {error}"
        ) from error
    else:
        with scan:
            for count, entry in enumerate(scan, start=1):
                if count > MAX_CLAUDE_CANDIDATES:
                    raise FileNotFoundError(
                        f"too many entries in the Claude project directory for "
                        f"{project_path}; use register --runtime-session-id"
                    )
                # Append every .jsonl pathname WITHOUT an is_file() prefilter: a
                # .jsonl FIFO/dir/broken-symlink must reach the reader, which
                # classifies non-regular/unreadable as unprovable (fail closed),
                # rather than being silently omitted so a readable sibling looks
                # unique.
                if entry.name.endswith(".jsonl"):
                    candidates.append(Path(entry.path))
    candidates.sort()
    matches: list[Path] = []
    budget = ReadBudget(MAX_CLAUDE_DISCOVERY_BYTES, "claude discovery")
    with active_read_budget(budget):
        try:
            for artifact in candidates:
                status = _claude_session_evidence(
                    artifact,
                    MAX_CLAUDE_RECORD_PREFIX_BYTES,
                    resolved_project,
                    artifact.stem,
                )
                if status == "other_project":
                    continue
                if status == "unprovable":
                    raise FileNotFoundError(
                        f"Claude session artifact {artifact.name} is unreadable or "
                        f"its project/identity could not be proven for "
                        f"{project_path}; use register --runtime-session-id"
                    )
                matches.append(artifact)
        except UnreadableFile as error:
            raise FileNotFoundError(
                f"claude_app discovery for {project_path} exceeded its read budget "
                f"({error}); use register --runtime-session-id"
            ) from error
    if not matches:
        raise FileNotFoundError(f"No exact Claude session for project {project_path}")
    if len(matches) > 1:
        raise FileNotFoundError(
            f"{len(matches)} exact Claude sessions for project {project_path}; "
            "use register --runtime-session-id to bind the intended one"
        )
    artifact = matches[0]
    return {
        "family": "claude_app",
        "session_id": artifact.stem,
        "session_source": str(artifact),
        "home": str(claude_home()),
        "seen_at": datetime.fromtimestamp(
            artifact.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds"),
    }


def discover_gemini_runtime_session(project_path: str | None = None) -> dict[str, Any]:
    tmp_root = gemini_home() / "tmp"
    candidates = sorted(tmp_root.glob("**/chats/session-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    resolved_project_path = str(Path(project_path).expanduser().resolve()) if project_path else None

    for path in candidates:
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if resolved_project_path and payload.get("projectPath") not in {None, resolved_project_path}:
            continue
        if payload.get("sessionId"):
            return {
                "family": "gemini_cli",
                "session_id": str(payload["sessionId"]),
                "session_source": str(path),
                "home": str(gemini_home()),
                "seen_at": payload.get("lastUpdated") or payload.get("startTime"),
            }
    raise FileNotFoundError("No Gemini session files found")


def discover_runtime_session(runtime_family: str, project_path: str | None = None) -> dict[str, Any]:
    if runtime_family == "codex_app":
        return discover_codex_runtime_session()
    if runtime_family == "claude_app":
        return discover_claude_runtime_session(project_path=project_path)
    if runtime_family == "gemini_cli":
        return discover_gemini_runtime_session(project_path=project_path)
    raise ValueError(f"Unsupported runtime family: {runtime_family}")


def session_is_expired(session: dict) -> bool:
    expires_at = parse_iso8601(session.get("lease_expires_utc"))
    if expires_at is None:
        return False
    return expires_at <= now_utc()


def session_is_dispatchable(session: dict) -> tuple[bool, str]:
    status = session.get("status")
    if status not in {"active", "parked"}:
        return False, f"status={status}"
    # GH-468: an activation reader's native id IS the worker's ordinary native, so
    # native identity is (family, id). A reader created from LLM_COLLAB_READER_
    # RUNTIME_ID alone — with no carried/resolvable family — must not become a
    # dispatchable (reader, id) route, or a later ordinary (real_family, id)
    # registration in another scope would not collide with or be masked by it.
    # Keep the record as a durable activation helper (claim/consume unchanged) but
    # non-routable until its real family is known; the ownership guard and
    # resolve_native_family() both key off this predicate, so this alone closes it.
    if session.get("ephemeral_reader"):
        family = (session.get("runtime") or {}).get("family")
        if not family or family == "reader":
            return False, "reader_runtime_family_unresolved"
    # An `active` session's validity follows its native task: it ends when that
    # task ends or an explicit continuation supersedes it, never on a clock. A
    # TTL is not evidence that a live session died, and treating it as evidence
    # is not a late wake but a silent one — on 2026-07-28 a session registered
    # at 19:12 went undispatchable at 20:12:01 while it was working, kept
    # reporting `status: active`, and nothing surfaced the wake path being dead
    # for three hours.
    #
    # `parked` keeps the clock. A parked claim is not held by a live task, so a
    # TTL is what makes an abandoned one reclaimable.
    if status != "active" and session_is_expired(session):
        return False, "lease_expired"
    return True, "ok"


def session_is_watcher_only(session: dict) -> bool:
    """Claude's pickup is its durable packet plus the app's own background inbox watcher.

    Keyed on the canonical agent identity, not on `runtime.family`: `register` accepts any
    caller-selected `--runtime-family` and `--runtime-command`, so a `claude` session declared
    as another family would otherwise select runtime_trigger, run a CLI command, and take the
    activation lease ahead of the watcher. Non-Claude families are unaffected.
    """
    return str(session.get("agent_id") or "") == "claude"


def session_target_ids(session: dict) -> set[str]:
    runtime = runtime_metadata(session)
    target_ids = {str(session["session_id"])}
    runtime_session_id = runtime.get("session_id")
    if runtime_session_id:
        target_ids.add(str(runtime_session_id))
    return target_ids


def session_requires_exact_receive_target(session: dict) -> bool:
    runtime = runtime_metadata(session)
    return (
        session.get("wake_strategy") == "runtime_trigger"
        and bool(runtime.get("command") or (runtime.get("family") and runtime.get("session_id")))
    )


def _repo_package_root() -> None:
    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def _bb_binding_state(project_id: str, chat_id: str, agent_id: str) -> str | None:
    """Map the ledger's exact reader result to the bootstrap decision vocabulary."""
    workspace_id = config_get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return "unreadable"
    try:
        ledger_module = importlib.import_module(".".join(("llm_collab", "ledger")))
        paths = ledger_module.LedgerPaths.derive(project_state_root(), workspace_id)
        with ledger_module.LedgerStore.open_reader(paths) as store:
            participant = store._connection.execute(
                """
                SELECT 1 FROM conversation_participants
                WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
                  AND conversation_id = ? AND participant_id = ?
                """,
                (workspace_id, project_id, chat_id, f"participant_{agent_id}"),
            ).fetchone()
            binding = store._connection.execute(
                """
                SELECT 1 FROM conversation_bindings
                WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
                  AND conversation_id = ? AND participant_id = ?
                LIMIT 1
                """,
                (workspace_id, project_id, chat_id, f"participant_{agent_id}"),
            ).fetchone()
            if participant is None and binding is None:
                return None
            resolved = store.resolve_conversation_binding(
                workspace_id=workspace_id,
                scope_kind="project",
                scope_identity=project_id,
                conversation_id=chat_id,
                participant_id=f"participant_{agent_id}",
            )
    except FileNotFoundError:
        return None
    except Exception:
        return "unreadable"

    if resolved.get("resolved"):
        # An active ledger owner without its session record is not absence; minting
        # another owner would be the exact split-brain this gate prevents.
        return "mismatch"
    return {
        "route_ambiguous": "ambiguous",
        "stale_generation": "mismatch",
        "session_unverified": "unreadable",
        "adapter_quarantined": "scope_refused",
        "pull_pending": "mismatch",
        "waiting_for_session": "mismatch",
    }.get(str(resolved.get("reason")), "unreadable")


def execute_bb_bootstrap_plan(
    plan,
    packet: dict[str, Any],
    inputs: dict[str, Any],
):
    """Run the bb managed-start saga and publish its exact receive session.

    Kept beside the other lifecycle seams so the inbox watcher remains a poller;
    its only job is to invoke this helper before it enumerates sessions.
    """
    _repo_package_root()
    bootstrap_module = importlib.import_module(".".join(("llm_collab", "bb_bootstrap")))
    client_module = importlib.import_module(".".join(("llm_collab", "bb_client")))
    managed_start_module = importlib.import_module(".".join(("llm_collab", "bb_managed_start")))
    runtime_home_module = importlib.import_module(".".join(("llm_collab", "codex_runtime_home")))
    ledger_module = importlib.import_module(".".join(("llm_collab", "ledger")))
    errors_module = importlib.import_module(".".join(("llm_collab", "managed_start_errors")))
    lifecycle_module = importlib.import_module(".".join(("llm_collab", "session_lifecycle")))

    execute_bootstrap = bootstrap_module.execute_bootstrap
    BbClient = client_module.BbClient
    SLICE_1A_PROFILE = client_module.SLICE_1A_PROFILE
    subprocess_transport = client_module.subprocess_transport
    bb_start_evidence_digest = managed_start_module.bb_start_evidence_digest
    bb_start_native = managed_start_module.bb_start_native
    validate_bb_start_evidence = managed_start_module.validate_bb_start_evidence
    bind_runtime_home = runtime_home_module.bind_runtime_home
    LedgerPaths = ledger_module.LedgerPaths
    LedgerStore = ledger_module.LedgerStore
    ManagedStartOrphaned = errors_module.ManagedStartOrphaned
    ManagedStartResponseLost = errors_module.ManagedStartResponseLost
    BbLifecycleProvider = lifecycle_module.BbLifecycleProvider
    LifecycleSubject = lifecycle_module.LifecycleSubject
    ManagedStartRequest = lifecycle_module.ManagedStartRequest
    SessionLifecycleCore = lifecycle_module.SessionLifecycleCore
    TrustedProjectRoot = lifecycle_module.TrustedProjectRoot

    workspace_id = config_get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise ValueError("workspace_id is required for bb bootstrap")
    runtime_home = bind_runtime_home(inputs["runtime_home"])
    provider = BbLifecycleProvider()
    core = SessionLifecycleCore(provider)
    request = ManagedStartRequest(
        workspace_id=workspace_id,
        scope_kind="project",
        scope_identity=plan.project_id,
        conversation_id=plan.conversation_id,
        participant_id=plan.participant_id,
        agent_id=f"agent_{plan.agent_id}",
        endpoint_id=inputs["endpoint_id"],
        runtime_instance_id=inputs["runtime_instance_id"],
    )
    trusted_root = TrustedProjectRoot(
        plan.project_id,
        str(inputs["repo_id"]),
        str(inputs["repo_root"]),
        str(inputs["cwd"]),
    )
    prompt = packet.get("body", "")
    if not isinstance(prompt, str):
        raise ValueError("first packet body must be text")
    client = BbClient(
        subprocess_transport(inputs["executable"]),
        enabled=True,
        timeout_seconds=inputs["timeout_seconds"],
    )
    start_native = bb_start_native(
        client,
        project_id=str(inputs["native_project_id"]),
        prompt=prompt,
        profile=SLICE_1A_PROFILE,
        endpoint_id=inputs["endpoint_id"],
        runtime_instance_id=inputs["runtime_instance_id"],
    )
    validate = lambda candidate: validate_bb_start_evidence(
        candidate,
        expected_project_id=str(inputs["native_project_id"]),
        expected_endpoint_id=inputs["endpoint_id"],
        expected_runtime_instance_id=inputs["runtime_instance_id"],
        expected_profile=SLICE_1A_PROFILE,
    )
    paths = LedgerPaths.derive(project_state_root(), workspace_id)
    with LedgerStore.open_writer(paths) as store:
        created = now_utc().isoformat(timespec="seconds")
        expires = (
            now_utc() + timedelta(seconds=provider.challenge_ttl_seconds)
        ).isoformat(timespec="seconds")
        subject = LifecycleSubject(
            workspace_id=workspace_id,
            scope_kind="project",
            scope_identity=plan.project_id,
            conversation_id=plan.conversation_id,
            participant_id=plan.participant_id,
            agent_id=f"agent_{plan.agent_id}",
            endpoint_id=inputs["endpoint_id"],
            native_session_id="pending",
            runtime_instance_id=inputs["runtime_instance_id"],
        )
        _ensure_lifecycle_prerequisites(store, subject, provider.descriptor(), created)

        def already_started(_message_id: str) -> bool:
            row = store._connection.execute(
                """
                SELECT 1 FROM managed_start_reservations
                WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
                  AND conversation_id = ? AND participant_id = ?
                  AND start_correlation_id = ?
                  AND state IN ('reserved', 'ambiguous_start', 'orphaned', 'bound')
                LIMIT 1
                """,
                (
                    workspace_id,
                    plan.project_id,
                    plan.conversation_id,
                    plan.participant_id,
                    plan.canonical_message_id,
                ),
            ).fetchone()
            return row is not None

        def start(item):
            result = core.start_managed(
                store,
                request,
                runtime_home=runtime_home,
                trusted_project_root=trusted_root,
                created_at_utc=created,
                expires_at_utc=expires,
                correlation_id=item.canonical_message_id,
                start_native=start_native,
                validate_start_evidence=validate,
                evidence_digest=bb_start_evidence_digest,
            )
            evidence = result["evidence"]
            native_thread_id = evidence.native_thread_id
            session_id = "bb-" + hashlib.sha256(
                "|".join(
                    (plan.project_id, plan.conversation_id, plan.agent_id, native_thread_id)
                ).encode()
            ).hexdigest()[:32]
            runtime = {
                "family": "bb",
                "session_id": native_thread_id,
                "home": runtime_home.runtime_home_realpath,
            }
            if inputs.get("session_source") is not None:
                runtime["session_source"] = inputs["session_source"]
            session = {
                "session_id": session_id,
                "agent_id": plan.agent_id,
                "project_id": plan.project_id,
                "chat_id": plan.conversation_id,
                "mode": "manual",
                "status": "active",
                "wake_strategy": "none",
                "allowed_actions": [],
                "lease_owner": None,
                "lease_expires_utc": None,
                "runtime": runtime,
                "created_utc": now_utc().isoformat(timespec="seconds"),
                "processed_messages": [plan.packet_path],
                "repo_targets": [str(inputs["repo_id"])],
                "binding_id": result["binding"]["binding_id"],
                "binding_generation": int(result["binding"]["generation"]),
                "endpoint_id": result["binding"]["endpoint_id"],
            }
            # Past this point the native thread EXISTS and its id is recoverable.
            # A publication failure that escaped as a generic error would reach
            # execute_bootstrap as BOOTSTRAP_FAILED — clean and retryable-looking —
            # and strand the real thread while inviting a second one. An orphan
            # carrying the id is the only honest outcome here.
            try:
                with _session_write_lock():
                    if update_binding_from_session(session, existing={}) is None:
                        raise ValueError("bb session cannot produce a binding record")
                    save_session(session)
            except Exception as error:
                raise ManagedStartOrphaned(
                    f"bb thread {native_thread_id} started but its session could not be "
                    f"published: {error}",
                    native_session_id=native_thread_id,
                ) from error
            return native_thread_id

        return execute_bootstrap(
            plan,
            already_started=already_started,
            start=start,
            on_ambiguous=ManagedStartResponseLost,
            on_orphaned=ManagedStartOrphaned,
        )


def _frontmatter_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if value:
        return [str(value)]
    return []


def _repo_target_set(value: Any) -> set[str] | None:
    if not isinstance(value, list):
        return None
    values = _frontmatter_strings(value)
    if (
        not values
        or values != value
        or any(item != item.strip() or not item for item in values)
        or len(values) != len(set(values))
    ):
        return None
    return set(values)


def repo_scope_matches(
    subscriber_targets: Any,
    packet_targets: Any,
    *,
    subscriber_project: Any = None,
    packet_project: Any = None,
) -> tuple[bool, str]:
    """Apply the one repo-scope rule used by inbox, watcher, and autobridge."""

    if subscriber_targets is None:
        return True, "unscoped"
    if not isinstance(subscriber_project, str) or not subscriber_project:
        return False, ROUTE_AMBIGUOUS_REASON
    if packet_project != subscriber_project:
        return False, ROUTE_AMBIGUOUS_REASON
    subscriber = _repo_target_set(subscriber_targets)
    packet = _repo_target_set(packet_targets)
    if subscriber is None or packet is None or not packet <= subscriber:
        return False, ROUTE_AMBIGUOUS_REASON
    return True, "repo_scope_match"


def _session_repo_scope_matches(
    session: dict,
    message: dict,
    invocation_repo_targets: Any = None,
) -> tuple[bool, str]:
    frontmatter = message.get("frontmatter", {})
    packet_targets = frontmatter.get("repo_targets")
    for subscriber_targets in (
        session.get("repo_targets"),
        invocation_repo_targets,
    ):
        matched, reason = repo_scope_matches(
            subscriber_targets,
            packet_targets,
            subscriber_project=session.get("project_id"),
            packet_project=frontmatter.get("project_id"),
        )
        if not matched:
            return False, reason
    return True, "repo_scope_match"


def _declared_target(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def binding_scoped_message_matches_session(
    session: dict,
    message: dict,
    *,
    invocation_repo_targets: Any = None,
) -> tuple[bool, str]:
    frontmatter = message.get("frontmatter", {})
    repo_match, repo_reason = _session_repo_scope_matches(
        session, message, invocation_repo_targets
    )
    if not repo_match:
        return False, repo_reason

    target_binding_id = _declared_target(
        frontmatter.get("target_binding_id", frontmatter.get("binding_id"))
    )
    session_binding_id = _declared_target(
        session.get("binding_id", session.get("conversation_binding_id"))
    )
    # A bound session requires the packet to carry a matching target_binding_id.
    # A generic (null-target) packet does not address this binding and must stay
    # unread (#95). Unbound sessions (no session_binding_id) keep today's
    # project/chat matching — a null-target packet still reaches them.
    if session_binding_id is not None and target_binding_id is None:
        return False, ROUTE_AMBIGUOUS_REASON
    if target_binding_id is not None and target_binding_id != session_binding_id:
        return False, ROUTE_AMBIGUOUS_REASON

    target_generation = _declared_target(
        frontmatter.get("target_binding_generation", frontmatter.get("binding_generation"))
    )
    session_generation = _declared_target(
        session.get("binding_generation", session.get("generation"))
    )
    if target_generation is not None and target_generation != session_generation:
        return False, STALE_GENERATION_REASON

    return True, "explicit_target_match"


def find_dispatchable_target_session(
    *,
    agent_id: str,
    project_id: str | None,
    chat_id: str | None,
    target_session_id: str | None,
    require_exact_scope: bool = False,
) -> dict[str, Any] | None:
    for session in iter_sessions(agent_id=agent_id):
        if require_exact_scope:
            if project_id is not None and session.get("project_id") != project_id:
                continue
            if chat_id is not None and session.get("chat_id") != chat_id:
                continue
        else:
            if project_id and session.get("project_id") not in {None, project_id}:
                continue
            if chat_id and session.get("chat_id") not in {None, chat_id}:
                continue
        if target_session_id and str(target_session_id) not in session_target_ids(session):
            continue
        dispatchable, _ = session_is_dispatchable(session)
        if dispatchable:
            return session
    return None


def resolve_exact_dispatch_target(
    project_id: str,
    chat_id: str,
    agent_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """The dispatchable session for an exact binding, or a refusal reason.

    Kept at two values because every existing caller unpacks two. A caller that also needs the
    BINDING it was validated against should use resolve_exact_dispatch_pair(): re-reading the
    path afterwards is a TOCTOU, and fields the binding owns and the session does not mirror --
    runtime_home above all -- cannot be recovered by any later cross-check.
    """
    pair, reason, _ = resolve_exact_dispatch_pair(project_id, chat_id, agent_id)
    return (pair[0] if pair else None), reason


def resolve_exact_dispatch_pair(
    project_id: str,
    chat_id: str,
    agent_id: str,
    sessions: list[dict[str, Any]] | None = None,
) -> tuple[
    tuple[dict[str, Any], dict[str, Any]] | None,
    str | None,
    tuple[dict[str, Any], dict[str, Any]] | None,
]:
    """Return the dispatchable pair, refusal reason, and validated expired pair.

    Hands back the exact binding snapshot this validation was performed against, so a caller
    never has to reopen the path to read a field the session does not carry. Introduced because
    codex_stream reopened it four times and a swap between validation and use redirected the
    endpoint; runtime_home is the field that makes a local cross-check impossible, since the
    session deliberately does not mirror it.
    """
    try:
        binding = load_binding(project_id, chat_id, agent_id)
    except FileNotFoundError:
        return None, EXACT_BINDING_REQUIRED_REASON, None

    if (
        binding.get("project_id") != project_id
        or binding.get("chat_id") != chat_id
        or binding.get("agent_id") != agent_id
    ):
        return None, EXACT_BINDING_MISMATCH_REASON, None

    bound_session_id = binding.get("session_id")
    bound_runtime_id = binding.get("runtime_session_id")
    bound_runtime_family = binding.get("runtime_family")
    if not bound_session_id or not bound_runtime_id:
        return None, EXACT_BINDING_REQUIRED_REASON, None

    # A caller resolving SEVERAL chats would otherwise rescan the whole session directory once
    # per chat; passing a pre-scanned list makes that one scan for the whole lookup. Defaults to
    # scanning, so every existing caller is unaffected.
    candidates = iter_sessions() if sessions is None else sessions
    exact_matches: list[dict[str, Any]] = []
    live_target_matches: list[dict[str, Any]] = []
    for session in candidates:
        runtime = runtime_metadata(session)
        if not bound_runtime_family or str(bound_runtime_family) != str(runtime.get("family")):
            continue
        if not runtime.get("session_id") or str(bound_runtime_id) != str(runtime.get("session_id")):
            continue
        if session.get("status") in {"active", "parked"}:
            live_target_matches.append(session)
        if session.get("agent_id") != agent_id:
            continue
        if session.get("project_id") != project_id or session.get("chat_id") != chat_id:
            continue
        if str(session.get("session_id")) != str(bound_session_id):
            continue
        exact_matches.append(session)

    if not exact_matches:
        return None, EXACT_BINDING_MISMATCH_REASON, None
    if len(exact_matches) > 1:
        return None, EXACT_BINDING_AMBIGUOUS_REASON, None

    session = exact_matches[0]
    dispatchable, refusal_reason = session_is_dispatchable(session)
    if not dispatchable and refusal_reason == "lease_expired":
        if len(live_target_matches) > 1:
            return None, EXACT_BINDING_AMBIGUOUS_REASON, None
        return None, EXACT_BINDING_NOT_DISPATCHABLE_REASON, (session, binding)
    if not dispatchable:
        return None, EXACT_BINDING_NOT_DISPATCHABLE_REASON, None
    return (session, binding), None, None


def message_targets_session(
    session: dict,
    message: dict,
    *,
    invocation_repo_targets: Any = None,
) -> tuple[bool, str]:
    frontmatter = message.get("frontmatter", {})
    repo_match, repo_reason = _session_repo_scope_matches(
        session, message, invocation_repo_targets
    )
    if not repo_match:
        return False, repo_reason
    if is_codex_self_target_message(message):
        return True, "explicit_target_match"
    exact_required = session_requires_exact_receive_target(session)
    target_session_id = frontmatter.get("target_session_id")
    if not target_session_id:
        if exact_required:
            return False, ROUTE_AMBIGUOUS_REASON
        return True, "broadcast_or_agent_scoped"
    if str(target_session_id) in session_target_ids(session):
        if exact_required:
            session_project = session.get("project_id")
            session_chat = session.get("chat_id")
            message_project = frontmatter.get("project_id")
            message_chat = frontmatter.get("chat_id")
            if not (
                session_project
                and session_chat
                and message_project
                and message_chat
                and session_project == message_project
                and session_chat == message_chat
            ):
                return False, ROUTE_AMBIGUOUS_REASON
            return binding_scoped_message_matches_session(
                session,
                message,
                invocation_repo_targets=invocation_repo_targets,
            )
        return True, "explicit_target_match"
    return False, "target_session_mismatch"


def matching_unread_messages(
    session: dict,
    *,
    invocation_repo_targets: Any = None,
    repo_scope_refusals: list[dict] | None = None,
    skip_paths: set[str] | None = None,
) -> list[dict]:
    messages = bounded_unread_messages(str(session["agent_id"]), skip_paths)
    project_id = session.get("project_id")
    chat_id = session.get("chat_id")
    if project_id:
        messages = [m for m in messages if m["frontmatter"].get("project_id") == project_id]
    if chat_id:
        messages = [m for m in messages if m["frontmatter"].get("chat_id") == chat_id]
    matched_messages: list[dict] = []
    for message in messages:
        # GH-539: a message whose repo-scope decision is already terminal is
        # skipped BEFORE the routing check, so the watcher stops re-doing the
        # work — not just re-logging it. Suppressing only the log left the cost
        # O(backlog) per poll.
        if skip_paths and message["path"] in skip_paths:
            continue
        repo_match, repo_reason = _session_repo_scope_matches(
            session, message, invocation_repo_targets
        )
        if not repo_match:
            if repo_scope_refusals is not None:
                # GH-539: carry the packet's routing inputs so a terminal-refusal
                # fingerprint can distinguish "same decision" from "packet rerouted".
                # Without these the fingerprint sees None and a corrected packet
                # stays suppressed under the same subscriber decision.
                repo_scope_refusals.append(
                    {
                        "path": message["path"],
                        "reason": repo_reason,
                        "packet_repo_targets": message["frontmatter"].get("repo_targets"),
                        "packet_project": message["frontmatter"].get("project_id"),
                        # GH-539: the mtime AS OBSERVED with this body. Sampling it
                        # later lets a rewrite between read and record store the new
                        # mtime against the old decision, so the corrected packet is
                        # skipped forever.
                        "packet_mtime": message.get("mtime"),
                    }
                )
            continue
        target_match, _ = message_targets_session(
            session,
            message,
            invocation_repo_targets=invocation_repo_targets,
        )
        if target_match:
            matched_messages.append(message)
    return matched_messages


def bounded_unread_messages(agent_id: str, skip_paths: set[str] | None = None) -> list[dict]:
    budget = ReadBudget(MAX_DISPATCH_INBOX_BYTES, "dispatch inbox")
    with active_read_budget(budget):
        raw = read_regular_file_bounded(
            agent_inbox_path(agent_id),
            budget.remaining,
        )
        inbox = json.loads(raw.decode("utf-8"))
        unread = inbox.get("unread") if isinstance(inbox, dict) else None
        if not isinstance(unread, list) or any(
            not isinstance(path, str)
            or not path
            or path != path.strip()
            or "\x00" in path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in unread
        ):
            raise ValueError("agent inbox must contain an unread path list")
        if len(unread) > MAX_DISPATCH_INBOX_ENTRIES:
            raise ValueError(
                f"agent inbox exceeds {MAX_DISPATCH_INBOX_ENTRIES} unread entries"
            )
        messages = []
        for relative_path in unread:
            # GH-539: drop terminally-refused paths BEFORE the body is opened, so
            # a large refusal backlog cannot exhaust MAX_DISPATCH_INBOX_BYTES
            # before an eligible packet is reached. Filtering after the read
            # skipped the routing work but still paid for every byte.
            if skip_paths and relative_path in skip_paths:
                continue
            try:
                # GH-539: bytes and identity from ONE descriptor. A separate
                # stat after the read can observe a rewritten file and would
                # certify content that was never parsed.
                message_raw, observed_mtime = read_regular_file_bounded_with_identity(
                    ROOT / relative_path,
                    min(MAX_DISPATCH_PACKET_BYTES, budget.remaining),
                )
            except FileNotFoundError as error:
                raise ValueError(
                    f"missing unread packet: {relative_path}"
                ) from error
            message_text = message_raw.decode("utf-8")
            closing = message_text.find("\n---", 4)
            if (
                not message_text.startswith("---\n")
                or closing < 0
                or message_text[closing + 4 : closing + 5] not in {"", "\n"}
            ):
                raise ValueError(f"malformed unread packet: {relative_path}")
            frontmatter, body = parse_frontmatter(message_text)
            messages.append(
                {
                    "path": relative_path,
                    "frontmatter": frontmatter,
                    "body": body,
                    "mtime": observed_mtime,
                }
            )
    return messages


def processed_messages(session: dict) -> set[str]:
    return set(session.get("processed_messages", []))


def message_needs_canonical_materialization(session: dict, message: dict) -> bool:
    if resolve_effective_action(session, message)[0] != "runtime_trigger":
        return False
    if livecraft_runtime_wake_available(session):
        return False
    frontmatter = message.get("frontmatter", {})
    return bool(
        session.get("binding_id")
        or session.get("conversation_binding_id")
        or frontmatter.get("target_binding_id")
        or frontmatter.get("binding_id")
    )


def processed_message_blocks_dispatch(session: dict, message: dict, seen: set[str]) -> bool:
    if message["path"] not in seen:
        return False
    return not (
        message_needs_canonical_materialization(session, message)
        and message["path"] not in canonical_settled_message_paths(session)
    )


def canonical_settled_message_paths(session: dict) -> set[str]:
    settled = session.get("canonical_settled_messages", {})
    if isinstance(settled, dict):
        return {str(path) for path in settled}
    return set()


def mark_message_processed(
    session: dict,
    message_path: str,
    *,
    prepared: tuple[dict, str] | None = None,
) -> None:
    if prepared is not None:
        save_session(session, prepared=prepared)
        return
    existing = session.get("processed_messages", [])
    if message_path not in existing:
        existing.append(message_path)
    session["processed_messages"] = existing
    save_session(session)


def mark_canonical_settlement_complete(session: dict, message_path: str, reason: str) -> None:
    existing = session.get("canonical_settled_messages", {})
    if not isinstance(existing, dict):
        existing = {}
    existing.setdefault(message_path, {"reason": reason})
    session["canonical_settled_messages"] = existing
    save_session(session)


def materialize_selected_runtime_packet(session: dict, message: dict) -> dict[str, Any]:
    _repo_package_root()
    materialization_module = importlib.import_module(
        "llm_collab.canonical.legacy_packet_materialization"
    )
    ledger_module = importlib.import_module(".".join(("llm_collab", "ledger")))

    workspace_id = config_get("workspace_id")
    if not workspace_id:
        return {
            "resolved": False,
            "reason": ROUTE_AMBIGUOUS_REASON,
            "detail": "workspace_id is missing",
        }
    try:
        paths = ledger_module.LedgerPaths.derive(project_state_root(), str(workspace_id))
        with ledger_module.LedgerStore.open_writer(paths) as store:
            return materialization_module.materialize_selected_legacy_packet(
                store,
                workspace_root=STATE_ROOT,
                session=session,
                message=message,
            )
    except materialization_module.LegacyPacketMaterializationRefused as exc:
        return {
            "resolved": False,
            "reason": exc.reason,
            "detail": exc.detail,
            "canonical_write_started": False,
        }
    except Exception as exc:
        return {
            "resolved": False,
            "reason": ROUTE_AMBIGUOUS_REASON,
            "detail": str(exc),
            "canonical_write_started": True,
        }


def claim_message_activation(session: dict, message: dict) -> tuple[bool, dict[str, Any] | None]:
    kind, detail = classify_activation(
        message.get("frontmatter", {}),
        target_agent=str(session["agent_id"]),
    )
    if kind == "none":
        return True, None
    if kind == "malformed":
        return False, {
            "event": "activation_refused",
            "message_path": message["path"],
            "reason": "malformed_activation",
            "detail": detail,
        }

    if session_is_watcher_only(session):
        # The app's background inbox watcher is the pickup path, and it claims the
        # activation under its own reader identity. A lease taken here under the
        # poller's PID is still live when that pickup runs, so the watcher is refused
        # (same_session_different_claimant) and the packet is stranded.
        return True, {
            "event": "activation_left_to_watcher",
            "message_path": message["path"],
            "reason": "claude_desktop_mailbox_watcher",
            "identity": detail,
        }

    from _activation_cleanup import claim_activation_lease
    from _activation_lease import LeaseRefused

    runtime_id = runtime_metadata(session).get("session_id")
    owner_pid = os.getpid()
    try:
        claim = claim_activation_lease(
            detail or {},
            owner_session_id=str(session["session_id"]),
            owner_pid=owner_pid,
            claimant_runtime_id=str(runtime_id) if runtime_id else None,
            takeover=True,
        )
    except LeaseRefused as exc:
        return False, {
            "event": "activation_refused",
            "message_path": message["path"],
            "reason": exc.reason,
            "owner": exc.owner,
            "identity": detail,
        }
    message["activation_lease"] = claim
    return True, {
        "event": "activation_claimed",
        "message_path": message["path"],
        "lease": claim["lease"],
        "fence_token": claim["fence_token"],
        "identity": claim["identity"],
        "poller_audit": claim["poller_audit"],
    }


def activation_claimant(session: dict) -> tuple[str | None, int]:
    runtime = runtime_metadata(session)
    runtime_id = runtime.get("session_id")
    return str(runtime_id) if runtime_id else None, os.getpid()


def activation_fenced_mutation(
    session: dict,
    message: dict,
    *,
    boundary: str,
    mutation,
) -> tuple[bool, dict[str, Any] | None, Any]:
    activation_lease = message.get("activation_lease")
    if not activation_lease:
        return True, None, mutation()

    from _activation_lease import LeaseRefused, with_lease_fence

    runtime_id, owner_pid = activation_claimant(session)
    try:
        result = with_lease_fence(
            activation_lease["identity"],
            owner_session_id=str(session["session_id"]),
            fence_token=int(activation_lease["fence_token"]),
            owner_pid=owner_pid,
            claimant_runtime_id=runtime_id,
            mutation=mutation,
        )
    except LeaseRefused as exc:
        return False, {
            "event": "activation_assert_refused",
            "message_path": message["path"],
            "boundary": boundary,
            "reason": exc.reason,
            "owner": exc.owner,
        }, None
    lease = activation_lease.get("lease") or {}
    return True, {
        "event": "activation_asserted",
        "message_path": message["path"],
        "boundary": boundary,
        "lease": {
            "lease_key": lease.get("lease_key"),
            "fence_token": activation_lease.get("fence_token"),
            "owner_session_id": activation_lease.get("owner_session_id"),
            "owner_runtime_session_id": activation_lease.get("owner_runtime_session_id"),
            "owner_pid": activation_lease.get("owner_pid"),
        },
    }, result


def is_codex_self_target_message(message: dict) -> bool:
    """Exclude every Codex self-target, including packets created before the guard."""
    frontmatter = message.get("frontmatter", {})
    return (
        frontmatter.get("from") == "codex"
        and frontmatter.get("to") == "codex"
    )


def should_skip_for_loop_protection(session: dict, message: dict) -> tuple[bool, str]:
    frontmatter = message.get("frontmatter", {})
    if is_codex_self_target_message(message):
        return True, "codex_self_target_thread_coordination"
    if frontmatter.get("autobridge_session_id") == session.get("session_id"):
        return True, "same_session_origin"
    if frontmatter.get("autobridge_hops", 0):
        return True, "message_already_autobridged"
    return False, "ok"


def resolve_effective_action(session: dict, message: dict) -> tuple[str, str]:
    mode = session.get("mode", "manual")
    wake_strategy = session.get("wake_strategy", "none")
    agent = get_agent(str(session["agent_id"]))
    activation_type = agent.get("activation", {}).get("type")
    runtime = runtime_metadata(session)
    runtime_command = runtime.get("command") if isinstance(runtime, dict) else None
    runtime_family = runtime.get("family")
    runtime_session_id = runtime.get("session_id")

    if mode == "manual":
        return "manual_noop", "manual_mode"
    if mode == "notify" or wake_strategy == "notify":
        return "notify_only", "notify_mode"
    if session_is_watcher_only(session):
        return "notify_only", "claude_desktop_mailbox_watcher"
    if wake_strategy == "runtime_trigger" and runtime_command:
        return "runtime_trigger", "runtime_command_available"
    if wake_strategy == "runtime_trigger" and runtime_family and runtime_session_id:
        return "runtime_trigger", "runtime_session_adapter_available"
    if activation_type == "human_relay":
        return "relay_prompt", "human_relay_fallback"
    if wake_strategy == "relay":
        return "relay_prompt", "relay_mode"
    if activation_type == "cli_session":
        return "notify_only", "cli_session_has_no_runtime_hook"
    if activation_type == "api_trigger":
        return "notify_only", "api_trigger_missing_runtime_command"
    return "notify_only", "unsupported_wake_strategy"


def build_runtime_payload(session: dict, message: dict) -> dict[str, Any]:
    fm = message["frontmatter"]
    runtime = runtime_metadata(session)
    return {
        "session": {
            "session_id": session["session_id"],
            "agent_id": session["agent_id"],
            "project_id": session.get("project_id"),
            "chat_id": session.get("chat_id"),
            "repo_targets": session.get("repo_targets"),
            "mode": session.get("mode"),
            "wake_strategy": session.get("wake_strategy"),
            "allowed_actions": session.get("allowed_actions", []),
            "runtime_family": runtime.get("family"),
            "runtime_session_id": runtime.get("session_id"),
            "runtime_session_source": runtime.get("session_source"),
            "runtime_home": runtime.get("home"),
        },
        "message": {
            "path": message["path"],
            "from": fm.get("from"),
            "to": fm.get("to"),
            "title": fm.get("title"),
            "project_id": fm.get("project_id"),
            "chat_id": fm.get("chat_id"),
            "priority": fm.get("priority"),
            "related_task": fm.get("related_task"),
            "sender_session_id": fm.get("sender_session_id"),
            "sender_agent_id": fm.get("sender_agent_id", fm.get("from")),
            "target_session_id": fm.get("target_session_id"),
            "supersedes_session_id": fm.get("supersedes_session_id"),
            "body": message.get("body", ""),
        },
        "activation_lease": message.get("activation_lease"),
    }


def _reply_channel_lines(session: dict, fm: dict) -> list[str]:
    """Name the channel. Do NOT emit a runnable reply command yet.

    A copyable command was tried and withdrawn (connector + codex review of #317). Two
    defects made it premature, and both need contracts that do not exist:

    - It carried no --target-session-id or --sender-session-id, because the incoming
      sender_session_id is not authoritative. A delayed reply therefore resolved
      whichever binding was current when it ran, and could wake a rebound runtime that
      never saw the request. The validated return address (`reply_to_*`) fixes this.
    - The generated packet carried no autobridge_session_id or autobridge_hops marker,
      so `should_skip_for_loop_protection` returned (False, "ok") for it and two
      runtime-triggered workers following the instruction could wake each other
      indefinitely.

    What survives is the part that fixed the observed failure: a worker was answering
    review handoffs on the pull request, where the sender never sees them. Telling it
    which channel to use needs no new contract. Telling it the exact invocation does.
    """
    return [
        "When the packet explicitly requests a substantive response, send it through",
        "the mailbox -- `deliver.py` -- addressed to the sender above.",
        "Do not send another mailbox message for a packet that is itself only a reply",
        "or delivery receipt. The runtime thread gets the terse delivery receipt.",
        "A PR comment, a code-review body or a desktop nudge does NOT reach the sender.",
        "Post to a PR only when the PR itself is the artifact -- a connector review",
        "request, or evidence a human will read there -- and deliver the packet as well.",
    ]


def build_resume_prompt(session: dict, message: dict) -> str:
    fm = message["frontmatter"]
    activation_lease = message.get("activation_lease")
    lines = [
        "You are resuming a registered llm-collab worker session for one bounded action.",
        "Read the packet at `message_path` below. Do not answer it in this runtime thread.",
        "",
        f"llm_collab_session_id: {session['session_id']}",
        f"agent_id: {session['agent_id']}",
        f"sender_agent_id: {fm.get('sender_agent_id', fm.get('from', ''))}",
        f"sender_session_id: {fm.get('sender_session_id', '')}",
        f"target_session_id: {fm.get('target_session_id', '')}",
        f"chat_id: {fm.get('chat_id', '')}",
        f"project_id: {fm.get('project_id', '')}",
        f"title: {fm.get('title', '')}",
        f"message_path: {ROOT / str(message.get('path', ''))}",
    ]
    if activation_lease:
        identity = activation_lease.get("identity") or {}
        lease = activation_lease.get("lease") or {}
        lines.extend(
            [
                f"activation_lease_key: {lease.get('lease_key', '')}",
                f"activation_fence_token: {activation_lease.get('fence_token', '')}",
                f"activation_owner_session_id: {activation_lease.get('owner_session_id', '')}",
                f"activation_project: {identity.get('project', '')}",
                f"activation_chat: {identity.get('chat', '')}",
                f"activation_task: {identity.get('task', '')}",
                f"activation_worktree: {identity.get('worktree', '')}",
                f"activation_branch: {identity.get('branch', '')}",
                f"activation_target_agent: {identity.get('target_agent', '')}",
                "Before mutating protected lane state, assert this exact activation lease fence.",
            ]
        )
    if activation_lease:
        lines.extend(
            [
                "",
                "Activation packet body:",
                message.get("body", "").strip() or "(no body)",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "The packet body is not copied here. Open `message_path` directly, read-only.",
                "If you use `inbox.py` instead, use `--peek` so reading does not acknowledge it.",
            ]
        )
    lines.extend(
        [
            "",
            *_reply_channel_lines(session, fm),
            "",
            "Do not put substantive work results in this runtime thread.",
            "Do not start unrelated work.",
        ]
    )
    return "\n".join(lines)


def build_app_server_ring_prompt(session: dict, message: dict) -> str:
    fm = message["frontmatter"]
    return build_packet_ring_prompt(
        str(fm.get("from", fm.get("sender_agent_id", ""))),
        str(session["agent_id"]),
        str(fm.get("chat_id", "")),
        Path(str(message.get("path", ""))).name,
    )


def derived_runtime_command(session: dict, message: dict) -> list[str] | None:
    runtime = runtime_metadata(session)
    runtime_family = runtime.get("family")
    runtime_session_id = runtime.get("session_id")
    if not runtime_family or not runtime_session_id:
        return None
    if session_is_watcher_only(session):
        return None
    prompt = build_resume_prompt(session, message)
    binary = runtime_binary(str(runtime_family))

    if runtime_family == "codex_app":
        return [
            binary,
            "exec",
            "resume",
            str(runtime_session_id),
            prompt,
            "--json",
            "--skip-git-repo-check",
        ]
    if runtime_family == "claude_app":
        return [
            binary,
            "-p",
            "--output-format",
            "json",
            "--resume",
            str(runtime_session_id),
            prompt,
        ]
    if runtime_family == "gemini_cli":
        return [
            binary,
            "--prompt",
            prompt,
            "--resume",
            str(runtime_session_id),
            "--output-format",
            "json",
        ]
    return None


LISTEN_FLAG = "--listen"


def command_is_app_server_invocation(command: str) -> bool:
    """True only for `<executable> [options] app-server [options] --listen ...`.

    Not the executable's FILENAME: LLM_COLLAB_CODEX_BIN accepts any path, so a wrapper,
    symlink or versioned binary launches correctly and a literal "codex app-server" match
    would never find it.

    But not a substring either. `app-server in command` admitted
    `worker-app-server-proxy --listen ...` and `worker --label app-server --listen ...`, and
    discover_codex_app_server takes the FIRST matching endpoint -- so a false positive here
    points delivery at somebody else's socket, which is worse than the false negative it was
    fixing. Both parts must be exact argv tokens, and `app-server` must sit in the
    SUBCOMMAND position: the first token after the executable that is not an option or an
    option's value.
    """
    tokens = command.split()
    if len(tokens) < 3:
        return False
    if not any(token == LISTEN_FLAG or token.startswith(f"{LISTEN_FLAG}=")
               for token in tokens[1:]):
        return False
    if not executable_is_a_codex_binary(tokens[0]):
        return False
    return app_server_is_the_subcommand(tokens)


def executable_is_a_codex_binary(executable: str) -> bool:
    """Whether this argv's executable can plausibly be the Codex CLI.

    Needed because NEITHER guess about unknown options is safe on its own. Treating an
    unrecognized flag as value-taking swallows the subcommand, so
    `codex --strict-config app-server ...` disappears. Treating it as valueless admits
    `worker --label app-server ...`, because `app-server` then lands in the subcommand slot.
    The option table cannot resolve that; the executable can.

    Deliberately weaker than the literal-name match this replaced: the CONFIGURED binary is
    accepted whatever it is called, which is the point of LLM_COLLAB_CODEX_BIN, and otherwise
    the basename must contain `codex` -- covering `codex-cli`, `codex-0.146.0` and wrappers
    named for what they wrap. A wrapper named neither is found only by configuring it, which
    is a documented knob rather than a silent failure.
    """
    configured = os.environ.get("LLM_COLLAB_CODEX_BIN")
    if configured and executable == configured:
        return True
    return "codex" in executable.rsplit("/", 1)[-1].lower()


def app_server_is_the_subcommand(tokens: list[str]) -> bool:
    """Whether `app-server` occupies the subcommand slot of this argv.

    Global options before the subcommand are skipped, and the ones that consume a following
    token are ENUMERATED rather than guessed. An earlier version assumed every unrecognized
    option was value-taking, which swallowed the subcommand after ordinary flags: valid
    invocations like `codex --strict-config app-server --listen ...`, and the same with
    `--oss`, `--search` or `--dangerously-bypass-approvals-and-sandbox`, made a reachable
    server vanish from discovery.

    Guessing in the other direction is the safer default. An unknown flag treated as
    valueless leaves the subcommand where it is; an unknown flag treated as value-taking eats
    it. The only cost is that a FUTURE value-taking option whose value is a bare word would
    hide that one invocation, which a test against the installed CLI is there to catch.
    """
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return token == "app-server"
        if "=" in token:
            index += 1  # --flag=value is one token
        elif token in VALUE_TAKING_GLOBAL_OPTIONS:
            index += 2  # the option and its separate value
        else:
            index += 1  # a flag
    return False


# Global options that consume a following token, transcribed from `codex --help` (every entry
# there carrying a `<VALUE>` placeholder). test_option_table_matches_the_installed_cli keeps
# this honest against the binary rather than against my memory of it -- the whole family of
# defects in this concern came from encoding a rule about a value instead of reading it.
VALUE_TAKING_GLOBAL_OPTIONS = frozenset({
    "-c", "--config",
    "--enable", "--disable",
    "--remote", "--remote-auth-token-env",
    "-i", "--image",
    "-m", "--model",
    "--local-provider",
    "-p", "--profile",
    "-s", "--sandbox",
    "-C", "--cd",
    "--add-dir",
    "-a", "--ask-for-approval",
})


def codex_app_server_process_rows() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ps", "eww", "-axo", "pid=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if not pid_text.isdigit():
            continue
        if not command_is_app_server_invocation(command):
            continue
        rows.append({"pid": int(pid_text), "command": command})
    return rows


def _extract_arg_value(command: str, flag: str) -> str | None:
    patterns = [
        rf"{re.escape(flag)}=([^\s]+)",
        rf"{re.escape(flag)}\s+([^\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, command)
        if match:
            return match.group(1).strip("'\"")
    return None


def discover_codex_app_server(
    runtime_home: str | None,
    *,
    allow_unscoped_env: bool = True,
) -> dict[str, Any] | None:
    """Find the App Server for `runtime_home`.

    LLM_COLLAB_CODEX_APP_SERVER_URL is a WORKSPACE-WIDE override with no home or project in it, so
    it wins for every home -- fine for dispatch, wrong for a project-aware reader: selecting a valid
    binding under a secondary CODEX_HOME still connected to the primary server, where thread/resume
    either fails or observes an unrelated matching thread. A caller that must stay inside one
    project passes allow_unscoped_env=False and gets home-based discovery only. Default True keeps
    every existing caller unchanged.
    """
    configured_url = os.environ.get("LLM_COLLAB_CODEX_APP_SERVER_URL")
    configured_token_file = os.environ.get("LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE")
    if configured_url and allow_unscoped_env:
        return {
            "url": configured_url,
            "token_file": configured_token_file,
            "source": "env",
            "pid": None,
        }

    if not runtime_home:
        return None
    marker = f"CODEX_HOME={runtime_home}"
    for row in codex_app_server_process_rows():
        command = row["command"]
        if not re.search(rf"(^|\s){re.escape(marker)}(\s|$)", command):
            continue
        listen_url = _extract_arg_value(command, "--listen")
        if not listen_url or not listen_url.startswith("ws://"):
            continue
        return {
            "url": listen_url,
            "token_file": _extract_arg_value(command, "--ws-token-file"),
            "source": "process",
            "pid": row["pid"],
        }
    return None


def _socket_read_exact(sock: socket.socket, count: int, client=None) -> bytes:
    """Read exactly `count` bytes.

    When a client is supplied, the absolute deadline is checked and the socket re-clamped before
    EVERY recv. Without that, one frame whose payload arrives a byte at a time is unbounded no
    matter what the caller checked beforehand: a 50ms deadline returned a frame after 795ms,
    because each recv succeeded inside its own timeout and nothing looked at the clock in between.
    Bounding a loop means checking inside it.

    Every call site is mutation-proved: dropping the client from the header, either extended-length
    branch, the mask or the payload read all fail, as does removing this check.

    I first claimed the fixed-size reads were unprovable, reasoning that a 2/8/4-byte read always
    completes inside the single timeout recv_json has already installed. That is the same wrong
    model as the bug: a socket timeout restarts on every recv() and never bounds the total, which
    is exactly why checking once outside the loop was insufficient. A fixed-size read iterates too
    whenever the peer fragments it -- two bytes 40ms apart under a 50ms deadline raise here at
    ~56ms, and return successfully at ~90ms without this check.
    """
    chunks: list[bytes] = []
    remaining = count
    # Anything the handshake over-read belongs to the first frames, so it is consumed before the
    # socket is touched again. Dropping it lost the first notification whenever the peer's upgrade
    # response and first frame shared a TCP segment.
    if client is not None and client._buffered_bytes:
        taken = client._buffered_bytes[:remaining]
        client._buffered_bytes = client._buffered_bytes[len(taken):]
        chunks.append(taken)
        remaining -= len(taken)
    while remaining > 0:
        if client is not None:
            client._check_deadline("while reading a frame")
            sock.settimeout(client.remaining_wait())
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("websocket connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# How a connection answers a server-initiated request. The policy is explicit because it
# depends on the connection's ROLE, not on the protocol: App Server broadcasts one pending
# request to every subscribed connection and the first response -- result OR error -- can
# resolve it. A connection that OWNS a turn must answer, or the turn hangs. A connection that
# merely observes must not, or it races the operator's own UI and can refuse work they
# started. There is no correct global default, so each caller states its role.
# A ceiling on the HTTP upgrade header, charged cumulatively as chunks arrive. Without it the
# handshake loop grows `response` until the terminator appears -- and a peer that never sends one
# grows it forever: 1024 full 4 KiB chunks were accepted, 4 MiB, stopping only when the peer closed.
# A per-recv timeout cannot bound this because every successful chunk resets it, and the observer
# runs with no absolute deadline by default. Real upgrade headers are a few hundred bytes.
MAX_HANDSHAKE_HEADER_BYTES = 64 * 1024


def handshake_header_bytes(response: bytes) -> int:
    """How many bytes of `response` are HTTP header, i.e. up to and including the terminator.

    The ceiling applies to header bytes. Charging the whole buffer rejected a valid header just under
    the limit whenever a coalesced first frame pushed the chunk over, while the code that follows
    correctly treats those same trailing bytes as frame data. The header is what an unbounded peer
    can grow; the frame after it is bounded elsewhere.

    A named function because this is arithmetic, and arithmetic should be tested as arithmetic: the
    socket-level test for it passed against the vulnerable code depending on where the kernel split
    the read, which is not a property any test should rest on.
    """
    terminator = response.find(b"\r\n\r\n")
    return len(response) if terminator < 0 else terminator + 4
SERVER_REQUEST_REFUSE = "refuse"
SERVER_REQUEST_IGNORE = "ignore"
SERVER_REQUEST_POLICIES = (SERVER_REQUEST_REFUSE, SERVER_REQUEST_IGNORE)
JSONRPC_METHOD_NOT_FOUND = -32601


class JsonRpcWebSocketClient:
    def __init__(
        self,
        url: str,
        token: str | None = None,
        timeout_seconds: int = 30,
        server_request_policy: str = SERVER_REQUEST_REFUSE,
        max_frame_bytes: int | None = None,
    ):
        if server_request_policy not in SERVER_REQUEST_POLICIES:
            raise ValueError(f"unknown server_request_policy: {server_request_policy!r}")
        self.url = url
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.server_request_policy = server_request_policy
        # An optional ceiling on ONE frame's declared payload length. None keeps the historical
        # behaviour for existing callers. Without it, the peer's own 64-bit length field is trusted
        # all the way into recv(): a frame advertising 1 TiB and sending no payload reached
        # recv(1099511627776) here, and any budget applied to the DECODED message is charged too
        # late to matter. A length is a claim, and this is where the claim is first believed.
        self.max_frame_bytes = max_frame_bytes
        # Bytes already pulled off the socket but not yet consumed. The handshake is read with
        # recv(4096), so when the peer's upgrade response and its first frame land in one segment,
        # those frame bytes arrive in the SAME read -- and everything after the \r\n\r\n used to be
        # discarded, silently losing the first frame. For a subscriber that frame is turn/started.
        # Whether it happens at all depends on TCP coalescing, which is why it looked flaky.
        self._buffered_bytes = b""
        self.sock: socket.socket | None = None
        self.counter = 0
        self.server_requests: list[str] = []
        # An optional ABSOLUTE instant after which no blocking operation may still be waiting.
        # None keeps the historical behaviour exactly: every wait gets the full timeout_seconds.
        # A per-call timeout cannot bound a handshake, because a peer trickling bytes resets it
        # on every chunk -- only an absolute limit bounds the sum.
        self.read_deadline: float | None = None

    def set_deadline(self, deadline: float | None) -> None:
        """The absolute monotonic instant after which no blocking wait may continue."""
        self.read_deadline = deadline

    def remaining_wait(self) -> float:
        """How long one blocking operation may wait: the timeout, capped by the deadline."""
        cap = float(self.timeout_seconds or 30)
        if self.read_deadline is None:
            return cap
        return max(0.01, min(cap, self.read_deadline - time.monotonic()))

    def _check_deadline(self, where: str) -> None:
        if self.read_deadline is not None and time.monotonic() >= self.read_deadline:
            raise TimeoutError(f"read deadline reached {where}")

    def __enter__(self) -> "JsonRpcWebSocketClient":
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
            raise ValueError(f"Unsupported Codex app-server websocket URL: {self.url}")
        self._check_deadline("before connecting")
        sock = socket.create_connection((parsed.hostname, parsed.port),
                                        timeout=self.remaining_wait())
        # Everything after the connect must close the socket on ANY failure. Leaving it to the
        # caller leaked a connected socket whenever sendall, recv or parsing raised before
        # self.sock was assigned -- there was nothing for __exit__ to close.
        try:
            sock.settimeout(self.remaining_wait())
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            headers = [
                f"GET {path} HTTP/1.1",
                f"Host: {parsed.hostname}:{parsed.port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            if self.token:
                headers.append(f"Authorization: Bearer {self.token}")
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response:
                # Checked per chunk, not per byte-budget: a peer sending the header in pieces
                # resets a per-call timeout on every piece, so only this bounds the total.
                self._check_deadline("during the websocket handshake")
                sock.settimeout(self.remaining_wait())
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("websocket handshake failed")
                response += chunk
                # The ceiling applies to HTTP HEADER bytes, so it is measured only up to the
                # terminator. Charging the whole buffer rejected a valid header just under the limit
                # whenever a coalesced first frame pushed the chunk over it -- while the code just
                # below correctly treats those same trailing bytes as frame data. The header is what
                # an unbounded peer can grow; the frame after it is bounded elsewhere.
                if handshake_header_bytes(response) > MAX_HANDSHAKE_HEADER_BYTES:
                    raise ConnectionError(
                        f"websocket handshake header exceeded "
                        f"{MAX_HANDSHAKE_HEADER_BYTES} bytes with no end of headers; refusing"
                    )
            header_text = response.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
            if " 101 " not in header_text.splitlines()[0]:
                raise ConnectionError(
                    f"websocket handshake failed: {header_text.splitlines()[0]}")
            expected_accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
            ).decode("ascii")
            if expected_accept not in header_text:
                raise ConnectionError("websocket handshake failed: invalid accept header")
            # Keep whatever followed the terminator; it is frame data, not handshake data.
            self._buffered_bytes = response.split(b"\r\n\r\n", 1)[1]
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        return self

    def __exit__(self, *_: object) -> None:
        if self.sock:
            try:
                self._send_frame(b"", opcode=0x8)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if not self.sock:
            raise ConnectionError("websocket is not connected")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.extend([0x80 | 126, (length >> 8) & 0xFF, length & 0xFF])
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        # Sending blocks too. A peer that completes the upgrade and then stops reading fills the
        # send buffer, and sendall inherits whatever timeout the last read happened to leave -- so
        # the absolute deadline did not bound it: a 50ms budget took over 3s against a blocked
        # peer. sendall applies its timeout to the whole send, so clamping it once is enough; no
        # custom send loop is needed here, unlike the receive path.
        self._check_deadline("while sending")
        self.sock.settimeout(self.remaining_wait())
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        if not self.sock:
            raise ConnectionError("websocket is not connected")
        first, second = _socket_read_exact(self.sock, 2, self)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(_socket_read_exact(self.sock, 2, self), "big")
        elif length == 127:
            length = int.from_bytes(_socket_read_exact(self.sock, 8, self), "big")
        # Checked after the length is decoded and BEFORE any byte of mask or payload is read, so a
        # refusal costs nothing and an absurd claim is never handed to recv().
        if self.max_frame_bytes is not None and length > self.max_frame_bytes:
            raise ConnectionError(
                f"frame declares {length} payload bytes, above the {self.max_frame_bytes} byte "
                "limit for this connection; refusing before reading it"
            )
        mask = _socket_read_exact(self.sock, 4, self) if masked else b""
        payload = _socket_read_exact(self.sock, length, self) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def recv_json(self) -> dict[str, Any]:
        while True:
            self._check_deadline("while reading")
            if self.sock is not None:
                self.sock.settimeout(self.remaining_wait())
            opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise ConnectionError("websocket closed")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0x1:
                # A JSON-RPC message is an object. Reject a valid-JSON non-object
                # frame ([]) as ValueError HERE, before _handle_server_request
                # calls .get -- otherwise it raised AttributeError past every
                # catch and left an already-accepted packet redeliverable
                # (GH-94). Malformed JSON / bad UTF-8 already raise ValueError
                # subclasses (JSONDecodeError / UnicodeDecodeError); the
                # post-accept loop catches ValueError, so every read path is
                # uniform without re-wrapping them here.
                message = json.loads(payload.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("websocket text frame is not a JSON-RPC object")
                if self._handle_server_request(message):
                    continue
                return message

    def _handle_server_request(self, message: dict[str, Any]) -> bool:
        """True when this message was a server request and has been dealt with here.

        Centralised so that EVERY read path is covered -- the request/response wait and any
        caller's own event loop alike. Two separate branches used to answer these with
        `{"result": {}}`: one while waiting for a response, one in the dispatch turn loop.
        Every member of the generated ServerRequest union is authority- or data-bearing
        (command/file/permission approvals, tool calls, user input, MCP elicitation, auth
        refresh, attestation) and NO Response schema in the bundle permits an empty object,
        so that envelope was unauthorized on its face.

        `is not None`, never truthiness: 0 is a legal JSON-RPC id, and under truthiness a
        request with id 0 fell through as if it were a notification.
        """
        if message.get("id") is None or not message.get("method"):
            return False
        method = str(message["method"])
        self.server_requests.append(method)
        if self.server_request_policy == SERVER_REQUEST_REFUSE:
            self.send_json({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {
                    "code": JSONRPC_METHOD_NOT_FOUND,
                    "message": ("llm-collab automatic dispatch cannot authorize server "
                                f"requests; {method} refused"),
                },
            })
        return True

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send_json({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        numeric_id: bool = False,
        jsonrpc: bool = True,
    ) -> Any:
        self.counter += 1
        request_id: str | int = self.counter if numeric_id else f"llm-collab-{self.counter}"
        payload: dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
        if jsonrpc:
            payload["jsonrpc"] = "2.0"
        self.send_json(payload)
        while True:
            message = self.recv_json()
            if message.get("id") == request_id:
                if message.get("error"):
                    error = message["error"]
                    raise RuntimeError(f"{method}: {error.get('message', 'unknown error')}")
                return message.get("result")
            # Server requests never reach here: recv_json() applies this connection's
            # policy before returning. This loop only correlates responses.


# Mirrors TOKEN_BLANK_CHARS in bin/pm2_watchers.py. Python's default strip() removes the
# ASCII information separators, which the CLI keeps, so reading the secret with the default
# turned a token the gate had approved into an empty string and sent no credential at all.
TOKEN_BLANK_CHARS = (
    "\t\n\v\f\r \u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def usable_token(content: str) -> str | None:
    """The sendable secret in this content, or None when there is not one.

    One strip and one predicate, in one place. Every character of the stripped secret must be
    printable ASCII: a BOM cannot be encoded in an HTTP header, U+001C and friends are not
    sendable, and an INTERIOR CRLF would split the header itself. The PM2 gates enforce this,
    but a server started or discovered outside those gates reaches this reader instead, so the
    contract holds here rather than being assumed upstream.

    Returning the value rather than a boolean matters: an earlier version stripped once to
    decide and again to return, and the second strip could not be proven by any test because
    the decision had already excluded every input the two strips disagree about. Dead
    distinctions in a credential path are worth removing rather than documenting.
    """
    stripped = content.strip(TOKEN_BLANK_CHARS)
    if not stripped:
        return None
    if not all("\x21" <= character <= "\x7e" for character in stripped):
        return None
    return stripped


def token_is_usable(content: str) -> bool:
    return usable_token(content) is not None


# A bearer token is a short opaque string; the surrounding gates cap every other untrusted read, and
# this one reached read_bytes() with no ceiling, so a corrupted or hostile --ws-token-file could
# exhaust memory before the socket was even opened. Generous next to any real token.
MAX_TOKEN_FILE_BYTES = 64 * 1024


class UnreadableFile(RuntimeError):
    """An oversized, non-regular, or I/O-failed path. Distinct from absent, deliberately."""


# An optional budget charged by EVERY bounded read while a lookup is active, including the binding
# reads that happen inside resolve_exact_dispatch_pair() where the caller has no reach. Set and
# cleared by the caller around one lookup; None means no cumulative accounting, which is the
# historical behaviour for every other caller.
_ACTIVE_READ_BUDGET: list = []


class ReadBudget:
    def __init__(self, limit: int, label: str = "exact-session read") -> None:
        self.limit = limit
        self.label = label
        self.spent = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.spent

    def charge(self, count: int, path: Path) -> None:
        self.spent += count
        if self.spent > self.limit:
            raise UnreadableFile(
                f"{self.label} exceeds {self.limit} bytes at {path}"
            )


class active_read_budget:
    """Charge every bounded read in this block to one cumulative budget."""

    def __init__(self, budget) -> None:
        self.budget = budget

    def __enter__(self):
        _ACTIVE_READ_BUDGET.append(self.budget)
        return self.budget

    def __exit__(self, *exc) -> bool:
        _ACTIVE_READ_BUDGET.pop()
        return False


def read_regular_file_bounded_with_identity(path: Path, limit: int) -> tuple[bytes, float | None]:
    """Bytes plus the mtime from the SAME descriptor that produced them (GH-539).

    read()-then-stat() is two operations on two possibly-different objects: a rewrite
    landing between them yields metadata describing bytes the caller never parsed.
    Anything that records "this content had this identity" must take both from one
    descriptor, which is what the fstat below already does.
    """
    identity: dict = {}
    payload = read_regular_file_bounded(path, limit, _identity_out=identity)
    return payload, identity.get("mtime")


def read_regular_file_bounded(path: Path, limit: int, *, _identity_out: dict | None = None) -> bytes:
    """Read at most `limit` bytes from a REGULAR file, without ever blocking on open().

    Every untrusted read in this codebase needs the same four things and gets them wrong
    individually: a non-blocking open (a writer-less FIFO blocks forever INSIDE open(), before any
    byte cap or deadline can apply), an fstat on that SAME descriptor (stat-then-reopen can resolve
    two different objects), a regular-file requirement, and a read LOOP (one os.read may return
    short and silently truncate). Each was fixed separately for the token file and then not applied
    to its siblings, so this exists to make the next call site correct by construction.

    Raises FileNotFoundError when absent, UnreadableFile for every other refusal.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise UnreadableFile(f"cannot open {path}: {error}") from error
    try:
        info = os.fstat(descriptor)
        if _identity_out is not None:
            # Same descriptor, same object: this mtime describes exactly the bytes
            # returned below.
            _identity_out["mtime"] = info.st_mtime
        if not stat.S_ISREG(info.st_mode):
            raise UnreadableFile(f"{path} is not a regular file; refusing to read it")
        if info.st_size > limit:
            raise UnreadableFile(f"{path} exceeds the {limit} byte limit; refusing to parse it")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as error:
        raise UnreadableFile(f"cannot read {path}: {error}") from error
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > limit:
        raise UnreadableFile(f"{path} exceeds the {limit} byte limit; refusing to parse it")
    if _ACTIVE_READ_BUDGET:
        _ACTIVE_READ_BUDGET[-1].charge(len(raw), path)
    return raw


def _codex_app_server_token(token_file: str | None) -> str | None:
    """Read the bearer token with exactly the semantics the gate validated.

    The gate accepts a token, then this reads it: if the two disagree about what counts as
    blank -- or about what is sendable at all -- an approved token becomes an empty or
    unsendable credential here while the transport still reports as configured.
    """
    if not token_file:
        return None
    path = Path(token_file).expanduser()
    # O_NONBLOCK matters before the read cap does: opening a FIFO with no writer BLOCKS FOREVER, and
    # that happens inside open(), so a byte limit that applies afterwards never runs. Both callers
    # load the token before the socket and the deadline exist, so --seconds cannot interrupt it
    # either. A bounded read on an unbounded open is not a bound.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        # fstat on the SAME descriptor, never stat-then-reopen: the second lookup could land on a
        # different file. Only a regular file can be a token; a FIFO, device or directory is not one
        # regardless of what it would yield.
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > MAX_TOKEN_FILE_BYTES:
            return None
        # os.read() is ONE syscall and may return fewer bytes than asked for on a perfectly
        # healthy regular file. A short read here does not fail -- it silently TRUNCATES the
        # credential, and a truncated token is still printable ASCII, so usable_token() accepts
        # it and both callers send a wrong secret the launch gate never approved. The previous
        # Path.open().read(n) looped internally; swapping to a raw descriptor lost that for free.
        chunks: list[bytes] = []
        remaining = MAX_TOKEN_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_TOKEN_FILE_BYTES:
        # Oversize reports as "no usable token", exactly like an unreadable one: both callers treat
        # None that way and the connection then fails on the server's own rejection, which is a
        # clear failure rather than a silent one. Changing the return contract instead would repeat
        # the mistake of altering a shared function's failure mode for one caller's benefit.
        return None
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return usable_token(content)


def _extract_default_codex_model(models_payload: Any) -> str | None:
    models = models_payload.get("data") if isinstance(models_payload, dict) else models_payload
    if not isinstance(models, list):
        return None
    for model in models:
        if isinstance(model, dict) and model.get("isDefault") and model.get("id"):
            return str(model["id"])
    for model in models:
        if isinstance(model, dict) and model.get("id"):
            return str(model["id"])
    return None


def execute_codex_app_server_trigger(session: dict, message: dict, runtime_home: str | None) -> dict[str, Any] | None:
    runtime = runtime_metadata(session)
    endpoint = discover_codex_app_server(runtime_home)
    if endpoint is None:
        return None

    timeout_seconds = int(runtime.get("timeout_seconds", 180))
    prompt = (
        build_resume_prompt(session, message)
        if message.get("activation_lease")
        else build_app_server_ring_prompt(session, message)
    )
    runtime_session_id = str(runtime["session_id"])
    token = _codex_app_server_token(endpoint.get("token_file"))
    notifications: list[str] = []
    assistant_text = ""
    terminal: dict[str, Any] | None = None

    with JsonRpcWebSocketClient(str(endpoint["url"]), token=token, timeout_seconds=timeout_seconds) as client:
        client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "llm-collab-session-autobridge", "version": "0.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        client.notify("initialized")
        client.request("thread/resume", {"threadId": runtime_session_id})
        model = (
            str(runtime.get("model"))
            if runtime.get("model")
            else os.environ.get("LLM_COLLAB_CODEX_MODEL")
        )
        if not model:
            model = _extract_default_codex_model(client.request("model/list", {}))
        turn_payload: dict[str, Any] = {
            "threadId": runtime_session_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if model:
            turn_payload["model"] = model
        # turn/start acceptance is the delivery boundary: once this returns, the
        # recipient HAS the packet. Anything that raises at or before this line
        # is a PRE-acceptance failure and stays retryable (it propagates out of
        # the `with`). Everything after is delivered — never retried, never
        # allowed to fall through to AX or another thread (GH-94 routing guard).
        started = client.request("turn/start", turn_payload)
        started_turn = started.get("turn") if isinstance(started, dict) else None
        started_turn_id = started_turn.get("id") if isinstance(started_turn, dict) else None
        deadline = time.monotonic() + timeout_seconds
        # Bound every post-acceptance read by the ABSOLUTE deadline. Without
        # this, _socket_read_exact resets to the full per-call timeout on each
        # chunk, so a peer trickling bytes could hold recv_json forever and stall
        # the whole inbox watcher. Mirrors the canonical delivery transport.
        client.set_deadline(deadline)
        while time.monotonic() < deadline:
            try:
                message_payload = client.recv_json()
            except (TimeoutError, OSError, ValueError):
                # The reply view is lost after acceptance: deadline hit inside
                # recv, dropped connection/peer reset (TimeoutError/OSError), or
                # a malformed/non-object peer frame normalized to ValueError at
                # the recv_json parse boundary. Delivered-but-unobserved; stop
                # observing, never raise, never retry (GH-94). recv_json now
                # guarantees a dict on success, so no in-loop type guard.
                break
            method = str(message_payload.get("method", ""))
            if not method:
                continue
            notifications.append(method)
            params = message_payload.get("params")
            # Only attribute output/terminals to OUR turn. A resumed thread can
            # buffer notifications for an earlier or concurrent turn; accepting
            # the first terminal blindly reported the wrong turn's status/output
            # (mirrors llm_collab/canonical/codex_delivery.py turn-id check).
            if started_turn_id is not None and isinstance(params, dict):
                frame_turn = params.get("turn") if isinstance(params.get("turn"), dict) else None
                frame_turn_id = params.get("turnId") or (frame_turn.get("id") if frame_turn else None)
                if frame_turn_id is not None and frame_turn_id != started_turn_id:
                    continue
            item = params.get("item") if isinstance(params, dict) else None
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = str(item.get("text", ""))
                if method == "item/agentMessage/delta":
                    assistant_text += text
                elif text:
                    assistant_text = text
            if method.lower() in {"turn/completed", "turn/failed", "turn/cancelled"}:
                terminal = params if isinstance(params, dict) else {"raw": params}
                break

    turn = terminal.get("turn") if isinstance(terminal, dict) else None
    status = turn.get("status") if isinstance(turn, dict) else None
    error = turn.get("error") if isinstance(turn, dict) else None
    delivery_observed = terminal is not None
    terminal_status = status if delivery_observed else "unobserved"
    return {
        "command": ["codex-app-server", str(endpoint["url"]), "turn/start", runtime_session_id],
        "derived_command": True,
        "adapter": "codex_app_server",
        "app_server": {
            "url": endpoint["url"],
            "pid": endpoint.get("pid"),
            "source": endpoint.get("source"),
        },
        "timeout_seconds": timeout_seconds,
        # turn/start was accepted, so returncode is 0 regardless of the reply
        # outcome: the caller marks the packet processed and never re-sends. The
        # reply detail lives in terminal_status / delivery_observed, not a
        # failure code — a delivered turn must not read as a redelivery trigger.
        "returncode": 0,
        "stdout": assistant_text.strip(),
        "stderr": "" if status == "completed" else json.dumps(error or terminal or {"unobserved": True}, sort_keys=True),
        "turn_started": started,
        "terminal_status": terminal_status,
        "delivery_observed": delivery_observed,
        "notifications": notifications[-50:],
    }


def execute_runtime_trigger(
    session: dict,
    message: dict,
    canonical_materialization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = runtime_metadata(session)
    command = runtime.get("command") if isinstance(runtime, dict) else None
    derived = False
    runtime_family = str(runtime.get("family", ""))
    runtime_home = runtime.get("home") or runtime_home_from_source(runtime_family, runtime.get("session_source"))
    livecraft_runtime_command = livecraft_runtime_wake_available(session)
    if runtime_family == "bb":
        if not bb_bootstrap_enabled():
            return {
                "status": "bb_adapter_disabled",
                "runtime_family": "bb",
                "returncode": 1,
                "delivery_accepted": False,
            }
        if not isinstance(canonical_materialization, dict) or not canonical_materialization.get(
            "materialized"
        ):
            return {
                "status": "bb_canonical_materialization_required",
                "runtime_family": "bb",
                "returncode": 1,
                "delivery_accepted": False,
            }
        try:
            continuation_module = importlib.import_module(
                ".".join(("llm_collab", "bb_continuation"))
            )
            BbContinuationRefused = continuation_module.BbContinuationRefused

            workspace_id = config_get("workspace_id")
            ledger_module = importlib.import_module(".".join(("llm_collab", "ledger")))
            paths = ledger_module.LedgerPaths.derive(project_state_root(), str(workspace_id))
            with ledger_module.LedgerStore.open_writer(paths) as store:
                result = continuation_module.continue_bb_thread(
                    store,
                    client=continuation_module.client_from_project(
                        get_project(str(session["project_id"]))
                    ),
                    session=session,
                    materialized=canonical_materialization,
                    observed_at_utc=now_utc().isoformat(timespec="seconds"),
                )
        except BbContinuationRefused as error:
            return {
                "status": "bb_continuation_refused",
                "runtime_family": "bb",
                "returncode": 1,
                "delivery_accepted": False,
                "stderr": str(error),
            }
        return {
            "status": result.state,
            "detail": result.detail,
            "runtime_family": "bb",
            "returncode": 0 if result.state in {"queued", "duplicate", "failed"} else 1,
            "delivery_accepted": result.state in {"queued", "duplicate", "failed"},
            "message_id": result.message_id,
            "delivery_id": result.delivery_id,
            "attempt_id": result.attempt_id,
            "receipt_id": result.receipt_id,
            "native_called": result.native_called,
        }
    if runtime_family == "pi":
        # Pre-wake drift guard: the current native session must still match the
        # provider/model/thinking/cwd fingerprint pinned at registration. A mismatch
        # or any unreadable/incomplete evidence emits NO wake and returns a
        # non-success, so the watcher does not mark the packet processed — it stays
        # durable/unread for pull and later re-evaluation. Fail closed before any
        # append; a wrong parse must never produce a false wake.
        pinned = session.get("pi_fingerprint")
        current = read_pi_session_fingerprint(runtime.get("session_source"), runtime.get("session_id"))
        if not isinstance(pinned, dict) or current is None or current != pinned:
            return {
                "status": "pi_fingerprint_drift",
                "runtime_family": "pi",
                "returncode": 1,
                "delivery_accepted": False,
            }
        if livecraft_runtime_command:
            # Livecraft has an API wake command, so fall through to the generic
            # runtime-command executor. It prompts the native session in the
            # background; the worker performs the exact-session durable drain.
            pass
        else:
            # A Pi session is woken by one durable, synced event on its own exact-session
            # event log — not by a mutable pointer file (the deleted pi_doorbell.py), which
            # a second delivery overwrote before the monitor read it. The event is only a
            # wake; the durable unread queue is the authority. A coalesced wake is harmless
            # because Pi drains the queue (inbox.py --session --acknowledge), not the event.
            wake_event = {"event": "pi_inbox_wake", "message_path": str(message["path"])}
            append_event(str(session["session_id"]), wake_event)
            append_wake_event(str(session["session_id"]), wake_event)
            # returncode 0 marks the packet in the session's processed_messages so the
            # watcher wakes once, not on every poll. That ledger is separate from the
            # durable unread queue: delivery_accepted stays False, so the packet remains
            # in agents/{id}/inbox.json for Pi to drain and acknowledge itself.
            return {
                "status": "runtime_triggered",
                "runtime_family": "pi",
                "event": "pi_inbox_wake",
                "returncode": 0,
                "delivery_accepted": False,
            }
    if not command and runtime_family == "codex_app":
        app_server_result = execute_codex_app_server_trigger(
            session,
            message,
            str(runtime_home) if runtime_home else None,
        )
        if app_server_result is not None:
            return app_server_result

    if not command:
        command = derived_runtime_command(session, message)
        derived = command is not None
    if not command:
        raise ValueError("runtime trigger requested without runtime.command")

    timeout_seconds = int(runtime.get("timeout_seconds", 30))
    payload = build_runtime_payload(session, message)
    env = os.environ.copy()
    env.update(
        {
            "LLM_COLLAB_SESSION_ID": str(session["session_id"]),
            "LLM_COLLAB_AGENT_ID": str(session["agent_id"]),
            "LLM_COLLAB_MESSAGE_PATH": str(message["path"]),
            "LLM_COLLAB_MESSAGE_TITLE": str(message["frontmatter"].get("title", "")),
            "LLM_COLLAB_MESSAGE_FROM": str(message["frontmatter"].get("from", "")),
            "LLM_COLLAB_AUTOBRIDGE_MODE": str(session.get("mode", "")),
            "LLM_COLLAB_RUNTIME_FAMILY": str(runtime.get("family", "")),
            "LLM_COLLAB_RUNTIME_SESSION_ID": str(runtime.get("session_id", "")),
            "LLM_COLLAB_RUNTIME_HOME": str(runtime.get("home", "")),
            "LLM_COLLAB_TARGET_SESSION_ID": str(message["frontmatter"].get("target_session_id", "")),
            "LLM_COLLAB_SENDER_SESSION_ID": str(message["frontmatter"].get("sender_session_id", "")),
        }
    )
    if runtime_home:
        if runtime_family == "codex_app":
            env["CODEX_HOME"] = str(runtime_home)
        elif runtime_family == "claude_app":
            env["CLAUDE_HOME"] = str(runtime_home)
        elif runtime_family == "gemini_cli":
            env["GEMINI_HOME"] = str(runtime_home)
    result = subprocess.run(
        command,
        input=None if derived else json.dumps(payload),
        stdin=subprocess.DEVNULL if derived else None,
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
        timeout=timeout_seconds,
        check=False,
    )
    trigger_result = {
        "command": command,
        "derived_command": derived,
        "timeout_seconds": timeout_seconds,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if livecraft_runtime_command:
        # The background prompt is accepted, not the durable packet. Keep the
        # packet unread so the Livecraft worker's exact-session drain acknowledges it.
        trigger_result["delivery_accepted"] = False
    return trigger_result


def runtime_delivery_accepted(runtime_result: dict[str, Any]) -> bool:
    return (
        runtime_result.get("returncode") == 0
        and bool(runtime_result.get("delivery_accepted", True))
    )


def ui_refresh_enabled(runtime: dict[str, Any]) -> bool:
    override = os.environ.get("LLM_COLLAB_UI_REFRESH")
    if override is not None:
        return override.lower() not in {"0", "false", "no", "off"}
    configured = runtime.get("ui_refresh")
    if configured is not None:
        return bool(configured)
    return runtime.get("family") == "codex_app"


def osascript_binary() -> str:
    return os.environ.get("LLM_COLLAB_OSASCRIPT_BIN", "osascript")


def codex_app_binary() -> str:
    return os.environ.get(
        "LLM_COLLAB_CODEX_APP_BIN",
        "/Applications/Codex.app/Contents/MacOS/Codex",
    )


def codex_remote_debugging_port(runtime: dict[str, Any] | None = None) -> str | None:
    configured = (
        (runtime or {}).get("remote_debugging_port")
        or os.environ.get("LLM_COLLAB_CODEX_REMOTE_DEBUGGING_PORT")
    )
    if configured is None:
        return None
    text = str(configured).strip()
    if not text:
        return None
    if not text.isdigit():
        raise ValueError(f"Codex remote debugging port must be numeric: {text}")
    return text


def run_osascript(script: str, timeout_seconds: int = 5) -> dict[str, Any]:
    result = subprocess.run(
        [osascript_binary(), "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def codex_process_rows() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ps", "eww", "-axo", "pid=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if not pid_text.isdigit():
            continue
        if "/Applications/Codex.app/Contents/MacOS/Codex" not in command:
            continue
        rows.append({"pid": int(pid_text), "command": command})
    return rows


def extract_codex_user_data_dir(command: str) -> str | None:
    match = re.search(r"--user-data-dir=(.*?)(?:\s[A-Za-z_][A-Za-z0-9_]*=|$)", command)
    if match:
        return match.group(1).strip()
    return None


def codex_user_data_dir_from_runtime_home(runtime_home: str | None) -> str | None:
    if not runtime_home:
        return None
    name = Path(runtime_home).expanduser().name
    app_support = Path.home() / "Library" / "Application Support"
    if name == ".codex":
        return str(app_support / "Codex")
    account_match = re.fullmatch(r"\.codex-app-account(\d+)", name)
    if account_match:
        return str(app_support / f"Codex Account {account_match.group(1)}")
    return None


def find_codex_process_for_runtime_home(runtime_home: str | None) -> dict[str, Any] | None:
    if not runtime_home:
        return None
    marker = f"CODEX_HOME={runtime_home}"
    for row in codex_process_rows():
        if marker in row["command"]:
            row["user_data_dir"] = (
                codex_user_data_dir_from_runtime_home(runtime_home)
                or extract_codex_user_data_dir(row["command"])
            )
            return row
    return None


def wait_for_process_exit(pid: int, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def relaunch_codex_account(runtime_home: str | None, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    if not runtime_home:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "runtime_home is required for codex account relaunch",
        }

    process = find_codex_process_for_runtime_home(runtime_home)
    terminated_pid = process.get("pid") if process else None
    user_data_dir = (
        process.get("user_data_dir")
        if process
        else codex_user_data_dir_from_runtime_home(runtime_home)
    )

    if terminated_pid is not None:
        os.kill(int(terminated_pid), signal.SIGTERM)
        exited = wait_for_process_exit(int(terminated_pid))
        if not exited:
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": f"Codex process {terminated_pid} did not exit after SIGTERM",
                "terminated_pid": terminated_pid,
                "user_data_dir": user_data_dir,
            }

    command = [codex_app_binary()]
    if user_data_dir:
        command.append(f"--user-data-dir={user_data_dir}")
    remote_debugging_port = codex_remote_debugging_port(runtime)
    if remote_debugging_port:
        command.append(f"--remote-debugging-port={remote_debugging_port}")
    env = {**os.environ, "CODEX_HOME": runtime_home}
    child = subprocess.Popen(
        command,
        cwd=str(Path.home()),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "terminated_pid": terminated_pid,
        "launched_pid": child.pid,
        "user_data_dir": user_data_dir,
        "remote_debugging_port": remote_debugging_port,
    }


def open_codex_thread_deeplink(
    runtime_home: str | None,
    runtime_session_id: str | None,
) -> dict[str, Any]:
    if not runtime_home:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "runtime_home is required for codex deeplink refresh",
        }
    if not runtime_session_id:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "runtime_session_id is required for codex deeplink refresh",
        }
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        runtime_session_id,
    ):
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"runtime_session_id is not a Codex deeplink UUID: {runtime_session_id}",
        }

    process = find_codex_process_for_runtime_home(runtime_home)
    require_process = os.environ.get("LLM_COLLAB_CODEX_DEEPLINK_REQUIRE_PROCESS", "1")
    if process is None and require_process.lower() not in {"0", "false", "no", "off"}:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"No running Codex process found for CODEX_HOME={runtime_home}",
        }

    user_data_dir = (
        process.get("user_data_dir")
        if process
        else codex_user_data_dir_from_runtime_home(runtime_home)
    )
    command = [codex_app_binary()]
    if user_data_dir:
        command.append(f"--user-data-dir={user_data_dir}")
    remote_debugging_port = codex_remote_debugging_port(runtime_metadata({"runtime": {"family": "codex_app"}}))
    if remote_debugging_port:
        command.append(f"--remote-debugging-port={remote_debugging_port}")
    command.append(f"codex://threads/{runtime_session_id}")

    child = subprocess.Popen(
        command,
        cwd=str(Path.home()),
        env={**os.environ, "CODEX_HOME": runtime_home},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    wait_seconds = float(os.environ.get("LLM_COLLAB_CODEX_DEEPLINK_LAUNCHER_WAIT_SECONDS", "2"))
    try:
        launcher_returncode = child.wait(timeout=wait_seconds)
        terminated_launcher = False
    except subprocess.TimeoutExpired:
        child.terminate()
        terminated_launcher = True
        try:
            launcher_returncode = child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            launcher_returncode = child.wait(timeout=2)
    return {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "launched_pid": child.pid,
        "launcher_returncode": launcher_returncode,
        "terminated_launcher": terminated_launcher,
        "target_pid": process.get("pid") if process else None,
        "user_data_dir": user_data_dir,
        "deeplink": f"codex://threads/{runtime_session_id}",
        "remote_debugging_port": remote_debugging_port,
    }


def codex_cdp_refresh(runtime_session_id: str | None) -> dict[str, Any]:
    if not runtime_session_id:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "runtime_session_id is required for codex CDP refresh",
        }

    port = int(os.environ.get("LLM_COLLAB_CODEX_CDP_PORT", "9222"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
            targets = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": (
                f"Codex CDP is unavailable on 127.0.0.1:{port}. "
                "Launch the target Codex account with --remote-debugging-port "
                "before using ui_refresh_method=cdp."
            ),
            "error": str(exc),
            "port": port,
        }

    target = next(
        (
            item
            for item in targets
            if isinstance(item, dict)
            and item.get("webSocketDebuggerUrl")
            and str(item.get("url", "")).startswith("app://")
        ),
        None,
    )
    if target is None:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"No Codex renderer target found on CDP port {port}",
            "port": port,
            "targets": targets,
        }

    invalidate_expression = f"""
(async () => {{
  const threadId = {json.dumps(runtime_session_id)};
  const send = window.electronBridge?.sendMessageFromView;
  const deliveries = [];
  if (typeof send === 'function') {{
    for (const payload of [
      {{ type: 'query-cache-invalidate', queryKey: ['thread', threadId] }},
      {{ type: 'query-cache-invalidate', queryKey: ['thread/read', threadId] }},
      {{ type: 'thread-stream-snapshot-request', conversationId: threadId }},
      {{ type: 'thread-stream-resume-request', conversationId: threadId }},
    ]) {{
      try {{
        deliveries.push({{ payload, ok: true, out: await send(payload) ?? null }});
      }} catch (error) {{
        deliveries.push({{ payload, ok: false, error: String(error) }});
      }}
    }}
  }}
  window.dispatchEvent(new PopStateEvent('popstate', {{ state: history.state }}));
  await new Promise((resolve) => window.setTimeout(resolve, 250));
  return {{
    ok: true,
    threadId,
    href: location.href,
    deliveries,
    body: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 500),
  }};
}})()
""".strip()

    select_expression = f"""
(async () => {{
  const threadId = {json.dumps(runtime_session_id)};
  const selector = `[data-app-action-sidebar-thread-id="${{threadId}}"]`;
  let row = null;
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline && !row) {{
    row = document.querySelector(selector);
    if (!row) {{
      await new Promise((resolve) => window.setTimeout(resolve, 150));
    }}
  }}
  if (!row) {{
    return {{
      ok: false,
      threadId,
      reason: 'target thread row not found after renderer reload',
      href: location.href,
      body: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 700),
    }};
  }}
  row.scrollIntoView({{ block: 'center', inline: 'nearest' }});
  row.click();
  await new Promise((resolve) => window.setTimeout(resolve, 1000));
  return {{
    ok: true,
    threadId,
    href: location.href,
    active: row.getAttribute('data-app-action-sidebar-thread-active'),
    rowText: (row.innerText || row.textContent || '').replace(/\\s+/g, ' ').slice(0, 300),
    body: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 1000),
  }};
}})()
""".strip()

    invalidation_result: Any = None
    with JsonRpcWebSocketClient(str(target["webSocketDebuggerUrl"]), timeout_seconds=10) as client:
        invalidation_result = client.request(
            "Runtime.evaluate",
            {
                "expression": invalidate_expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
            numeric_id=True,
            jsonrpc=False,
        )
        client.request("Page.reload", {"ignoreCache": True}, numeric_id=True, jsonrpc=False)

    time.sleep(float(os.environ.get("LLM_COLLAB_CODEX_CDP_RELOAD_WAIT_SECONDS", "2")))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
            refreshed_targets = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "returncode": 1,
            "stdout": json.dumps({"invalidate": invalidation_result}, sort_keys=True),
            "stderr": "Codex CDP target disappeared after renderer reload",
            "error": str(exc),
            "port": port,
        }

    refreshed_target = next(
        (
            item
            for item in refreshed_targets
            if isinstance(item, dict)
            and item.get("webSocketDebuggerUrl")
            and str(item.get("url", "")).startswith("app://")
        ),
        None,
    )
    if refreshed_target is None:
        return {
            "returncode": 1,
            "stdout": json.dumps({"invalidate": invalidation_result}, sort_keys=True),
            "stderr": f"No Codex renderer target found on CDP port {port} after reload",
            "port": port,
            "targets": refreshed_targets,
        }

    with JsonRpcWebSocketClient(str(refreshed_target["webSocketDebuggerUrl"]), timeout_seconds=10) as client:
        select_result = client.request(
            "Runtime.evaluate",
            {
                "expression": select_expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
            numeric_id=True,
            jsonrpc=False,
        )
    select_value = (
        select_result.get("result", {}).get("value")
        if isinstance(select_result, dict)
        else None
    )
    selected_ok = bool(select_value.get("ok")) if isinstance(select_value, dict) else False

    return {
        "returncode": 0 if selected_ok else 1,
        "stdout": json.dumps({"invalidate": invalidation_result, "select": select_result}, sort_keys=True),
        "stderr": "" if selected_ok else "Codex renderer reload completed but target thread selection failed",
        "port": port,
        "target": {
            "id": refreshed_target.get("id"),
            "title": refreshed_target.get("title"),
            "url": refreshed_target.get("url"),
        },
    }


def refresh_runtime_ui(session: dict) -> dict[str, Any]:
    runtime = runtime_metadata(session)
    runtime_family = runtime.get("family")
    if not ui_refresh_enabled(runtime):
        return {"skipped": True, "reason": "ui_refresh_disabled"}

    if runtime_family == "codex_app":
        method = str(
            runtime.get("ui_refresh_method")
            or os.environ.get("LLM_COLLAB_CODEX_UI_REFRESH_METHOD")
            or "cdp"
        )
        if method == "none":
            return {
                "skipped": True,
                "reason": "codex_ui_refresh_method_none",
            }
        if method == "shortcut":
            return {
                "skipped": True,
                "reason": "codex_shortcut_refresh_unsupported",
                "stderr": "Command-R does not refresh Codex app threads.",
            }
        if method == "cdp":
            result = codex_cdp_refresh(
                str(runtime.get("session_id")) if runtime.get("session_id") else None,
            )
            return {"skipped": False, "method": "codex_cdp_refresh", **result}
        if method == "relaunch_account":
            runtime_home = runtime.get("home") or runtime_home_from_source(
                "codex_app",
                runtime.get("session_source"),
            )
            result = relaunch_codex_account(str(runtime_home) if runtime_home else None, runtime)
            return {"skipped": False, "method": "codex_relaunch_account", **result}
        if method == "deeplink":
            runtime_home = runtime.get("home") or runtime_home_from_source(
                "codex_app",
                runtime.get("session_source"),
            )
            result = open_codex_thread_deeplink(
                str(runtime_home) if runtime_home else None,
                str(runtime.get("session_id")) if runtime.get("session_id") else None,
            )
            return {"skipped": False, "method": "codex_thread_deeplink", **result}
        return {"skipped": True, "reason": f"unsupported_codex_ui_refresh_method={method}"}

    return {"skipped": True, "reason": f"unsupported_runtime_family={runtime_family}"}


def create_relay_prompt(session: dict, message: dict) -> dict[str, Any]:
    agent = get_agent(str(session["agent_id"]))
    prompt = build_handoff_prompt(agent, first_time=False)
    prompt_dir = autobridge_prompt_dir(str(session["session_id"]))
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / f"{utc_iso().replace(':', '-')}_{Path(message['path']).stem}.md"
    body = "\n".join(
        [
            f"# Session Autobridge Relay: {session['session_id']}",
            "",
            "A parked session matched a new inbox message but has no direct runtime wake hook.",
            "",
            f"- Agent: `{session['agent_id']}`",
            f"- Mode: `{session.get('mode')}`",
            f"- Requested wake strategy: `{session.get('wake_strategy')}`",
            f"- Matched message: `{message['path']}`",
            "",
            "Relay prompt:",
            "",
            "```text",
            prompt,
            "```",
        ]
    )
    write_file(prompt_path, body)
    return {"prompt_path": str(prompt_path.relative_to(STATE_ROOT)), "prompt": prompt}


def dispatch_session(
    session_id: str,
    *,
    project_id: str | None = None,
    repo_targets: list[str] | None = None,
    skip_paths: set[str] | None = None,
) -> dict[str, Any]:
    session = load_session(session_id)
    if session.get("binding_id"):
        # The watcher gate validates an existing file binding before calling this
        # function. Keep direct/unit callers compatible without reopening the
        # ledger; only otherwise-unbound sessions need the new derived lookup.
        binding_ok, resolved_binding = True, None
    else:
        binding_ok, resolved_binding = resolve_session_receive_binding(session)
    if not binding_ok:
        append_event(
            session_id,
            {
                "event": "session_skipped",
                "reason": "stale_or_foreign_canonical_binding",
                "status": session.get("status"),
            },
        )
        return {
            "session_id": session_id,
            "dispatchable": False,
            "reason": "stale_or_foreign_canonical_binding",
            "matched_messages": 0,
            "actions": [],
        }
    # Keep the derived binding in an ephemeral routing view. It is deliberately
    # not added to ``session``: later processed/settlement writes must not turn
    # this read-only resolver into a second session-file authority.
    routing_session = session if resolved_binding is None else {**session, **resolved_binding}
    if project_id is not None and session.get("project_id") != project_id:
        append_event(
            session_id,
            {
                "event": "session_skipped",
                "reason": "project_scope_mismatch",
                "status": session.get("status"),
            },
        )
        return {
            "session_id": session_id,
            "dispatchable": False,
            "reason": "project_scope_mismatch",
            "matched_messages": 0,
            "actions": [],
        }
    dispatchable, reason = session_is_dispatchable(session)
    if not dispatchable:
        append_event(
            session_id,
            {
                "event": "session_skipped",
                "reason": reason,
                "status": session.get("status"),
            },
        )
        return {
            "session_id": session_id,
            "dispatchable": False,
            "reason": reason,
            "matched_messages": 0,
            "actions": [],
        }

    matched = []
    repo_scope_refusals: list[dict] = []
    completed_settlements = []
    seen = processed_messages(session)
    for message in matching_unread_messages(
        routing_session,
        invocation_repo_targets=repo_targets,
        repo_scope_refusals=repo_scope_refusals,
        skip_paths=skip_paths,
    ):
        if processed_message_blocks_dispatch(routing_session, message, seen):
            if (
                message_needs_canonical_materialization(routing_session, message)
                and message["path"] in canonical_settled_message_paths(session)
            ):
                completed_settlements.append(message)
            continue
        target_match, target_reason = message_targets_session(
            routing_session,
            message,
            invocation_repo_targets=repo_targets,
        )
        if not target_match:
            append_event(
                session_id,
                {
                    "event": "message_skipped",
                    "message_path": message["path"],
                    "reason": target_reason,
                },
            )
            continue
        skip, skip_reason = should_skip_for_loop_protection(session, message)
        if skip:
            append_event(
                session_id,
                {
                    "event": "message_skipped",
                    "message_path": message["path"],
                    "reason": skip_reason,
                },
            )
            try:
                reserve_message_result(session, message, include_canonical=False)
            except (
                FileNotFoundError,
                UnreadableFile,
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                ValueError,
            ) as error:
                append_event(
                    session_id,
                    {
                        "event": "message_skipped_unprocessed",
                        "message_path": message["path"],
                        "reason": "session_capacity_refused",
                        "detail": str(error),
                    },
                )
                continue
            fenced, assertion_event, _ = activation_fenced_mutation(
                session,
                message,
                boundary="loop_protection_mark_message_processed",
                mutation=lambda: mark_message_processed(session, message["path"]),
            )
            if assertion_event is not None:
                append_event(session_id, assertion_event)
            if not fenced:
                append_event(
                    session_id,
                    {
                        "event": "message_skipped_unprocessed",
                        "message_path": message["path"],
                        "reason": assertion_event["reason"] if assertion_event else "activation_assert_refused",
                    },
                )
            continue
        matched.append(message)

    actions: list[dict[str, Any]] = []
    for message in completed_settlements:
        if runtime_metadata(session).get("family") == "pi":
            continue
        event = {
            "event": "message_already_consumed",
            "message_path": message["path"],
            "requested_mode": session.get("mode"),
            "requested_wake_strategy": session.get("wake_strategy"),
            "effective_action": "runtime_trigger",
            "reason": "already_processed",
            "runtime_result": {"returncode": 0, "skipped": True},
        }
        append_event(session_id, event)
        actions.append(event)
    materialization_slot_available = True
    for message in matched:
        activation_kind, activation_detail = classify_activation(
            message.get("frontmatter", {}),
            target_agent=str(session["agent_id"]),
        )
        if activation_kind == "malformed":
            append_event(
                session_id,
                {
                    "event": "activation_refused",
                    "message_path": message["path"],
                    "reason": "malformed_activation",
                    "detail": activation_detail,
                },
            )
            continue
        try:
            prepared_result = reserve_message_result(
                session,
                message,
                include_canonical=True,
            )
        except (
            FileNotFoundError,
            UnreadableFile,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ) as error:
            event = {
                "event": "message_skipped",
                "message_path": message["path"],
                "reason": "session_capacity_refused",
                "detail": str(error),
            }
            append_event(session_id, event)
            actions.append(event)
            continue
        activation_allowed, activation_event = claim_message_activation(session, message)
        if activation_event is not None:
            append_event(session_id, activation_event)
        if not activation_allowed:
            continue
        action, action_reason = resolve_effective_action(session, message)
        event: dict[str, Any] = {
            "event": "message_dispatched",
            "message_path": message["path"],
            "requested_mode": session.get("mode"),
            "requested_wake_strategy": session.get("wake_strategy"),
            "effective_action": action,
            "reason": action_reason,
            "sender_session_id": message["frontmatter"].get("sender_session_id"),
            "target_session_id": message["frontmatter"].get("target_session_id"),
        }
        should_mark_processed = True

        if action == "runtime_trigger":
            runtime = runtime_metadata(session)
            if (
                runtime.get("family") == "pi"
                and not message_needs_canonical_materialization(routing_session, message)
                and not livecraft_runtime_wake_available(routing_session)
            ):
                event["reason"] = EXACT_BINDING_REQUIRED_REASON
                should_mark_processed = False
                append_event(session_id, event)
                actions.append(event)
                continue
            if message_needs_canonical_materialization(routing_session, message):
                if not materialization_slot_available:
                    event["reason"] = "pull_pending"
                    event["canonical_materialization_result"] = {
                        "resolved": False,
                        "reason": "pull_pending",
                        "detail": "one canonical materialization slot per poll",
                        "deferred": True,
                        "canonical_write_started": False,
                    }
                    should_mark_processed = False
                    append_event(session_id, event)
                    actions.append(event)
                    continue
                asserted, assertion_event, materialization_result = activation_fenced_mutation(
                    session,
                    message,
                    boundary="canonical_receive_materialization",
                    mutation=lambda: materialize_selected_runtime_packet(routing_session, message),
                )
                if assertion_event is not None:
                    event.setdefault("activation_assertions", []).append(assertion_event)
                if not asserted:
                    event["reason"] = (
                        assertion_event["reason"]
                        if assertion_event
                        else "activation_assert_refused"
                    )
                    append_event(session_id, event)
                    actions.append(event)
                    continue
                event["canonical_materialization_result"] = materialization_result
                if materialization_result.get("canonical_write_started"):
                    materialization_slot_available = False
                if not materialization_result.get("resolved"):
                    event["reason"] = materialization_result.get(
                        "reason",
                        ROUTE_AMBIGUOUS_REASON,
                    )
                    append_event(session_id, event)
                    actions.append(event)
                    continue
                if (
                    materialization_result.get("materialized")
                    and not materialization_result.get("created")
                    and runtime.get("family") != "bb"
                ):
                    event["reason"] = "pull_pending"
                    append_event(session_id, event)
                    actions.append(event)
                    continue
                if (
                    runtime.get("family") == "pi"
                    and not materialization_result.get("materialized")
                ):
                    event["reason"] = "pull_pending"
                    append_event(session_id, event)
                    actions.append(event)
                    continue
                if message["path"] not in canonical_settled_message_paths(session):
                    settlement_reason = (
                        "gate_disabled"
                        if materialization_result.get("gate") == "disabled"
                        else "materialized"
                    )
                    asserted, assertion_event, _ = activation_fenced_mutation(
                        session,
                        message,
                        boundary="mark_canonical_settlement_complete",
                        mutation=lambda: mark_canonical_settlement_complete(
                            session,
                            message["path"],
                            settlement_reason,
                        ),
                    )
                    if assertion_event is not None:
                        event.setdefault("activation_assertions", []).append(assertion_event)
                    if not asserted:
                        event["reason"] = (
                            assertion_event["reason"]
                            if assertion_event
                            else "activation_assert_refused"
                        )
                        append_event(session_id, event)
                        actions.append(event)
                        continue
                prepared_candidate = {
                    **prepared_result[0],
                    "canonical_settled_messages": dict(
                        session.get("canonical_settled_messages", {})
                    ),
                    "updated_utc": session.get(
                        "updated_utc", prepared_result[0].get("updated_utc")
                    ),
                }
                prepared_result = (
                    prepared_candidate,
                    json.dumps(prepared_candidate, indent=2, sort_keys=True),
                )
            runtime_materialization = event.get("canonical_materialization_result")
            if runtime.get("family") == "bb":
                runtime_trigger = lambda: execute_runtime_trigger(
                    session, message, runtime_materialization
                )
            else:
                runtime_trigger = lambda: execute_runtime_trigger(session, message)
            asserted, assertion_event, runtime_result = activation_fenced_mutation(
                session,
                message,
                boundary="runtime_trigger",
                mutation=runtime_trigger,
            )
            if assertion_event is not None:
                event.setdefault("activation_assertions", []).append(assertion_event)
            if not asserted:
                event["reason"] = assertion_event["reason"] if assertion_event else "activation_assert_refused"
                append_event(session_id, event)
                actions.append(event)
                continue
            event["runtime_result"] = runtime_result
            should_mark_processed = runtime_result.get("returncode") == 0
            if runtime_delivery_accepted(runtime_result):
                asserted, assertion_event, _ = activation_fenced_mutation(
                    session,
                    message,
                    boundary="operator_turn_summary",
                    mutation=lambda: write_operator_turn_summary(
                        session,
                        message,
                        event_name="picked_up",
                        body="\n".join(
                            [
                                f"{session['agent_id']} picked up `{message['frontmatter'].get('title', '(no title)')}`.",
                                f"From: `{message['frontmatter'].get('sender_agent_id', message['frontmatter'].get('from', ''))}`",
                                f"Receiver runtime thread: `{runtime.get('session_id', '')}`",
                                f"Sender thread: `{message['frontmatter'].get('sender_session_id', '')}`",
                            ]
                        ),
                    ),
                )
                if assertion_event is not None:
                    event.setdefault("activation_assertions", []).append(assertion_event)
            if should_mark_processed:
                try:
                    asserted, assertion_event, ui_refresh_result = activation_fenced_mutation(
                        session,
                        message,
                        boundary="runtime_ui_refresh",
                        mutation=lambda: refresh_runtime_ui(session),
                    )
                    if assertion_event is not None:
                        event.setdefault("activation_assertions", []).append(assertion_event)
                    if not asserted:
                        event["reason"] = (
                            assertion_event["reason"]
                            if assertion_event
                            else "activation_assert_refused"
                        )
                        should_mark_processed = False
                        append_event(session_id, event)
                        actions.append(event)
                        continue
                    event["ui_refresh_result"] = ui_refresh_result
                except Exception as exc:
                    event["ui_refresh_result"] = {
                        "skipped": False,
                        "returncode": 1,
                        "stderr": str(exc),
                    }
        elif action == "relay_prompt":
            asserted, assertion_event, relay_result = activation_fenced_mutation(
                session,
                message,
                boundary="relay_prompt",
                mutation=lambda: create_relay_prompt(session, message),
            )
            if assertion_event is not None:
                event.setdefault("activation_assertions", []).append(assertion_event)
            if not asserted:
                event["reason"] = assertion_event["reason"] if assertion_event else "activation_assert_refused"
                append_event(session_id, event)
                actions.append(event)
                continue
            event["relay_result"] = relay_result

        mark_after_event = False
        if should_mark_processed:
            if message.get("activation_lease"):
                asserted, assertion_event, _ = activation_fenced_mutation(
                    session,
                    message,
                    boundary="mark_message_processed",
                    mutation=lambda: mark_message_processed(
                        session,
                        message["path"],
                        prepared=prepared_result,
                    ),
                )
                if assertion_event is not None:
                    event.setdefault("activation_assertions", []).append(assertion_event)
                if not asserted:
                    event["reason"] = assertion_event["reason"] if assertion_event else "activation_assert_refused"
                    should_mark_processed = False
            else:
                mark_after_event = True
        append_event(session_id, event)
        if should_mark_processed and mark_after_event:
            mark_message_processed(
                session,
                message["path"],
                prepared=prepared_result,
            )
        actions.append(event)

    for refusal in repo_scope_refusals:
        append_event(
            session_id,
            {
                "event": "message_skipped",
                "message_path": refusal["path"],
                "reason": refusal["reason"],
            },
        )

    return {
        "session_id": session_id,
        "dispatchable": True,
        "reason": "ok",
        "matched_messages": len(matched),
        "repo_scope_refused": repo_scope_refusals,
        "actions": actions,
    }
