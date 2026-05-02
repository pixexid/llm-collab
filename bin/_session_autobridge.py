from __future__ import annotations

import json
import os
import re
import signal
import base64
import hashlib
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from _helpers import (
    ROOT,
    build_handoff_prompt,
    get_agent,
    get_unread_messages,
    now_utc,
    utc_iso,
    write_file,
    write_chat_note,
)

AUTOBRIDGE_ROOT = ROOT / "State" / "session_autobridge"
SESSIONS_DIR = AUTOBRIDGE_ROOT / "sessions"
EVENTS_DIR = AUTOBRIDGE_ROOT / "events"
PROMPTS_DIR = AUTOBRIDGE_ROOT / "prompts"
BINDINGS_DIR = AUTOBRIDGE_ROOT / "bindings"
THREAD_PAIRS_DIR = AUTOBRIDGE_ROOT / "thread_pairs"

SESSION_MODES = ("manual", "notify", "auto-read", "auto-reply")
SESSION_STATUSES = ("active", "parked", "stopping", "stopped", "superseded")
WAKE_STRATEGIES = ("none", "notify", "relay", "runtime_trigger")


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
    if not path.exists():
        raise FileNotFoundError(f"Unknown session: {session_id}")
    return json.loads(path.read_text())


def iter_sessions(agent_id: str | None = None) -> list[dict]:
    if not SESSIONS_DIR.exists():
        return []
    sessions: list[dict] = []
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            session = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if agent_id is not None and session.get("agent_id") != agent_id:
            continue
        sessions.append(session)
    return sessions


def load_binding(project_id: str, chat_id: str, agent_id: str) -> dict:
    path = autobridge_binding_path(project_id, chat_id, agent_id)
    if not path.exists():
        raise FileNotFoundError(
            f"Unknown binding: project={project_id} chat={chat_id} agent={agent_id}"
        )
    return json.loads(path.read_text())


def load_thread_pair(project_id: str, chat_id: str, agent_a: str, agent_b: str) -> dict:
    path = autobridge_thread_pair_path(project_id, chat_id, agent_a, agent_b)
    if not path.exists():
        raise FileNotFoundError(
            f"Unknown thread pair: project={project_id} chat={chat_id} agents={agent_a},{agent_b}"
        )
    return json.loads(path.read_text())


