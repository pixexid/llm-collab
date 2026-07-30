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

from _helpers import config_get, ensure_project, project_state_root, resolve_project_repo_path, utc_iso
from llm_collab.ledger import LedgerPaths, LedgerStore
from llm_collab.worker import (
    WorkerLookupError,
    list_workers,
    retire_worker,
    show_worker,
)
from llm_collab.codex_app_server_live_probe import probe_exact_thread
from llm_collab.codex_runtime_home import bind_runtime_home
from llm_collab.session_lifecycle import (
    CodexLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleCore,
    TrustedProjectRoot,
)


def open_store(*, writer: bool = False) -> LedgerStore:
    workspace_id = config_get("workspace_id")
    if not workspace_id:
        raise SystemExit("[error] collab.config.json lacks workspace_id")
    paths = LedgerPaths.derive(project_state_root(), str(workspace_id))
    return LedgerStore.open_writer(paths) if writer else LedgerStore.open_reader(paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-collab worker")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "list", "retire"):
        sub = commands.add_parser(name)
        sub.add_argument("--project", required=True, help="Exact project id")
    for name in ("show", "retire"):
        commands.choices[name].add_argument("worker_id")
    attach = commands.add_parser("attach", help="Attach one exact existing native session (register->reserve->consume->active)")
    for arg in ("--project", "--chat", "--participant", "--agent", "--endpoint-id",
                "--native-session", "--runtime-instance", "--codex-home",
                "--endpoint"):
        attach.add_argument(arg, required=True)
    attach.add_argument("--token", default=None)
    attach.add_argument("--repo-id", default="app")
    attach.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)
    ensure_project(args.project, allow_none=False)

    if args.command == "attach":
        import datetime as _dt
        import secrets as _secrets
        workspace_id = config_get("workspace_id")
        if not workspace_id:
            raise SystemExit("[error] collab.config.json lacks workspace_id")
        paths = LedgerPaths.derive(project_state_root(), str(workspace_id))
        repo_root = resolve_project_repo_path(args.project, args.repo_id)
        if repo_root is None:
            raise SystemExit(f"[error] no repo path for project {args.project} repo {args.repo_id}")
        runtime_home = bind_runtime_home(Path(args.codex_home))
        trusted_root = TrustedProjectRoot(args.project, args.repo_id, str(repo_root), str(repo_root))
        subject = LifecycleSubject(
            workspace_id=workspace_id, scope_kind="project", scope_identity=args.project,
            conversation_id=args.chat, participant_id=args.participant, agent_id=args.agent,
            endpoint_id=args.endpoint_id, native_session_id=args.native_session,
            runtime_instance_id=args.runtime_instance,
        )

        def probe(tid):
            return probe_exact_thread(
                tid, endpoint_url=args.endpoint, token=args.token,
                timeout_seconds=args.timeout_seconds,
            )

        provider = CodexLifecycleProvider(exact_thread_probe=probe)
        core = SessionLifecycleCore(provider)
        now = utc_iso()
        correlation_id = "corr_" + _secrets.token_urlsafe(8)
        ttl = int(provider.descriptor().get("challenge_ttl_seconds", 60))
        expiry = (_dt.datetime.fromisoformat(now).astimezone(_dt.timezone.utc) + _dt.timedelta(seconds=ttl)).isoformat()
        with LedgerStore.open_writer(paths) as store:
            revision = store.current_registry_revision(workspace_id=workspace_id)
            core.register_participant(
                store, subject, created_at_utc=now, registry_revision=revision)
            # P1-1: do NOT register_lifecycle_provider here — the provider must be
            # pre-approved in the trusted registry. reserve fails closed if absent.
            challenge = core.reserve(
                store, subject, runtime_home=runtime_home, created_at_utc=now,
                expires_at_utc=expiry, correlation_id=correlation_id,
                trusted_project_root=trusted_root)
            # ponytail: autocommit — a failed consume leaves a self-expiring dangling
            # challenge, not a binding; add a ledger transaction seam only if dangling
            # challenges ever need cleanup.
            result = core.consume(
                store, subject, challenge, runtime_home=runtime_home,
                consumed_at_utc=now, correlation_id=correlation_id,
                trusted_project_root=trusted_root)
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0

    if args.command == "retire":
        with open_store(writer=True) as store:
            try:
                retired = retire_worker(
                    store,
                    workspace_id=store.paths.workspace_id,
                    project_id=args.project,
                    worker_id=args.worker_id,
                )
            except WorkerLookupError as exc:
                print(f"[error] {exc}", file=sys.stderr)
                return 1
        for key, value in retired.items():
            print(f"{key}: {value}")
        return 0

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
