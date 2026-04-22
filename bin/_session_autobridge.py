from __future__ import annotations

import json
import os
import subprocess
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
)

AUTOBRIDGE_ROOT = ROOT / "State" / "session_autobridge"
SESSIONS_DIR = AUTOBRIDGE_ROOT / "sessions"
EVENTS_DIR = AUTOBRIDGE_ROOT / "events"
PROMPTS_DIR = AUTOBRIDGE_ROOT / "prompts"

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


def load_session(session_id: str) -> dict:
    path = autobridge_session_path(session_id)
    if not path.exists():
        raise FileNotFoundError(f"Unknown session: {session_id}")
    return json.loads(path.read_text())


def save_session(payload: dict) -> None:
    session_id = str(payload["session_id"])
    path = autobridge_session_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_utc"] = utc_iso()
    write_file(path, json.dumps(payload, indent=2, sort_keys=True))


def append_event(session_id: str, event: dict[str, Any]) -> None:
    path = autobridge_event_log_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    event_payload = {"ts": utc_iso(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event_payload, sort_keys=True) + "\n")


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


def matching_unread_messages(session: dict) -> list[dict]:
    messages = get_unread_messages(str(session["agent_id"]))
    project_id = session.get("project_id")
    chat_id = session.get("chat_id")
    if project_id:
        messages = [m for m in messages if m["frontmatter"].get("project_id") == project_id]
    if chat_id:
        messages = [m for m in messages if m["frontmatter"].get("chat_id") == chat_id]
    return messages


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
    runtime = session.get("runtime", {})
    runtime_command = runtime.get("command") if isinstance(runtime, dict) else None

    if mode == "manual":
        return "manual_noop", "manual_mode"
    if mode == "notify" or wake_strategy == "notify":
        return "notify_only", "notify_mode"
    if activation_type == "human_relay":
        return "relay_prompt", "human_relay_fallback"
    if wake_strategy == "runtime_trigger" and runtime_command:
        return "runtime_trigger", "runtime_command_available"
    if wake_strategy == "relay":
        return "relay_prompt", "relay_mode"
    if activation_type == "cli_session":
        return "notify_only", "cli_session_has_no_runtime_hook"
    if activation_type == "api_trigger":
        return "notify_only", "api_trigger_missing_runtime_command"
    return "notify_only", "unsupported_wake_strategy"


def build_runtime_payload(session: dict, message: dict) -> dict[str, Any]:
    fm = message["frontmatter"]
    return {
        "session": {
            "session_id": session["session_id"],
            "agent_id": session["agent_id"],
            "project_id": session.get("project_id"),
            "chat_id": session.get("chat_id"),
            "mode": session.get("mode"),
            "wake_strategy": session.get("wake_strategy"),
            "allowed_actions": session.get("allowed_actions", []),
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
            "body": message.get("body", ""),
        },
    }


def execute_runtime_trigger(session: dict, message: dict) -> dict[str, Any]:
    runtime = session.get("runtime", {})
    command = runtime.get("command") if isinstance(runtime, dict) else None
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
        }
    )
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
        "timeout_seconds": timeout_seconds,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


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
        }

        if action == "runtime_trigger":
            runtime_result = execute_runtime_trigger(session, message)
            event["runtime_result"] = runtime_result
        elif action == "relay_prompt":
            event["relay_result"] = create_relay_prompt(session, message)

        append_event(session_id, event)
        mark_message_processed(session, message["path"])
        actions.append(event)

    return {
        "session_id": session_id,
        "dispatchable": True,
        "reason": "ok",
        "matched_messages": len(matched),
        "actions": actions,
    }
