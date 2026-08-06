#!/usr/bin/env python3.11
"""Start one exact Codex worker thread through the lifecycle reservation saga."""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import math
import secrets
import subprocess
import urllib.parse
from pathlib import Path

from _helpers import (
    config_get,
    project_state_root,
    resolve_project_repo_path,
    utc_iso,
)
from llm_collab.codex_app_server_live_probe import (
    CodexAppServerExactThreadResult,
    _WebSocketJsonRpcTransport,
    probe_exact_thread,
)
from llm_collab.codex_app_server_managed_start import (
    ManagedCodexStartConfig,
    ManagedCodexStartTransport,
    SUPPORTED_CODEX_CLI_VERSIONS,
)
from llm_collab.codex_runtime_home import bind_runtime_home
from llm_collab.codex_session_ref import open_repository_descriptor_chain
from llm_collab.ledger import LedgerPaths, LedgerStore
from llm_collab.session_lifecycle import (
    CODEX_MANAGED_START_PROVIDER_REVISION,
    CODEX_MANAGED_START_SUPPORTED_OPERATIONS_JSON,
    CodexLifecycleProvider,
    ManagedStartRequest,
    SessionLifecycleCore,
    TrustedProjectRoot,
)
from llm_collab.worker import WorkerLookupError, show_worker


DEFAULT_CODEX_CLI_VERSION = "0.147.0-alpha.1.2"
DEFAULT_USER_AGENT_PRODUCT = "Codex Desktop"
START_TTL_SECONDS = 180
MAX_GIT_OUTPUT_BYTES = 8192


class CodexWorkerStartError(RuntimeError):
    pass


def add_worker_codex_arguments(commands: argparse._SubParsersAction) -> None:
    approve = commands.add_parser(
        "approve-codex-start",
        help="Pre-approve the exact managed Codex start provider revision",
    )
    approve.add_argument("--project", required=True)

    start = commands.add_parser(
        "start-codex", help="Create and bind one exact Codex worker thread"
    )
    start.add_argument("worker_id")
    start.add_argument("--project", required=True)
    start.add_argument("--endpoint", required=True)
    start.add_argument("--endpoint-id", required=True)
    start.add_argument("--runtime-instance", required=True)
    start.add_argument("--codex-home", required=True)
    start.add_argument("--token-file")
    start.add_argument("--repo-id", default="app")
    start.add_argument("--cwd", required=True)
    start.add_argument("--model", required=True)
    start.add_argument("--model-provider", default="openai")
    start.add_argument("--timeout-seconds", type=float, default=30.0)
    start.add_argument("--expected-cli-version", default=DEFAULT_CODEX_CLI_VERSION)
    start.add_argument(
        "--user-agent-product",
        default=DEFAULT_USER_AGENT_PRODUCT,
        help="Exact App Server user-agent product before /<version>",
    )


def _provider(exact_thread_probe) -> CodexLifecycleProvider:
    return CodexLifecycleProvider(
        exact_thread_probe=exact_thread_probe,
        provider_revision=CODEX_MANAGED_START_PROVIDER_REVISION,
        supported_operations_json=CODEX_MANAGED_START_SUPPORTED_OPERATIONS_JSON,
        challenge_ttl_seconds=START_TTL_SECONDS,
    )


def _workspace() -> tuple[str, LedgerPaths]:
    workspace_id = config_get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise CodexWorkerStartError("collab.config.json lacks workspace_id")
    return workspace_id, LedgerPaths.derive(project_state_root(), workspace_id)


