"""
Shared utilities for llm-collab bin scripts.
All paths are relative to WORKSPACE_ROOT (the directory containing collab.config.json).
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------

def find_workspace_root(start: Path | None = None) -> Path:
    """Walk up from start (default: cwd) looking for collab.config.json."""
    here = Path(start or os.getcwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "collab.config.json").exists():
            return candidate
    # Fallback: the bin/ script's parent
    return Path(__file__).resolve().parent.parent


ROOT: Path = find_workspace_root()
CONFIG_FILE = ROOT / "collab.config.json"
AGENTS_FILE = ROOT / "agents.json"
PROJECTS_FILE = ROOT / "projects.json"
AGENTS_DIR = ROOT / "agents"
CHATS_DIR = ROOT / "Chats"
TASKS_DIR = ROOT / "Tasks"
STATE_DIR = ROOT / "State" / "inbox"
INDEX_DIR = ROOT / "Index"

TASK_FOLDERS = ("active", "backlog", "done")
TASK_STATUSES = ("open", "in_progress", "blocked", "review", "done")
TASK_PRIORITIES = ("low", "normal", "high", "urgent")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_config_cache: dict | None = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        if not CONFIG_FILE.exists():
            print(
                f"[error] collab.config.json not found at {ROOT}\n"
                "Run: python scripts/init.py",
                file=sys.stderr,
            )
            sys.exit(1)
        _config_cache = json.loads(CONFIG_FILE.read_text())
    return _config_cache


def config_get(key: str, default: Any = None) -> Any:
    return load_config().get(key, default)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

_agents_cache: list | None = None


def load_agents() -> list[dict]:
    global _agents_cache
    if _agents_cache is None:
        if not AGENTS_FILE.exists():
            print(
                f"[error] agents.json not found at {ROOT}\n"
                "Run: python scripts/init.py",
                file=sys.stderr,
            )
            sys.exit(1)
        payload = json.loads(AGENTS_FILE.read_text())
        _agents_cache = payload.get("agents", [])
    return _agents_cache


def get_agent(agent_id: str) -> dict:
    for a in load_agents():
        if a["id"] == agent_id:
            return a
    print(f"[error] Unknown agent: {agent_id!r}", file=sys.stderr)
    sys.exit(1)


def agent_ids() -> list[str]:
    return [a["id"] for a in load_agents()]


def watcher_enabled_agents() -> list[dict]:
    return [
        a for a in load_agents()
        if a.get("activation", {}).get("watcher_enabled", False)
    ]


def is_human_relay(agent: dict) -> bool:
    return agent.get("activation", {}).get("type") == "human_relay"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

_projects_cache: list | None = None


def load_projects() -> list[dict]:
    global _projects_cache
    if _projects_cache is None:
        if not PROJECTS_FILE.exists():
            return []
        payload = json.loads(PROJECTS_FILE.read_text())
        _projects_cache = payload.get("projects", [])
    return _projects_cache


def get_project(project_id: str) -> dict | None:
    for p in load_projects():
        if p["id"] == project_id:
            return p
    return None


def project_ids() -> list[str]:
    return [p["id"] for p in load_projects()]


def ensure_project(project_id: str | None, *, allow_none: bool = True) -> None:
    if project_id is None:
        if allow_none:
            return
        print("[error] project_id is required but missing", file=sys.stderr)
        sys.exit(1)
    if get_project(project_id) is None:
        known = project_ids()
        if known:
            print(
                f"[error] Unknown project_id: {project_id!r}. Known: {', '.join(known)}",
                file=sys.stderr,
            )
        else:
            print(
                f"[error] Unknown project_id: {project_id!r}. No projects configured in projects.json.",
                file=sys.stderr,
            )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Per-agent paths
# ---------------------------------------------------------------------------

def agent_dir(agent_id: str) -> Path:
    return AGENTS_DIR / agent_id


def agent_identity_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "identity.md"


def agent_memory_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "memory.md"


def agent_inbox_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "inbox.json"


# ---------------------------------------------------------------------------
# Inbox state (pointer model)
# ---------------------------------------------------------------------------

def load_agent_inbox(agent_id: str) -> dict:
    path = agent_inbox_path(agent_id)
    if not path.exists():
        return {"agent": agent_id, "updated_utc": utc_iso(), "unread": [], "read": []}
    return json.loads(path.read_text())


def save_agent_inbox(agent_id: str, data: dict) -> None:
    path = agent_inbox_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_utc"] = utc_iso()
    write_file(path, json.dumps(data, indent=2))


def add_to_inbox(agent_id: str, message_path: str | Path) -> None:
    """Append a message path (relative to ROOT) to the agent's unread list."""
    rel = str(Path(message_path).relative_to(ROOT)) if Path(message_path).is_absolute() else str(message_path)
    inbox = load_agent_inbox(agent_id)
    if rel not in inbox["unread"] and rel not in inbox["read"]:
        inbox["unread"].append(rel)
    save_agent_inbox(agent_id, inbox)


