"""
Shared utilities for llm-collab bin scripts.
Workspace paths are relative to WORKSPACE_ROOT. Project runtime state may live
outside the Git checkout via collab.config.json `project_state_root`.
"""

from __future__ import annotations

import json
import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
RUNTIME_ROOT = Path(
    os.environ.get(
        "LLM_COLLAB_RUNTIME_ROOT",
        Path.home() / ".local" / "share" / "llm-collab" / "runtime" / "main",
    )
).expanduser()
CONFIG_FILE = ROOT / "collab.config.json"
AGENTS_FILE = ROOT / "agents.json"
PROJECTS_FILE = ROOT / "projects.json"
AGENTS_DIR = ROOT / "agents"
CHATS_DIR = ROOT / "Chats"
TASKS_DIR = ROOT / "Tasks"
STATE_DIR = ROOT / "State" / "inbox"
INDEX_DIR = ROOT / "Index"
AWARENESS_FILE = ROOT / "State" / "awareness.json"

TASK_FOLDERS = ("active", "backlog", "done")
TASK_STATUSES = ("open", "in_progress", "blocked", "review", "done")
TASK_PRIORITIES = ("low", "normal", "high", "urgent")
RELEASE_EVIDENCE_VERDICTS = (
    "success",
    "risk-accepted-followup",
    "non-production",
)
RELEASE_EVIDENCE_FIELDS = {"merge_sha", "verdict", "run_id", "note"}


