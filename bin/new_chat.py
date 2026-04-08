#!/usr/bin/env python3
"""
new_chat.py — Create a new chat thread.

Usage:
  python bin/new_chat.py --title "Implement checkout flow" --project my-app
  python bin/new_chat.py --title "Research caching options" --prefix research
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    CHATS_DIR,
    ROOT,
    chat_id,
    date_prefix,
    slugify,
    utc_iso,
    write_file,
)


def parse_args():
    p = argparse.ArgumentParser(description="Create a new chat thread.")
    p.add_argument("--title", required=True, help="Chat title")
    p.add_argument("--prefix", default="", help="Optional prefix (e.g. 'research', 'workstream')")
    p.add_argument("--project", default=None, help="project_id this chat belongs to")
    return p.parse_args()


def main():
    args = parse_args()

    cid = chat_id()
    slug = slugify(args.title)
    prefix_part = f"{slugify(args.prefix)}_" if args.prefix else ""
    dir_name = f"{date_prefix()}_{prefix_part}{slug}__{cid}"
    chat_dir = CHATS_DIR / dir_name
    chat_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "chat_id": cid,
        "title": args.title,
        "project_id": args.project,
        "created_utc": utc_iso(),
    }
    write_file(chat_dir / "meta.json", json.dumps(meta, indent=2))

    overview_lines = [
        f"# {args.title}",
        "",
        f"**Chat ID**: {cid}",
        f"**Created**: {utc_iso()}",
    ]
    if args.project:
        overview_lines.append(f"**Project**: {args.project}")
    overview_lines += [
        "",
        "## Purpose",
        "",
        "(describe the goal of this thread)",
        "",
        "## Participants",
        "",
        "- ",
        "",
        "## Decisions",
        "",
        "- ",
    ]
    write_file(chat_dir / "overview.md", "\n".join(overview_lines))

    result = {
        "chat_id": cid,
        "chat_dir": str(chat_dir.relative_to(ROOT)),
        "path": str(chat_dir),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
