#!/usr/bin/env python3
"""
Migrate Amiga .ai-collaboration state into llm-collab.

Source format:
  - State/inbox/read/{agent}.json
  - Chats/*
  - Tasks/{active,backlog,done}/*.md
  - State/worktrees.json

Target format:
  - agents/{agent}/inbox.json
  - Chats/*
  - Tasks/{active,backlog,done}/*.md (with project_id backfilled)
  - State/worktrees.json
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_old_read_paths(path: Path) -> list[str]:
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        return [str(p) for p in payload]
    if isinstance(payload, dict):
        messages = payload.get("messages", [])
        if isinstance(messages, list):
            return [str(p) for p in messages]
    return []


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end].strip()
    body = text[end + 4 :].lstrip("\n")
    fm: dict[str, str | int | bool | None | list] = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "" or value.lower() == "null":
            fm[key] = None
        elif value.lower() == "true":
            fm[key] = True
        elif value.lower() == "false":
            fm[key] = False
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                fm[key] = []
            else:
                fm[key] = [v.strip().strip('"').strip("'") for v in inner.split(",")]
        else:
            try:
                fm[key] = int(value)
            except ValueError:
                fm[key] = value
    return fm, body


def dump_frontmatter(fm: dict, body: str) -> str:
    lines = ["---"]
    for key, value in fm.items():
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                joined = ", ".join(str(v) for v in value)
                lines.append(f"{key}: [{joined}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


def relativize_message_path(raw_path: str, source_root: Path) -> str:
    p = Path(raw_path)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(source_root.resolve()))
        except ValueError:
            return raw_path
    return raw_path


def load_target_inbox(path: Path, agent_id: str) -> dict:
    if not path.exists():
        return {"agent": agent_id, "updated_utc": utc_iso(), "unread": [], "read": []}
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        return {"agent": agent_id, "updated_utc": utc_iso(), "unread": [], "read": []}
    unread = payload.get("unread", [])
    read = payload.get("read", [])
    return {
        "agent": str(payload.get("agent") or agent_id),
        "updated_utc": str(payload.get("updated_utc") or utc_iso()),
        "unread": [str(p) for p in unread] if isinstance(unread, list) else [],
        "read": [str(p) for p in read] if isinstance(read, list) else [],
    }


def copy_chat_dirs(source: Path, workspace: Path) -> tuple[int, int]:
    source_chats = source / "Chats"
    target_chats = workspace / "Chats"
    if not source_chats.exists():
        return 0, 0
    copied = 0
    skipped = 0
    for chat_dir in sorted(source_chats.iterdir()):
        if not chat_dir.is_dir():
            continue
        dst = target_chats / chat_dir.name
        if dst.exists():
            skipped += 1
            continue
        shutil.copytree(chat_dir, dst)
        copied += 1
    return copied, skipped


def copy_memory_files(source: Path, workspace: Path, overwrite: bool = False) -> tuple[int, int, int]:
    memory_dir = source / "Memory"
    agents_dir = workspace / "agents"
    if not memory_dir.exists() or not agents_dir.exists():
        return 0, 0, 0

    copied = 0
    skipped = 0
    missing_agent = 0

    for memory_file in sorted(memory_dir.glob("*.memory.md")):
        agent_name = memory_file.name.split(".memory.md")[0].strip().lower()
        target_dir = agents_dir / agent_name
        if not target_dir.exists():
            missing_agent += 1
            continue
        target_file = target_dir / "memory.md"
        if target_file.exists() and not overwrite:
            skipped += 1
            continue
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(memory_file, target_file)
        copied += 1
    return copied, skipped, missing_agent


def copy_task_files(source: Path, workspace: Path) -> tuple[int, int]:
    copied = 0
    skipped = 0
    for folder in ("active", "backlog", "done"):
        src_dir = source / "Tasks" / folder
        dst_dir = workspace / "Tasks" / folder
        if not src_dir.exists():
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(src_dir.glob("*.md")):
            dst_file = dst_dir / src_file.name
            if dst_file.exists():
                skipped += 1
                continue
            shutil.copy2(src_file, dst_file)
            copied += 1
    return copied, skipped


def backfill_chat_project_id(workspace: Path, project_id: str) -> int:
    patched = 0
    chats_dir = workspace / "Chats"
    if not chats_dir.exists():
        return patched
    for chat_dir in chats_dir.iterdir():
        if not chat_dir.is_dir():
            continue
        meta_path = chat_dir / "meta.json"
        if not meta_path.exists():
            continue
        payload = json.loads(meta_path.read_text())
        if payload.get("project_id"):
            continue
        payload["project_id"] = project_id
        meta_path.write_text(json.dumps(payload, indent=2) + "\n")
        patched += 1
    return patched


def backfill_message_project_id(workspace: Path, project_id: str) -> int:
    patched = 0
    chats_dir = workspace / "Chats"
    if not chats_dir.exists():
        return patched
    for chat_dir in chats_dir.iterdir():
        if not chat_dir.is_dir():
            continue
        for message_file in chat_dir.glob("*.md"):
            fm, body = parse_frontmatter(message_file.read_text())
            if not fm:
                continue
            if fm.get("project_id"):
                continue
            fm["project_id"] = project_id
            message_file.write_text(dump_frontmatter(fm, body))
            patched += 1
    return patched


def backfill_task_project_id(workspace: Path, project_id: str) -> int:
    patched = 0
    for folder in ("active", "backlog", "done"):
        task_dir = workspace / "Tasks" / folder
        if not task_dir.exists():
            continue
        for task_file in task_dir.glob("*.md"):
            fm, body = parse_frontmatter(task_file.read_text())
            if not fm:
                continue
            if fm.get("project_id"):
                continue
            fm["project_id"] = project_id
            task_file.write_text(dump_frontmatter(fm, body))
            patched += 1
    return patched


def expected_task_folder(status: str | None) -> str:
    if status == "done":
        return "done"
    if status in ("open", "in_progress", "blocked", "review"):
        return "active"
    return "backlog"


def normalize_task_folders(workspace: Path) -> tuple[int, int]:
    moved = 0
    skipped_collisions = 0
    tasks_root = workspace / "Tasks"
    for folder in ("active", "backlog", "done"):
        task_dir = tasks_root / folder
        if not task_dir.exists():
            continue
        for task_file in list(task_dir.glob("*.md")):
            fm, _ = parse_frontmatter(task_file.read_text())
            if not fm:
                continue
            status = str(fm.get("status") or "open")
            target_folder = expected_task_folder(status)
            if target_folder == folder:
                continue
            target_path = tasks_root / target_folder / task_file.name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                skipped_collisions += 1
                continue
            task_file.rename(target_path)
            moved += 1
    return moved, skipped_collisions


def merge_worktrees(source: Path, workspace: Path) -> tuple[int, int]:
    source_path = source / "State" / "worktrees.json"
    target_path = workspace / "State" / "worktrees.json"
    if not source_path.exists():
        return 0, 0

    source_payload = json.loads(source_path.read_text())
    source_entries = source_payload if isinstance(source_payload, list) else source_payload.get("worktrees", [])
    if not isinstance(source_entries, list):
        source_entries = []

    if target_path.exists():
        target_payload = json.loads(target_path.read_text())
        target_entries = target_payload if isinstance(target_payload, list) else target_payload.get("worktrees", [])
        if not isinstance(target_entries, list):
            target_entries = []
    else:
        target_entries = []

    key = lambda entry: (
        str(entry.get("task_id", "")),
        str(entry.get("agent", "")),
        str(entry.get("worktree_path", "")),
        str(entry.get("branch", "")),
    )
    seen = {key(entry) for entry in target_entries if isinstance(entry, dict)}
    appended = 0
    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        k = key(entry)
        if k in seen:
            continue
        target_entries.append(entry)
        seen.add(k)
        appended += 1

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(target_entries, indent=2) + "\n")
    return len(target_entries), appended


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Amiga collaboration state into llm-collab format.")
    parser.add_argument("--source", required=True, help="Path to Amiga .ai-collaboration directory")
    parser.add_argument("--workspace", required=True, help="Path to llm-collab workspace root")
    parser.add_argument("--project-id", default="amiga", help="Project ID used to backfill migrated tasks/chats")
    parser.add_argument("--migrate-chats", action="store_true", help="Copy chat folders into workspace Chats/")
    parser.add_argument("--migrate-tasks", action="store_true", help="Copy task markdown files into workspace Tasks/")
    parser.add_argument("--migrate-worktrees", action="store_true", help="Merge State/worktrees.json entries")
    parser.add_argument("--migrate-memory", action="store_true", help="Copy Memory/*.memory.md into agents/*/memory.md")
    parser.add_argument("--overwrite-memory", action="store_true", help="Allow migrated memory files to overwrite existing target memory.md")
    parser.add_argument("--backfill-project-id", action="store_true", help="Backfill missing project_id on migrated chats/tasks")
    parser.add_argument(
        "--normalize-task-folders",
        action="store_true",
        help="Move migrated task files into folder that matches frontmatter status.",
    )
    args = parser.parse_args()

    source = Path(args.source).resolve()
    workspace = Path(args.workspace).resolve()

    read_dir = source / "State" / "inbox" / "read"
    agents_dir = workspace / "agents"

    if not read_dir.exists():
        print("[skip] No source read-state directory found.")
        return
    if not agents_dir.exists():
        raise SystemExit(f"[error] Target agents directory missing: {agents_dir}")

    inbox_migrated = 0
    for state_file in sorted(read_dir.glob("*.json")):
        agent_id = state_file.stem
        source_paths = load_old_read_paths(state_file)
        migrated_paths = [relativize_message_path(p, source) for p in source_paths]

        target_path = agents_dir / agent_id / "inbox.json"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target = load_target_inbox(target_path, agent_id)

        merged_read = sorted(set(target.get("read", [])) | set(migrated_paths))
        merged_unread = [p for p in target.get("unread", []) if p not in merged_read]

        new_payload = {
            "agent": agent_id,
            "updated_utc": utc_iso(),
            "unread": merged_unread,
            "read": merged_read,
        }
        target_path.write_text(json.dumps(new_payload, indent=2) + "\n")
        inbox_migrated += 1
        print(f"[migrated:inbox] {agent_id}: +{len(migrated_paths)} read pointers -> {target_path}")

    chats_copied = chats_skipped = 0
    tasks_copied = tasks_skipped = 0
    worktrees_total = worktrees_appended = 0
    memory_copied = memory_skipped = memory_missing_agent = 0
    chats_backfilled = 0
    messages_backfilled = 0
    tasks_backfilled = 0
    task_folders_moved = 0
    task_folders_collision = 0

    if args.migrate_chats:
        chats_copied, chats_skipped = copy_chat_dirs(source, workspace)
        print(f"[migrated:chats] copied={chats_copied} skipped_existing={chats_skipped}")

    if args.migrate_tasks:
        tasks_copied, tasks_skipped = copy_task_files(source, workspace)
        print(f"[migrated:tasks] copied={tasks_copied} skipped_existing={tasks_skipped}")

    if args.migrate_worktrees:
        worktrees_total, worktrees_appended = merge_worktrees(source, workspace)
        print(f"[migrated:worktrees] appended={worktrees_appended} total={worktrees_total}")

    if args.migrate_memory:
        memory_copied, memory_skipped, memory_missing_agent = copy_memory_files(
            source,
            workspace,
            overwrite=args.overwrite_memory,
        )
        print(
            f"[migrated:memory] copied={memory_copied} "
            f"skipped_existing={memory_skipped} missing_agent={memory_missing_agent}"
        )

    if args.backfill_project_id:
        chats_backfilled = backfill_chat_project_id(workspace, args.project_id)
        messages_backfilled = backfill_message_project_id(workspace, args.project_id)
        tasks_backfilled = backfill_task_project_id(workspace, args.project_id)
        print(
            f"[migrated:project_scope] project_id={args.project_id} "
            f"chats_patched={chats_backfilled} messages_patched={messages_backfilled} tasks_patched={tasks_backfilled}"
        )

    if args.normalize_task_folders:
        task_folders_moved, task_folders_collision = normalize_task_folders(workspace)
        print(
            f"[migrated:task_folders] moved={task_folders_moved} "
            f"skipped_collisions={task_folders_collision}"
        )

    summary = {
        "inbox_migrated_agents": inbox_migrated,
        "chats_copied": chats_copied,
        "chats_skipped_existing": chats_skipped,
        "tasks_copied": tasks_copied,
        "tasks_skipped_existing": tasks_skipped,
        "worktrees_appended": worktrees_appended,
        "worktrees_total": worktrees_total,
        "memory_copied": memory_copied,
        "memory_skipped_existing": memory_skipped,
        "memory_missing_agent": memory_missing_agent,
        "project_id": args.project_id if args.backfill_project_id else None,
        "chats_project_backfilled": chats_backfilled,
        "messages_project_backfilled": messages_backfilled,
        "tasks_project_backfilled": tasks_backfilled,
        "task_folders_moved": task_folders_moved,
        "task_folders_collision": task_folders_collision,
    }
    print("\nMigration summary:")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