def mark_messages_read(agent_id: str, paths: list[str]) -> None:
    inbox = load_agent_inbox(agent_id)
    for p in paths:
        if p in inbox["unread"]:
            inbox["unread"].remove(p)
        if p not in inbox["read"]:
            inbox["read"].append(p)
    save_agent_inbox(agent_id, inbox)


def get_unread_messages(agent_id: str) -> list[dict]:
    """Return list of parsed message dicts for unread messages."""
    inbox = load_agent_inbox(agent_id)
    messages = []
    for rel_path in inbox["unread"]:
        abs_path = ROOT / rel_path
        if abs_path.exists():
            fm, body = parse_frontmatter(abs_path.read_text())
            messages.append({"path": rel_path, "frontmatter": fm, "body": body})
    return messages


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return now_utc().isoformat(timespec="seconds")


def ts() -> str:
    """Sortable timestamp for filenames: 2026-04-07T10-00-00"""
    return now_utc().strftime("%Y-%m-%dT%H-%M-%S")


def date_prefix() -> str:
    return now_utc().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------

def shortid(length: int = 6) -> str:
    return uuid.uuid4().hex[:length]


def chat_id() -> str:
    return f"CHAT-{uuid.uuid4().hex[:8].upper()}"


def task_id() -> str:
    return f"TASK-{uuid.uuid4().hex[:6].upper()}"


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

def slugify(text: str, max_len: int = 48) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text).strip("-")
    return text[:max_len].rstrip("-")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_json(path: Path) -> dict | list:
    return json.loads(path.read_text())


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Frontmatter (YAML-lite: key: value, lists as [a, b])
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ---\\n...\\n--- frontmatter from body. Returns (fm_dict, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4:].strip()
    fm: dict = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            fm[k] = [i.strip().strip('"').strip("'") for i in inner.split(",") if i.strip()] if inner else []
        elif v.lower() == "null" or v == "":
            fm[k] = None
        elif v.lower() == "true":
            fm[k] = True
        elif v.lower() == "false":
            fm[k] = False
        else:
            try:
                fm[k] = int(v)
            except ValueError:
                fm[k] = v
    return fm, body


def dump_frontmatter(fm: dict, body: str) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                inner = ", ".join(f'"{i}"' if " " in str(i) else str(i) for i in v)
                lines.append(f"{k}: [{inner}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


# ---------------------------------------------------------------------------
# Chat helpers
# ---------------------------------------------------------------------------

def find_chats(partial: str | None = None) -> list[Path]:
    if not CHATS_DIR.exists():
        return []
    dirs = sorted(
        (d for d in CHATS_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")),
        key=lambda d: d.name,
    )
    if partial and partial != "last":
        dirs = [d for d in dirs if partial.lower() in d.name.lower()]
    return dirs


def latest_chat() -> Path | None:
    chats = find_chats()
    return chats[-1] if chats else None


def find_chat_by_partial(partial: str) -> Path | None:
    if partial == "last":
        return latest_chat()
    matches = find_chats(partial)
    if not matches:
        return None
    return matches[-1]


def load_chat_meta(chat_dir: Path) -> dict:
    meta_file = chat_dir / "meta.json"
    if meta_file.exists():
        return read_json(meta_file)
    return {}


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------

def task_folder_for_status(status: str) -> str:
    if status == "done":
        return "done"
    if status in ("open", "in_progress", "blocked", "review"):
        return "active"
    return "backlog"


def all_task_files() -> list[Path]:
    files = []
    for folder in TASK_FOLDERS:
        d = TASKS_DIR / folder
        if d.exists():
            files.extend(sorted(d.glob("*.md")))
    return files


def find_task_by_id(task_id: str) -> Path | None:
    tid = task_id.upper()
    for f in all_task_files():
        if tid in f.name.upper():
            return f
    for f in all_task_files():
        fm, _ = parse_frontmatter(f.read_text())
        if fm.get("task_id", "").upper() == tid:
            return f
    return None


def target_task_path(title: str, tid: str, status: str) -> Path:
    folder = task_folder_for_status(status)
    slug = slugify(title)
    return TASKS_DIR / folder / f"{date_prefix()}_{slug}__{tid}.md"


# ---------------------------------------------------------------------------
# Handoff prompt generator
# ---------------------------------------------------------------------------

def build_handoff_prompt(agent: dict) -> str:
    activation = agent.get("activation", {})
    identity_note = activation.get("identity_note", f"You are {agent.get('display_name', agent['id'])}.")
    bootstrap_cmd = f"python {ROOT}/bin/session_bootstrap.py --agent {agent['id']}"
    lines = [
        f"You are {agent.get('display_name', agent['id'])}.",
        "",
        identity_note,
        "",
        "Bootstrap your session by running:",
        f"  {bootstrap_cmd}",
        "",
        "Then read your inbox and execute your latest task.",
    ]
    return "\n".join(lines)


def print_handoff_prompt(agent: dict) -> None:
    border = "━" * 60
    print(f"\n{border}")
    print(f"⚠  {agent.get('display_name', agent['id'])} requires human relay.")
    print(f"   Share this prompt with the operator to activate them:")
    print(border)
    print()
    print(build_handoff_prompt(agent))
    print()
    print(border)