def approve_codex_start(args: argparse.Namespace) -> int:
    workspace_id, paths = _workspace()
    provider = _provider(
        lambda _thread_id: (_ for _ in ()).throw(
            AssertionError("provider approval must not probe a native thread")
        )
    )
    with LedgerStore.open_writer(paths) as store:
        store.register_lifecycle_provider(
            workspace_id=workspace_id,
            provider_descriptor=provider.descriptor(),
            created_at_utc=utc_iso(),
        )
    print(
        json.dumps(
            {
                "project_id": args.project,
                "provider_id": provider.provider_id,
                "provider_revision": provider.provider_revision,
                "supported_operations": ["start"],
                "approved": True,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _git_line(path: Path, argument: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", argument],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodexWorkerStartError("repository identity probe failed") from error
    if (
        result.returncode != 0
        or len(result.stdout) > MAX_GIT_OUTPUT_BYTES
        or len(result.stderr) > MAX_GIT_OUTPUT_BYTES
    ):
        raise CodexWorkerStartError("repository identity probe was refused")
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise CodexWorkerStartError("repository identity is not UTF-8") from error
    if len(lines) != 1 or not lines[0] or "\x00" in lines[0]:
        raise CodexWorkerStartError("repository identity probe was ambiguous")
    return lines[0]


def _git_path(path: Path, argument: str) -> Path:
    value = Path(_git_line(path, argument)).expanduser()
    if not value.is_absolute():
        value = path / value
    try:
        resolved = value.resolve(strict=True)
    except OSError as error:
        raise CodexWorkerStartError("repository identity path is unavailable") from error
    return resolved


def _trusted_worktree(project: str, repo_id: str, cwd_value: str) -> TrustedProjectRoot:
    configured = resolve_project_repo_path(project, repo_id)
    if configured is None:
        raise CodexWorkerStartError(
            f"no repo path for project {project} repo {repo_id}"
        )
    try:
        configured_root = Path(configured).expanduser().resolve(strict=True)
        cwd = Path(cwd_value).expanduser().resolve(strict=True)
    except OSError as error:
        raise CodexWorkerStartError("configured repository or cwd is unavailable") from error
    if not configured_root.is_dir() or not cwd.is_dir():
        raise CodexWorkerStartError("configured repository and cwd must be directories")
    if _git_path(configured_root, "--show-toplevel") != configured_root:
        raise CodexWorkerStartError("configured repository path is not its Git root")
    candidate_root = _git_path(cwd, "--show-toplevel")
    configured_common = _git_path(configured_root, "--git-common-dir")
    candidate_common = _git_path(cwd, "--git-common-dir")
    if candidate_common != configured_common:
        raise CodexWorkerStartError("cwd is not a worktree of the configured repository")
    try:
        chain = open_repository_descriptor_chain(candidate_root, cwd)
    except (OSError, ValueError) as error:
        raise CodexWorkerStartError("repository authority could not be pinned") from error
    return TrustedProjectRoot(
        project,
        repo_id,
        str(candidate_root),
        str(cwd),
        descriptor_chain=chain,
    )


def _loopback_endpoint(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "ws" or not parsed.hostname:
        raise CodexWorkerStartError("Codex App Server endpoint must be loopback ws://")
    if parsed.hostname != "localhost":
        try:
            if not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise CodexWorkerStartError(
                    "Codex App Server endpoint must be loopback ws://"
                )
        except ValueError as error:
            raise CodexWorkerStartError(
                "Codex App Server endpoint must be loopback ws://"
            ) from error
    return value


def _worker_request(
    paths: LedgerPaths,
    *,
    workspace_id: str,
    project: str,
    worker_id: str,
    endpoint_id: str,
    runtime_instance_id: str,
) -> ManagedStartRequest:
    try:
        with LedgerStore.open_reader(paths) as store:
            worker = show_worker(
                store,
                workspace_id=workspace_id,
                project_id=project,
                worker_id=worker_id,
            )
    except WorkerLookupError as error:
        raise CodexWorkerStartError(str(error)) from error
    if worker.get("resolved") or worker.get("reason") not in {
        "waiting_for_session",
        "pull_pending",
    }:
        raise CodexWorkerStartError(
            "worker already has a live or unresolved binding; resume or retire it"
        )
    return ManagedStartRequest(
        workspace_id=workspace_id,
        scope_kind="project",
        scope_identity=project,
        conversation_id=str(worker["conversation_id"]),
        participant_id=str(worker["participant_id"]),
        agent_id=str(worker["agent_id"]),
        endpoint_id=endpoint_id,
        runtime_instance_id=runtime_instance_id,
    )


def start_codex(args: argparse.Namespace) -> int:
    if (
        isinstance(args.timeout_seconds, bool)
        or not math.isfinite(args.timeout_seconds)
        or not 0 < args.timeout_seconds <= 60
    ):
        raise CodexWorkerStartError("--timeout-seconds must be between 0 and 60")
    if args.expected_cli_version not in SUPPORTED_CODEX_CLI_VERSIONS:
        raise CodexWorkerStartError("--expected-cli-version is not supported")
    endpoint = _loopback_endpoint(args.endpoint)
    workspace_id, paths = _workspace()
    request = _worker_request(
        paths,
        workspace_id=workspace_id,
        project=args.project,
        worker_id=args.worker_id,
        endpoint_id=args.endpoint_id,
        runtime_instance_id=args.runtime_instance,
    )
    runtime_home = bind_runtime_home(Path(args.codex_home))
    trusted_root = _trusted_worktree(args.project, args.repo_id, args.cwd)
    try:
        from _session_autobridge import _codex_app_server_token

        token = _codex_app_server_token(args.token_file)
        if args.token_file and token is None:
            raise CodexWorkerStartError("--token-file is unreadable or unusable")

        def exact_probe(thread_id: str) -> CodexAppServerExactThreadResult:
            return probe_exact_thread(
                thread_id,
                endpoint_url=endpoint,
                token=token,
                timeout_seconds=args.timeout_seconds,
            )

        provider = _provider(exact_probe)
        core = SessionLifecycleCore(provider)
        created = utc_iso()
        expiry = (
            dt.datetime.fromisoformat(created).astimezone(dt.timezone.utc)
            + dt.timedelta(seconds=START_TTL_SECONDS)
        ).isoformat()
        config = ManagedCodexStartConfig(
            endpoint_id=args.endpoint_id,
            runtime_instance_id=args.runtime_instance,
            runtime_home_id=runtime_home.runtime_home_id,
            runtime_home_realpath=runtime_home.runtime_home_realpath,
            project_id=args.project,
            repo_id=args.repo_id,
            canonical_cwd=trusted_root.cwd,
            provider_revision=provider.provider_revision,
            model=args.model,
            model_provider=args.model_provider,
            approval_policy="never",
            sandbox_request="read-only",
            sandbox_response={"type": "readOnly"},
            ephemeral=False,
            expected_user_agent_prefix=(
                f"{args.user_agent_product}/{args.expected_cli_version}"
            ),
            expected_cli_version=args.expected_cli_version,
        )

        def start_native(start_id: str):
            with _WebSocketJsonRpcTransport(
                endpoint, timeout_seconds=args.timeout_seconds, token=token
            ) as connection:
                return ManagedCodexStartTransport(connection, config=config)(start_id)

        with LedgerStore.open_writer(paths) as store:
            result = core.start_managed(
                store,
                request,
                runtime_home=runtime_home,
                trusted_project_root=trusted_root,
                created_at_utc=created,
                expires_at_utc=expiry,
                correlation_id="corr_" + secrets.token_urlsafe(12),
                start_native=start_native,
            )
    finally:
        if trusted_root.descriptor_chain is not None:
            trusted_root.descriptor_chain.close()

    evidence = result["evidence"]
    binding = result["binding"]
    print(
        json.dumps(
            {
                "worker_id": args.worker_id,
                "start_id": result["start_id"],
                "native_session_id": evidence.native_thread_id,
                "binding_id": binding["binding_id"],
                "generation": binding["generation"],
                "provider_revision": provider.provider_revision,
                "cwd": trusted_root.cwd,
                "model": args.model,
                "turn_started": False,
            },
            separators=(",", ":"),
        )
    )
    return 0
