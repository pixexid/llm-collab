#!/usr/bin/env python3
"""
Migrate inbox read-state from Amiga .ai-collaboration to llm-collab.

Source format:
  State/inbox/read/{agent}.json
  - either list[str] of message paths
  - or {"messages": list[str]}

Target format:
  agents/{agent}/inbox.json
  - preserves any existing unread pointers
  - merges read pointers (deduplicated)
"""

from __future__ import annotations

import argparse
import json
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Amiga inbox read-state into llm-collab format.")
    parser.add_argument("--source", required=True, help="Path to Amiga .ai-collaboration directory")
    parser.add_argument("--workspace", required=True, help="Path to llm-collab workspace root")
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

    migrated = 0
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
        migrated += 1
        print(f"[migrated] {agent_id}: +{len(migrated_paths)} read pointers -> {target_path}")

    print(f"\nDone. Migrated {migrated} agent inbox file(s).")


if __name__ == "__main__":
    main()

