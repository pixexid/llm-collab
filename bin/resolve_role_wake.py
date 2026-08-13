#!/usr/bin/env python3.11
"""Resolve one native or collab project to its active orchestrator thread."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path[:0] = [str(SCRIPT_DIR), str(ROOT)]

from _helpers import get_project  # noqa: E402
from _python_runtime import require_python  # noqa: E402
from _role_generation import current_orchestrator_thread_id  # noqa: E402
from record_executed_triples import _resolve_thread_project  # noqa: E402

require_python()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    scope = result.add_mutually_exclusive_group(required=True)
    scope.add_argument("--thread-project", help="native BB project id from a thread event")
    scope.add_argument("--project", help="registered collab project id from a host producer")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.thread_project is not None:
        project_id = _resolve_thread_project(args.thread_project)
        if project_id is None:
            raise SystemExit(
                f"native bb project {args.thread_project!r} has no registered collab owner; refusing wake"
            )
    else:
        project_id = args.project
        if get_project(project_id) is None:
            raise SystemExit(f"unregistered project {project_id!r}; refusing wake")
    print(
        json.dumps(
            {
                "project_id": project_id,
                "thread_id": current_orchestrator_thread_id(project_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        raise SystemExit(1)
