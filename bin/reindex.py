#!/usr/bin/env python3
"""
reindex.py — Regenerate Index/index.md from all chats and tasks.

Usage:
  python bin/reindex.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    INDEX_DIR,
    ROOT,
    all_task_files,
    find_chats,
    load_chat_meta,
    parse_frontmatter,
    utc_iso,
    write_file,
)


def count_messages(chat_dir: Path) -> int:
    return len(list(chat_dir.glob("*.md"))) - 1  # exclude overview.md


def main():
    lines = [
        "# Collaboration Index",
        "",
        f"_Generated: {utc_iso()}_",
        "",
        "## Chats",
        "",
        "| Chat ID | Title | Project | Messages | Created |",
        "|---------|-------|---------|----------|---------|",
    ]

    for chat_dir in find_chats():
        meta = load_chat_meta(chat_dir)
        cid = meta.get("chat_id", "?")
        title = meta.get("title", chat_dir.name)
        project = meta.get("project_id", "")
        created = meta.get("created_utc", "")[:10]
        msg_count = count_messages(chat_dir)
        lines.append(f"| {cid} | {title} | {project} | {msg_count} | {created} |")

    lines += [
        "",
        "## Tasks",
        "",
        "| Task ID | Status | Owner | Priority | Project | Title |",
        "|---------|--------|-------|----------|---------|-------|",
    ]

    for f in all_task_files():
        fm, _ = parse_frontmatter(f.read_text())
        if not fm.get("task_id"):
            continue
        lines.append(
            f"| {fm.get('task_id','')} | {fm.get('status','')} | {fm.get('owner','')} | "
            f"{fm.get('priority','')} | {fm.get('project_id','')} | {fm.get('title','')} |"
        )

    output = "\n".join(lines) + "\n"
    out_path = INDEX_DIR / "index.md"
    write_file(out_path, output)
    print(f"[reindex] Written: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
