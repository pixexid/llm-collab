#!/usr/bin/env python3
"""
Legacy compatibility wrapper for old `.ai-collaboration/bin/msg.py`.

Maps the old interface to `deliver.py` and prints a deprecation notice.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import find_chat_by_partial, load_chat_meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Legacy wrapper for deliver.py")
    parser.add_argument("--chat", default="last", help="Chat selector: 'last' or substring")
    parser.add_argument("--from", dest="from_agent", default="you", help="Sender identity")
    parser.add_argument("--to", dest="to_agent", required=True, help="Recipient identity")
    parser.add_argument("--title", required=True, help="Short semantic title")
    parser.add_argument("--priority", default="normal", choices=["low", "normal", "high", "urgent"])
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--project", default=None, help="project_id this message belongs to")
    parser.add_argument("--related-task", default=None, help="TASK-id cross-reference")
    parser.add_argument("--repo-targets", default="", help="Comma-separated repo IDs")
    parser.add_argument("--path-targets", default="", help="Comma-separated file/dir paths")
    parser.add_argument("--body-file", default="-", help="Path to markdown body file or '-' for stdin")
    parser.add_argument("--direction", default=None, help="Ignored legacy option")
    return parser.parse_args()


def resolve_project(chat_selector: str, explicit_project: str | None) -> str:
    if explicit_project:
        return explicit_project
    chat_dir = find_chat_by_partial(chat_selector)
    if chat_dir is None:
        raise SystemExit("[error] Could not resolve chat for project inference; pass --project explicitly.")
    chat_meta = load_chat_meta(chat_dir)
    project_id = chat_meta.get("project_id")
    if not project_id:
        raise SystemExit(
            "[error] Chat has no project_id and --project was not provided. "
            "Create/use a project-scoped chat or pass --project."
        )
    return str(project_id)


def main() -> None:
    args = parse_args()
    sender = "operator" if args.from_agent == "you" else args.from_agent
    if args.from_agent == "you":
        print("[warn] '--from you' is deprecated; using sender 'operator'.", file=sys.stderr)
    if args.direction:
        print("[warn] '--direction' is ignored by msg.py compatibility wrapper.", file=sys.stderr)

    project_id = resolve_project(args.chat, args.project)

    cmd = [
        sys.executable,
        str(Path(__file__).parent / "deliver.py"),
        "--chat",
        args.chat,
        "--from",
        sender,
        "--to",
        args.to_agent,
        "--title",
        args.title,
        "--priority",
        args.priority,
        "--tags",
        args.tags,
        "--project",
        project_id,
        "--repo-targets",
        args.repo_targets,
        "--path-targets",
        args.path_targets,
    ]
    if args.related_task:
        cmd.extend(["--related-task", args.related_task])
    if args.body_file and args.body_file != "-":
        cmd.extend(["--body-file", args.body_file])

    stdin_data = None
    if args.body_file == "-":
        stdin_data = sys.stdin.read()

    print("[deprecated] msg.py -> deliver.py compatibility mode", file=sys.stderr)
    result = subprocess.run(cmd, text=True, input=stdin_data, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