def parse_release_evidence(raw: str | None) -> dict:
    """Parse and strictly validate one release-evidence JSON object."""
    if raw is None:
        raise ValueError("--release-evidence is required for a done transition")
    try:
        evidence = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(
            "--release-evidence must be one valid JSON object, not free text"
        ) from error
    if not isinstance(evidence, dict):
        raise ValueError("--release-evidence must decode to one JSON object")

    unknown = sorted(set(evidence) - RELEASE_EVIDENCE_FIELDS)
    if unknown:
        raise ValueError(
            "--release-evidence contains unknown field(s): " + ", ".join(unknown)
        )

    merge_sha = evidence.get("merge_sha")
    if not isinstance(merge_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", merge_sha) is None:
        raise ValueError("release evidence merge_sha must be exactly 40 hex characters")

    verdict = evidence.get("verdict")
    if verdict not in RELEASE_EVIDENCE_VERDICTS:
        allowed = "|".join(RELEASE_EVIDENCE_VERDICTS)
        raise ValueError(f"release evidence verdict must be one of {allowed}")

    run_id = evidence.get("run_id")
    if run_id is not None and (
        isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0
    ):
        raise ValueError("release evidence run_id must be a positive strict integer")
    if verdict == "success" and run_id is None:
        raise ValueError("release evidence run_id is required for verdict=success")

    if "note" in evidence:
        note = evidence["note"]
        if not isinstance(note, str) or not note.strip():
            raise ValueError("release evidence note must be a non-empty string when provided")
        evidence["note"] = note.strip()

    evidence["merge_sha"] = merge_sha.lower()
    return evidence


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


def _expand_config_path(raw: str, *, base: Path = ROOT) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()


def project_state_root() -> Path:
    """Directory that stores local project runtime state outside tracked repo files."""
    raw = config_get("project_state_root")
    if raw:
        return _expand_config_path(str(raw))
    return ROOT / "projects"


def project_state_dir(project_id: str) -> Path:
    return project_state_root() / project_id


def display_path(path: Path) -> str:
    """Return a stable human-facing path, relative to ROOT when possible."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def python_cmd() -> str:
    """Best-effort python launcher command for human-facing snippets."""
    if shutil.which("python3"):
        return "python3"
    if shutil.which("python"):
        return "python"
    return "python3"


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


def is_agent_disabled(agent: dict) -> bool:
    activation = agent.get("activation", {})
    role = str(agent.get("role", ""))
    return (
        agent.get("disabled") is True
        or activation.get("enabled") is False
        or role.startswith("legacy_disabled")
    )


def ensure_agent_enabled(agent_id: str, *, context: str) -> dict:
    agent = get_agent(agent_id)
    if is_agent_disabled(agent):
        print(
            f"[error] Agent {agent_id!r} is disabled for {context}. "
            "Re-enable it explicitly in agents.json before routing work to it.",
            file=sys.stderr,
        )
        sys.exit(1)
    return agent


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


def resolve_project_repo_path(project_id: str, repo_key: str = "app") -> Path | None:
    project = get_project(project_id)
    if not project:
        return None
    repos = project.get("repos")
    if not isinstance(repos, dict):
        return None
    raw = repos.get(repo_key)
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return path.resolve()
    if path.parts and path.parts[0] == "..":
        return (ROOT / path).resolve()
    projects_root = config_get("projects_root")
    base = _expand_config_path(str(projects_root)) if projects_root else ROOT
    return (base / path).resolve()


def _read_repo_nvmrc(repo_root: Path) -> str | None:
    nvmrc_path = repo_root / ".nvmrc"
    if not nvmrc_path.exists():
        return None
    raw = nvmrc_path.read_text().strip()
    if not raw:
        return None
    return raw.removeprefix("v")


def _parse_semver_parts(raw: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", raw)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _resolve_repo_runtime_bin_dir(repo_root: Path) -> Path | None:
    version = _read_repo_nvmrc(repo_root)
    if not version:
        return None

    exact_candidates = [
        Path.home() / ".nvm" / "versions" / "node" / f"v{version}" / "bin",
        Path.home() / ".local" / f"node-v{version}" / "bin",
    ]

    for candidate in exact_candidates:
        if candidate.exists():
            return candidate

    version_parts = version.split(".")
    if len(version_parts) != 3:
        return None

    major = version_parts[0]
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        matching = [
            (parsed, path / "bin")
            for path in nvm_root.iterdir()
            if path.is_dir()
            and path.name.startswith(f"v{major}.")
            and (parsed := _parse_semver_parts(path.name)) is not None
        ]
        if matching:
            return max(matching, key=lambda entry: entry[0])[1]

    local_node_root = Path.home() / ".local"
    if local_node_root.exists():
        matching = [
            (parsed, path / "bin")
            for path in local_node_root.glob(f"node-v{major}.*.*")
            if (parsed := _parse_semver_parts(path.name.removeprefix("node-"))) is not None
        ]
        if matching:
            return max(matching, key=lambda entry: entry[0])[1]

    return None


def _resolve_command_path(command: str, repo_root: Path | None = None) -> str:
    if "/" in command:
        return command

    runtime_bin_dir = _resolve_repo_runtime_bin_dir(repo_root) if repo_root else None
    if runtime_bin_dir:
        candidate = runtime_bin_dir / command
        if candidate.exists():
            return str(candidate)

    direct = shutil.which(command)
    if direct:
        return direct

    candidates = [
        Path("/opt/homebrew/bin") / command,
        Path("/usr/local/bin") / command,
        Path.home() / ".local" / "bin" / command,
    ]

    local_node_root = Path.home() / ".local"
    if local_node_root.exists():
        candidates.extend(sorted(local_node_root.glob(f"node-v*/bin/{command}")))

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        candidates.extend(sorted(nvm_root.glob(f"*/bin/{command}")))

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return command


def _build_command_env(command_path: str, repo_root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    existing = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    preferred = [str(Path(command_path).resolve().parent)]

    runtime_bin_dir = _resolve_repo_runtime_bin_dir(repo_root) if repo_root else None
    if runtime_bin_dir:
        preferred.append(str(runtime_bin_dir))

    preferred.extend(["/opt/homebrew/bin", "/usr/local/bin"])
    deduped = []
    for entry in [*preferred, *existing]:
        if entry and entry not in deduped:
            deduped.append(entry)
    env["PATH"] = os.pathsep.join(deduped)
    return env


def run_project_preflight(
    project_id: str | None,
    *,
    cwd: Path | None = None,
    extra_args: list[str] | None = None,
) -> dict:
    if not project_id:
        return {"ran": False, "reason": "task/message has no project_id"}
    project = get_project(project_id)
    if not project:
        return {"ran": False, "reason": f"unknown project_id: {project_id}"}
    command = project.get("preflight_command")
    if not command:
        return {"ran": False, "reason": f"project {project_id} has no preflight_command"}
    if not isinstance(command, list) or not all(isinstance(token, str) for token in command):
        return {"ran": False, "reason": f"project {project_id} preflight_command must be list[str]"}

    run_cwd = (cwd or resolve_project_repo_path(project_id, "app") or ROOT).resolve()
    full_command = [*command, *(extra_args or [])]
    command_path = _resolve_command_path(full_command[0], run_cwd)
    executed_command = [command_path, *full_command[1:]]
    result = subprocess.run(
        executed_command,
        cwd=run_cwd,
        text=True,
        capture_output=True,
        check=False,
        env=_build_command_env(command_path, run_cwd),
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    parsed_json = None
    if stdout:
        try:
            parsed_json = json.loads(stdout)
        except json.JSONDecodeError:
            parsed_json = None

    return {
        "ran": True,
        "ok": result.returncode == 0,
        "command": executed_command,
        "cwd": str(run_cwd),
        "returncode": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "json": parsed_json,
    }


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
    data.pop("_durability_pending", None)
    data["updated_utc"] = utc_iso()
    pending = {**data, "_durability_pending": True}
    write_file_durably(path, json.dumps(pending, indent=2))
    write_file_durably(path, json.dumps(data, indent=2))


@contextlib.contextmanager
def inbox_write_lock(agent_id: str):
    """Serialize the load-modify-save on one agent's inbox index.

    Delivery (add_to_inbox) and the Pi acknowledgment drain (mark_messages_read)
    both read the index, modify it, and write it back. Unlocked, a delivery that
    reads between an acknowledgment's read and write is lost, or an acknowledgment
    resurrects a packet a concurrent delivery added — the same lost-update the Pi
    coalesced-wake path exposes. One blocking flock per agent inbox makes each RMW
    atomic against the others. ponytail: per-agent lock, which is as narrow as the
    contention is (all writers to one inbox), so no finer granularity is needed.
    """
    lock_path = agent_inbox_path(agent_id).with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def add_to_inbox(agent_id: str, message_path: str | Path) -> None:
    """Append a message path (relative to ROOT) to the agent's unread list."""
    rel = str(Path(message_path).relative_to(ROOT)) if Path(message_path).is_absolute() else str(message_path)
    with inbox_write_lock(agent_id):
        inbox = load_agent_inbox(agent_id)
        if rel not in inbox["unread"] and rel not in inbox["read"]:
            inbox["unread"].append(rel)
        save_agent_inbox(agent_id, inbox)


def mark_messages_read(agent_id: str, paths: list[str]) -> None:
    with inbox_write_lock(agent_id):
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
# Recipient awareness state (local-only, runtime)
# ---------------------------------------------------------------------------

def load_awareness_state() -> dict:
    if not AWARENESS_FILE.exists():
        return {"version": 1, "agents": {}}
    try:
        payload = json.loads(AWARENESS_FILE.read_text())
    except json.JSONDecodeError:
        return {"version": 1, "agents": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "agents": {}}
    agents = payload.get("agents")
    if not isinstance(agents, dict):
        payload["agents"] = {}
    return payload


def save_awareness_state(payload: dict) -> None:
    AWARENESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_file(AWARENESS_FILE, json.dumps(payload, indent=2))


def has_collab_awareness(agent_id: str) -> bool:
    state = load_awareness_state()
    agents = state.get("agents", {})
    if agent_id in agents:
        entry = agents.get(agent_id, {})
        return bool(isinstance(entry, dict) and entry.get("aware", False))
    return False


def set_collab_awareness(agent_id: str, message_path: str | Path) -> None:
    state = load_awareness_state()
    agents = state.get("agents", {})
    rel = str(Path(message_path).relative_to(ROOT)) if Path(message_path).is_absolute() else str(message_path)
    agents[agent_id] = {
        "aware": True,
        "updated_utc": utc_iso(),
        "source": "onboarding_message",
        "message_path": rel,
    }
    state["agents"] = agents
    save_awareness_state(state)


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


def write_file_durably(path: Path, content: str) -> None:
    """Atomically replace one file and persist the replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
            try:
                parsed = json.loads(v)
                fm[k] = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                inner = v[1:-1].strip()
                fm[k] = [i.strip().strip('"').strip("'") for i in inner.split(",") if i.strip()] if inner else []
        elif v.startswith("{") and v.endswith("}"):
            try:
                parsed = json.loads(v)
                fm[k] = parsed if isinstance(parsed, dict) else v
            except json.JSONDecodeError:
                fm[k] = v
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
                inner = ", ".join(json.dumps(i, ensure_ascii=True) for i in v)
                lines.append(f"{k}: [{inner}]")
        elif isinstance(v, dict):
            lines.append(
                f"{k}: {json.dumps(v, ensure_ascii=True, separators=(',', ':'))}"
            )
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


def chat_id_of(chat_dir: Path) -> str | None:
    """The chat id a directory actually carries: the token after the final `__`.

    Never re-derive the id GRAMMAR here. An earlier version matched
    `CHAT-[0-9A-Za-z]+$`, which does not match the hyphenated ids this workspace
    produces -- CHAT-CLAUDE-CODEX, CHAT-BIND-SAFE, CHAT-READY-DRIFT. Both duplicates of
    such an id parsed as None, so neither was an exact match and newest-wins applied
    silently: the collision guard was absent for a whole class of ids while appearing to
    be present. The naming convention is `<date>_<slug>__<CHAT-ID>`, so splitting on the
    final separator needs no grammar at all.
    """
    _, separator, tail = chat_dir.name.rpartition("__")
    return tail if separator and tail else None


def chat_meta_project(chat_dir: Path) -> str | None:
    """The project a directory's metadata claims, or None when it cannot be established.

    Unreadable or malformed metadata is a NON-MATCH, never an error: an unrelated directory
    sharing a chat id could otherwise raise during filtering and block a perfectly valid chat
    in the project the caller actually named.
    """
    try:
        meta = load_chat_meta(chat_dir)
    except (OSError, ValueError):
        # Only unreadable or undecodable metadata. A bare `except Exception` here masked
        # programming defects as "every scoped chat disappeared", which is both a silent
        # failure and an extremely confusing one. json.JSONDecodeError is a ValueError.
        return None
    if not isinstance(meta, dict):
        return None
    project = meta.get("project_id")
    return str(project) if project else None


def _collision_message(partial: str, candidates: list, project: str | None) -> str:
    names = "\n  ".join(sorted(d.name for d in candidates))
    scope = f" within project {project}" if project else ""
    return (
        f"chat id {partial} matches {len(candidates)} directories{scope}; ids must be unique "
        f"there:\n  {names}\n"
        "Merge or re-id the duplicates before sending -- delivery will not guess."
    )


def find_chat_by_partial(partial: str, *, project: str | None = None) -> Path | None:
    """Resolve a chat selector to one directory.

    Two matches for one chat id mean the workspace is corrupt: ids must be unique, and
    picking the newest silently routed delivery into whichever directory sorted last.
    That is how mail reaches the wrong receiver -- and how a chat whose duplicate lacked
    meta.json blocked delivery entirely with an error naming a directory the sender never
    chose. Refuse instead of guessing.

    The decision is made by comparing the id each directory ACTUALLY carries, never by
    the shape of the selector. Testing the selector's shape got all three edge cases
    wrong at once: a lowercase selector failed the uppercase pattern and bypassed the
    refusal entirely, an id-shaped prefix like `CHAT-89` was refused although it is an
    ordinary loose match, and a directory whose title merely mentions another id counted
    toward the collision. Loose partials keep newest-wins -- that is human lookup, not
    delivery addressing.
    """
    matches = find_chats(partial) if partial != "last" else find_chats()
    selector = partial.strip().casefold()

    # Who BEARS this id is a global fact, established before any scoping. The pre-filter
    # discarded that knowledge, so an id borne only in another project left an in-scope
    # directory that merely MENTIONS it in its title -- `followup-CHAT-X__CHAT-Y` -- and the
    # loose fallback then returned CHAT-Y. A caller who named an id must never be handed a
    # different chat because the one they named lives elsewhere.
    bearers = ([] if partial == "last"
               else [d for d in matches if (chat_id_of(d) or "").casefold() == selector])

    if project is not None:
        # ONE pre-filter, before any selection. Filtering after exact-id selection left the
        # `last` and loose paths unscoped, so they could still choose another project's chat
        # and only fail later at delivery. Scope is a property of the candidate SET, not a
        # tie-breaker applied to a winner.
        scoped = [d for d in matches if chat_meta_project(d) == project]
    else:
        scoped = list(matches)

    if not scoped:
        return None
    if partial == "last":
        return scoped[-1]

    scoped_names = {d.name for d in scoped}
    exact = [d for d in bearers if d.name in scoped_names]
    if len(exact) > 1:
        raise ValueError(_collision_message(partial, exact, project))
    if exact:
        return exact[0]
    if bearers:
        # the id exists, but not here. Falling through to a loose match would deliver into a
        # directory the caller never named.
        return None
    return scoped[-1]

    selector = partial.strip().casefold()
    exact = [d for d in matches if (chat_id_of(d) or "").casefold() == selector]
    if len(exact) > 1:
        raise ValueError(_collision_message(partial, exact, project))
    if exact:
        return exact[0]
    return matches[-1]


def load_chat_meta(chat_dir: Path) -> dict:
    meta_file = chat_dir / "meta.json"
    if meta_file.exists():
        return read_json(meta_file)
    return {}


def write_chat_note(
    chat_dir: Path,
    *,
    title: str,
    body: str,
    sender: str = "system",
    recipient: str = "operator",
    priority: str = "normal",
    tags: list[str] | None = None,
    project_id: str | None = None,
    extra_frontmatter: dict[str, Any] | None = None,
) -> Path:
    meta = load_chat_meta(chat_dir)
    chat_id = str(meta.get("chat_id", chat_dir.name))
    resolved_project_id = project_id or meta.get("project_id")
    fm: dict[str, Any] = {
        "chat_id": chat_id,
        "from": sender,
        "to": recipient,
        "title": title,
        "priority": priority,
        "tags": tags or [],
        "project_id": resolved_project_id,
        "sent_utc": utc_iso(),
        "informational": True,
    }
    if extra_frontmatter:
        fm.update(extra_frontmatter)
    timestamp = ts()
    slug = slugify(title, max_len=40)
    path = chat_dir / f"{timestamp}_note-{sender}_{slug}.md"
    write_file(path, dump_frontmatter(fm, body.strip() or "(no body)"))
    return path


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

def collab_join_skill_path() -> Path:
    return ROOT / "skills" / "llm-collab-join" / "SKILL.md"


def collab_bootstrap_command(agent_id: str) -> str:
    return f"{RUNTIME_ROOT}/bin/llm-collab current_runtime.py --agent {agent_id}"


def build_handoff_prompt(
    agent: dict,
    *,
    sender_id: str | None = None,
    first_time: bool = False,
) -> str:
    activation = agent.get("activation", {})
    agent_id = str(agent["id"])
    display_name = str(agent.get("display_name", agent_id))

    identity_note = activation.get(
        "identity_note",
        f"You are {display_name} ({agent_id}). Read only messages addressed to '{agent_id}'.",
    )
    bootstrap_cmd = collab_bootstrap_command(agent_id)
    join_skill = collab_join_skill_path()
    memory_path = f"{ROOT}/agents/{agent_id}/memory.md"
    if first_time:
        lines = [
            identity_note,
            "",
            "First-time setup required before task work:",
            f"1) Read worker contract: {ROOT}/AGENTS.md",
            f"2) Read docs: {ROOT}/README.md",
            f"3) Read docs: {ROOT}/docs/multi-project.md",
            f"4) Read docs: {ROOT}/docs/getting-started.md",
            f"5) Read docs: {ROOT}/docs/identity-system.md",
            f"6) Read docs: {ROOT}/docs/workflows/README.md",
            "7) Update memory files now as thin pointers:",
            "   - Your main/global memory file for this model account.",
            "   - The repo/project memory file used when working on this project.",
            f"   - Local collab memory file: {memory_path}",
            "",
            "Memory updates must point at:",
            f"- Join skill: {join_skill}",
            f"- Bootstrap: {bootstrap_cmd}",
            "- Rule: keep memory files as pointers; do not restate collab command families.",
            "- Rule: always bootstrap and check inbox at session start.",
            "- Rule: project-scoped is the default; never reuse another project's paths, DB refs, tools, or policy.",
            "",
            "Then bootstrap now and execute your latest inbox message.",
            f"  {bootstrap_cmd}",
        ]
    else:
        lines = [
            identity_note,
            "",
            "Please check your inbox now and execute the latest task.",
            f"  {bootstrap_cmd}",
        ]
    return "\n".join(lines)


def print_handoff_prompt(
    agent: dict,
    *,
    sender_id: str | None = None,
    first_time: bool = False,
) -> None:
    border = "━" * 60
    print(f"\n{border}")
    print(f"⚠  {agent.get('display_name', agent['id'])} requires human relay.")
    print(f"   Share this prompt with the operator to activate them:")
    print(border)
    print()
    print(build_handoff_prompt(agent, sender_id=sender_id, first_time=first_time))
    print()
    print(border)

def canonical_path(value, base=None):
    """The single path invariant shared by every llm-collab path comparison.

    Absolute (resolved against the repository root, never the caller's cwd), redundant
    segments collapsed, no trailing separator, symlinks deliberately unresolved because
    delivery discovery matches the launched spelling literally.

    This lives here, imported by both bin/pm2_watchers.py and bin/session_autobridge.py
    and mirrored by canonicalPath() in pm2/ecosystem.config.cjs, because six separate
    defects came from normalizing one side of a two-sided comparison and calling the
    concern closed. A second copy is how the seventh happens.

    One tilde grammar only: exact ``~`` or a ``~/`` prefix. ``~user`` forms stay literal
    because os.path.expanduser accepts them and the CJS mirror cannot, and a form that
    canonicalizes to an absolute home on one side and a repo-relative ``~user`` directory
    on the other is the validate-one-path/use-another defect wearing a different hat.

    Leading separator runs collapse to one, and a ``~/`` tail's own leading run is
    stripped before joining. Both exist because Node and Python disagree by default:
    ``os.path.join(home, "/x")`` discards home while ``path.join`` keeps it, and
    ``normpath`` preserves exactly two leading slashes per POSIX while ``path.resolve``
    collapses them. ``~//x`` and ``//tmp/codex-home`` each canonicalized to a different
    literal on each side, which is the same one-sided-normalization defect as the rest.
    """
    import os as _os

    text = str(value).strip()
    if text == "~" or text.startswith("~/"):
        home = _os.path.expanduser("~")
        text = home if text == "~" else _os.path.join(home, text[2:].lstrip("/"))
    root = str(base) if base is not None else str(ROOT)
    joined = text if _os.path.isabs(text) else _os.path.join(root, text)
    normalised = re.sub(r"^/+", "/", _os.path.normpath(joined))
    return Path(normalised)
