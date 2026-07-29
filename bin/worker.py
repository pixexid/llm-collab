#!/usr/bin/env python3
"""worker.py — read-only `llm-collab worker show|list` over the canonical ledger (GH-396).

No provider mutation, no runtime injection; the report comes from the existing
canonical resolver/operator inspection via a query-only reader.

  python bin/worker.py list --project llm-collab
  python bin/worker.py show worker_<id> --project llm-collab
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _helpers import config_get, ensure_project, project_state_root
from llm_collab.ledger import LedgerPaths, LedgerStore
from llm_collab.worker import WorkerLookupError, list_workers, show_worker


def open_store() -> LedgerStore:
    workspace_id = config_get("workspace_id")
    if not workspace_id:
        raise SystemExit("[error] collab.config.json lacks workspace_id")
    return LedgerStore.open_reader(
        LedgerPaths.derive(project_state_root(), str(workspace_id))
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-collab worker")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "list"):
        sub = commands.add_parser(name)
        sub.add_argument("--project", required=True, help="Exact project id")
    commands.choices["show"].add_argument("worker_id")
    args = parser.parse_args(argv)
    ensure_project(args.project, allow_none=False)

    with open_store() as store:
        if args.command == "list":
            workers = list_workers(
                store, workspace_id=store.paths.workspace_id, project_id=args.project
            )
            if not workers:
                print(f"no workers in project {args.project}")
                return 0
            for w in workers:
                print(
                    f"{w['worker_id']}  {w['agent_id']}  {w['conversation_id']}  "
                    f"{w['participant_id']}  {w['state'] or w['reason']}"
                )
            return 0
        try:
            worker = show_worker(
                store,
                workspace_id=store.paths.workspace_id,
                project_id=args.project,
                worker_id=args.worker_id,
            )
        except WorkerLookupError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        for key, value in worker.items():
            print(f"{key}: {value}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