def save_binding(payload: dict) -> None:
    path = autobridge_binding_path(
        str(payload["project_id"]),
        str(payload["chat_id"]),
        str(payload["agent_id"]),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_utc"] = utc_iso()
    write_file(path, json.dumps(payload, indent=2, sort_keys=True))


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


def save_session(payload: dict) -> None:
    session_id = str(payload["session_id"])
    path = autobridge_session_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_utc"] = utc_iso()
    write_file(path, json.dumps(payload, indent=2, sort_keys=True))


def binding_payload_from_session(session: dict) -> dict[str, Any] | None:
    runtime = runtime_metadata(session)
    project_id = session.get("project_id")
    chat_id = session.get("chat_id")
    runtime_family = runtime.get("family")
    runtime_session_id = runtime.get("session_id")
    if not project_id or not chat_id or not runtime_family or not runtime_session_id:
        return None

    existing: dict[str, Any] = {}
    try:
        existing = load_binding(str(project_id), str(chat_id), str(session["agent_id"]))
    except FileNotFoundError:
        existing = {}

    return {
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


def update_binding_from_session(session: dict) -> dict[str, Any] | None:
    payload = binding_payload_from_session(session)
    if payload is None:
        return None
    save_binding(payload)
    return payload


def append_event(session_id: str, event: dict[str, Any]) -> None:
    path = autobridge_event_log_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    event_payload = {"ts": utc_iso(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event_payload, sort_keys=True) + "\n")


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
    matches = sorted(ROOT.joinpath("Chats").glob(f"*__{chat_id}"))
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


def discover_claude_runtime_session(project_path: str | None = None) -> dict[str, Any]:
    projects_root = claude_home() / "projects"
    candidate_indexes: list[Path] = []
    project_slug = claude_project_slug(project_path)
    if project_slug:
        candidate = projects_root / project_slug / "sessions-index.json"
        if candidate.exists():
            candidate_indexes.append(candidate)
    if not candidate_indexes and projects_root.exists():
        candidate_indexes.extend(projects_root.glob("*/sessions-index.json"))

    newest_entry: dict[str, Any] | None = None
    newest_index: Path | None = None
    for index_path in candidate_indexes:
        try:
            payload = json.loads(index_path.read_text())
        except json.JSONDecodeError:
            continue
        for entry in payload.get("entries", []):
            if project_path and entry.get("projectPath") != str(Path(project_path).expanduser().resolve()):
                continue
            if newest_entry is None or int(entry.get("fileMtime", 0)) > int(newest_entry.get("fileMtime", 0)):
                newest_entry = entry
                newest_index = index_path

    if newest_entry and newest_entry.get("sessionId"):
        return {
            "family": "claude_app",
            "session_id": str(newest_entry["sessionId"]),
            "session_source": str(newest_index),
            "home": str(claude_home()),
            "seen_at": newest_entry.get("modified") or newest_entry.get("created"),
        }
    raise FileNotFoundError("No Claude project session index found")


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
    if session_is_expired(session):
        return False, "lease_expired"
    return True, "ok"


def session_target_ids(session: dict) -> set[str]:
    runtime = runtime_metadata(session)
    target_ids = {str(session["session_id"])}
    runtime_session_id = runtime.get("session_id")
    if runtime_session_id:
        target_ids.add(str(runtime_session_id))
    return target_ids


def find_dispatchable_target_session(
    *,
    agent_id: str,
    project_id: str | None,
    chat_id: str | None,
    target_session_id: str | None,
) -> dict[str, Any] | None:
    for session in iter_sessions(agent_id=agent_id):
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


def message_targets_session(session: dict, message: dict) -> tuple[bool, str]:
    frontmatter = message.get("frontmatter", {})
    target_session_id = frontmatter.get("target_session_id")
    if not target_session_id:
        return True, "broadcast_or_agent_scoped"
    if str(target_session_id) in session_target_ids(session):
        return True, "explicit_target_match"
    return False, "target_session_mismatch"


def matching_unread_messages(session: dict) -> list[dict]:
    messages = get_unread_messages(str(session["agent_id"]))
    project_id = session.get("project_id")
    chat_id = session.get("chat_id")
    if project_id:
        messages = [m for m in messages if m["frontmatter"].get("project_id") == project_id]
    if chat_id:
        messages = [m for m in messages if m["frontmatter"].get("chat_id") == chat_id]
    matched_messages: list[dict] = []
    for message in messages:
        target_match, _ = message_targets_session(session, message)
        if target_match:
            matched_messages.append(message)
    return matched_messages


def processed_messages(session: dict) -> set[str]:
    return set(session.get("processed_messages", []))


def mark_message_processed(session: dict, message_path: str) -> None:
    existing = session.get("processed_messages", [])
    if message_path not in existing:
        existing.append(message_path)
    session["processed_messages"] = existing
    save_session(session)


def should_skip_for_loop_protection(session: dict, message: dict) -> tuple[bool, str]:
    frontmatter = message.get("frontmatter", {})
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
    }


def build_resume_prompt(session: dict, message: dict) -> str:
    fm = message["frontmatter"]
    body = message.get("body", "").strip()
    lines = [
        "You are resuming a registered llm-collab worker session for one bounded action.",
        "Read the routed message context below and produce exactly one bounded reply or action.",
        "",
        f"llm_collab_session_id: {session['session_id']}",
        f"agent_id: {session['agent_id']}",
        f"sender_agent_id: {fm.get('sender_agent_id', fm.get('from', ''))}",
        f"sender_session_id: {fm.get('sender_session_id', '')}",
        f"target_session_id: {fm.get('target_session_id', '')}",
        f"chat_id: {fm.get('chat_id', '')}",
        f"project_id: {fm.get('project_id', '')}",
        f"title: {fm.get('title', '')}",
        "",
        "Message body:",
        body or "(no body)",
        "",
        "If the request is trivial, answer tersely. Do not start unrelated work.",
    ]
    return "\n".join(lines)


def derived_runtime_command(session: dict, message: dict) -> list[str] | None:
    runtime = runtime_metadata(session)
    runtime_family = runtime.get("family")
    runtime_session_id = runtime.get("session_id")
    if not runtime_family or not runtime_session_id:
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
        if "codex app-server" not in command:
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


def discover_codex_app_server(runtime_home: str | None) -> dict[str, Any] | None:
    configured_url = os.environ.get("LLM_COLLAB_CODEX_APP_SERVER_URL")
    configured_token_file = os.environ.get("LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE")
    if configured_url:
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


def _socket_read_exact(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("websocket connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class JsonRpcWebSocketClient:
    def __init__(self, url: str, token: str | None = None, timeout_seconds: int = 30):
        self.url = url
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.sock: socket.socket | None = None
        self.counter = 0

    def __enter__(self) -> "JsonRpcWebSocketClient":
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
            raise ValueError(f"Unsupported Codex app-server websocket URL: {self.url}")
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=self.timeout_seconds)
        sock.settimeout(self.timeout_seconds)
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
            response += sock.recv(4096)
            if not response:
                raise ConnectionError("websocket handshake failed")
        header_text = response.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
        if " 101 " not in header_text.splitlines()[0]:
            raise ConnectionError(f"websocket handshake failed: {header_text.splitlines()[0]}")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept not in header_text:
            raise ConnectionError("websocket handshake failed: invalid accept header")
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
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        if not self.sock:
            raise ConnectionError("websocket is not connected")
        first, second = _socket_read_exact(self.sock, 2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(_socket_read_exact(self.sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(_socket_read_exact(self.sock, 8), "big")
        mask = _socket_read_exact(self.sock, 4) if masked else b""
        payload = _socket_read_exact(self.sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def recv_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise ConnectionError("websocket closed")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))

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
            if message.get("id") and message.get("method"):
                self.send_json({"jsonrpc": "2.0", "id": message["id"], "result": {}})


def _codex_app_server_token(token_file: str | None) -> str | None:
    if not token_file:
        return None
    path = Path(token_file).expanduser()
    if not path.exists():
        return None
    return path.read_text().strip()


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
    prompt = build_resume_prompt(session, message)
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
        started = client.request("turn/start", turn_payload)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            message_payload = client.recv_json()
            if message_payload.get("id") and message_payload.get("method"):
                client.send_json({"jsonrpc": "2.0", "id": message_payload["id"], "result": {}})
                continue
            method = str(message_payload.get("method", ""))
            if not method:
                continue
            notifications.append(method)
            params = message_payload.get("params")
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
        if terminal is None:
            raise TimeoutError("Codex app-server turn did not complete before timeout")

    turn = terminal.get("turn") if isinstance(terminal, dict) else None
    status = turn.get("status") if isinstance(turn, dict) else None
    error = turn.get("error") if isinstance(turn, dict) else None
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
        "returncode": 0 if status == "completed" else 1,
        "stdout": assistant_text.strip(),
        "stderr": "" if status == "completed" else json.dumps(error or terminal, sort_keys=True),
        "turn_started": started,
        "terminal_status": status,
        "notifications": notifications[-50:],
    }


def execute_runtime_trigger(session: dict, message: dict) -> dict[str, Any]:
    runtime = runtime_metadata(session)
    command = runtime.get("command") if isinstance(runtime, dict) else None
    derived = False
    runtime_family = str(runtime.get("family", ""))
    runtime_home = runtime.get("home") or runtime_home_from_source(runtime_family, runtime.get("session_source"))
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
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "command": command,
        "derived_command": derived,
        "timeout_seconds": timeout_seconds,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def ui_refresh_enabled(runtime: dict[str, Any]) -> bool:
    override = os.environ.get("LLM_COLLAB_UI_REFRESH")
    if override is not None:
        return override.lower() not in {"0", "false", "no", "off"}
    configured = runtime.get("ui_refresh")
    if configured is not None:
        return bool(configured)
    return runtime.get("family") in {"codex_app", "claude_app"}


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

    if runtime_family == "claude_app":
        result = run_osascript(
            """
tell application "Claude" to activate
tell application "System Events"
  tell process "Claude"
    click menu item "Reload This Page" of menu 1 of menu bar item "View" of menu bar 1
  end tell
end tell
""".strip()
        )
        return {"skipped": False, "method": "claude_reload_page", **result}

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
    return {"prompt_path": str(prompt_path.relative_to(ROOT)), "prompt": prompt}


def dispatch_session(session_id: str) -> dict[str, Any]:
    session = load_session(session_id)
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
    seen = processed_messages(session)
    for message in matching_unread_messages(session):
        if message["path"] in seen:
            continue
        target_match, target_reason = message_targets_session(session, message)
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
            mark_message_processed(session, message["path"])
            continue
        matched.append(message)

    actions: list[dict[str, Any]] = []
    for message in matched:
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
            write_operator_turn_summary(
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
            )
            runtime_result = execute_runtime_trigger(session, message)
            event["runtime_result"] = runtime_result
            should_mark_processed = runtime_result.get("returncode") == 0
            if should_mark_processed:
                try:
                    event["ui_refresh_result"] = refresh_runtime_ui(session)
                except Exception as exc:
                    event["ui_refresh_result"] = {
                        "skipped": False,
                        "returncode": 1,
                        "stderr": str(exc),
                    }
        elif action == "relay_prompt":
            event["relay_result"] = create_relay_prompt(session, message)

        append_event(session_id, event)
        if should_mark_processed:
            mark_message_processed(session, message["path"])
        actions.append(event)

    return {
        "session_id": session_id,
        "dispatchable": True,
        "reason": "ok",
        "matched_messages": len(matched),
        "actions": actions,
    }
