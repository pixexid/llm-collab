#!/usr/bin/env python3
"""Spawn one BB assignment only after its frozen execution identity is complete."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import re
import subprocess
from dataclasses import dataclass

from _helpers import utc_iso, write_file_durably
from llm_collab.bb_client import (
    REFUSAL_AMBIGUOUS,
    REFUSAL_ORPHANED_THREAD,
    BbClient,
    BbProfile,
    BbRefusal,
    BbThread,
    subprocess_transport,
)

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{3,128}")
REGISTRY_MAX_BYTES = 1_000_000
COMMAND_TIMEOUT_SECONDS = 60
EXCLUDED_WRITING_MODELS = {
    ("pi", "meta/muse-spark-1.2-contributor"),
    ("pi", "zai/glm-5.2"),
}


class Refusal(Exception):
    """A refusal proven before the task-bearing BB call."""


@dataclass(frozen=True)
class ProjectContext:
    repo_root: Path
    record_dir: Path
    repo_target: str


@dataclass(frozen=True)
class SpawnSuccess:
    output: str
    thread_id: str
    record_path: Path


def run(argv: list[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise Refusal(f"{' '.join(argv[:4])} exceeded its {timeout}s deadline") from error
    except OSError as error:
        raise Refusal(f"cannot run {argv[0]}: {error}") from error


def checked_git(
    repo_root: Path, argv: list[str], *, allowed: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess:
    command = ["git", "-C", str(repo_root), *argv]
    result = run(command)
    if result.returncode not in allowed:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise Refusal(f"{' '.join(command[:6])} failed: {detail}")
    return result


def validate_base(repo_root: Path, base_sha: str) -> str:
    if SHA_RE.fullmatch(base_sha) is None:
        raise Refusal(
            f"base {base_sha!r} is a branch name or malformed revision; "
            "--base-sha requires exactly 40 hex characters"
        )
    base_sha = base_sha.lower()
    checked_git(repo_root, ["fetch", "--quiet", "origin", "main"])
    resolved = checked_git(
        repo_root, ["rev-parse", "--verify", f"{base_sha}^{{commit}}"]
    ).stdout.strip().lower()
    if resolved != base_sha:
        raise Refusal(f"base {base_sha} did not resolve to that exact commit")
    origin_main = checked_git(
        repo_root, ["rev-parse", "--verify", "origin/main^{commit}"]
    ).stdout.strip().lower()
    ancestry = checked_git(
        repo_root,
        ["merge-base", "--is-ancestor", base_sha, origin_main],
        allowed=(0, 1),
    )
    if ancestry.returncode == 1:
        raise Refusal(f"base {base_sha} is not an ancestor of origin/main {origin_main}")
    raw_drift = checked_git(
        repo_root, ["rev-list", "--count", f"{base_sha}..{origin_main}"]
    ).stdout.strip()
    try:
        drift = int(raw_drift)
    except ValueError as error:
        raise Refusal(f"git rev-list returned a non-integer drift count: {raw_drift!r}") from error
    if drift:
        raise Refusal(
            f"base {base_sha} is {drift} commit{'s' if drift != 1 else ''} "
            f"behind origin/main {origin_main}"
        )
    return base_sha


def coordination_root() -> Path:
    marker = SCRIPT_ROOT / ".git"
    if marker.is_dir():
        return SCRIPT_ROOT
    try:
        with marker.open("rb") as handle:
            raw_bytes = handle.read(4097)
        raw = raw_bytes.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise Refusal(f"cannot resolve the coordination root from {marker}: {error}") from error
    prefix = "gitdir: "
    if not raw.startswith(prefix) or len(raw_bytes) > 4096:
        raise Refusal(f"cannot resolve the coordination root from malformed {marker}")
    git_dir = Path(raw[len(prefix):].strip())
    if not git_dir.is_absolute():
        git_dir = marker.parent / git_dir
    for candidate in (git_dir, *git_dir.parents):
        if candidate.name == ".git":
            return candidate.parent.resolve()
    raise Refusal(f"cannot find the common checkout above {git_dir}")


def read_json(path: Path, budget: list[int]) -> object:
    try:
        with path.open("rb") as handle:
            raw = handle.read(budget[0] + 1)
        if len(raw) > budget[0]:
            raise Refusal(
                f"registry reads exceed the cumulative {REGISTRY_MAX_BYTES}-byte bound"
            )
        budget[0] -= len(raw)
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, RecursionError, ValueError) as error:
        raise Refusal(f"cannot read {path}: {error}") from error


def configured_path(root: Path, value: object, default: Path) -> Path:
    if not value:
        return default.resolve()
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def project_context(project_id: str, repo_target: str | None) -> ProjectContext:
    root = coordination_root()
    budget = [REGISTRY_MAX_BYTES]
    projects = read_json(root / "projects.json", budget)
    config = read_json(root / "collab.config.json", budget)
    entries = projects.get("projects") if isinstance(projects, dict) else None
    project = next(
        (
            entry
            for entry in entries or []
            if isinstance(entry, dict) and entry.get("id") == project_id
        ),
        None,
    )
    if project is None:
        raise Refusal(f"unknown registered collab project {project_id!r}")
    repos = project.get("repos")
    keys = sorted(key for key in repos if isinstance(key, str) and key) \
        if isinstance(repos, dict) else []
    if not keys:
        raise Refusal(f"project {project_id!r} has no configured repository keys")
    if repo_target is None and len(keys) > 1:
        raise Refusal(
            f"--repo-target is required for project {project_id!r}; "
            f"valid keys: {', '.join(keys)}"
        )
    selected = repo_target or keys[0]
    if selected not in keys:
        raise Refusal(
            f"--repo-target {selected!r} is not configured for project {project_id!r}; "
            f"valid keys: {', '.join(keys)}"
        )
    raw_repo = repos[selected]
    if not isinstance(raw_repo, str) or not raw_repo:
        raise Refusal(f"project {project_id!r} repo target {selected!r} has no path")
    projects_root = configured_path(
        root,
        config.get("projects_root") if isinstance(config, dict) else None,
        root,
    )
    repo_path = Path(raw_repo).expanduser()
    repo_root = (
        repo_path.resolve()
        if repo_path.is_absolute()
        else (projects_root / repo_path).resolve()
    )
    state_root = configured_path(
        root,
        config.get("project_state_root") if isinstance(config, dict) else None,
        root / "projects",
    )
    return ProjectContext(
        repo_root=repo_root,
        record_dir=state_root / project_id / "bb-assignments",
        repo_target=selected,
    )


def assignment_record(
    *,
    args: argparse.Namespace,
    context: ProjectContext,
    base_sha: str,
    thread_id: str,
    environment_id: str | None,
    attestation: BbRefusal | None,
) -> dict:
    requested_profile = {
        "provider": args.provider,
        "model": args.model,
        "reasoning_level": args.reasoning_level,
    }
    record = {
        "version": 1,
        "recorded_utc": utc_iso(),
        "assignment_kind": args.assignment_kind,
        "collab_project_id": args.collab_project,
        "repo_target": context.repo_target,
        "repo_root": str(context.repo_root),
        "bb_project_id": args.project,
        "requested_profile": requested_profile,
        "base_sha": base_sha,
        "environment_id": environment_id,
        "thread_id": thread_id,
    }
    if attestation is not None:
        record["executed_profile"] = None
        record["profile_attestation"] = {
            "status": "unattested",
            "reason": attestation.reason,
            "detail": attestation.detail,
        }
        return record
    record.update(requested_profile)
    record["executed_profile"] = requested_profile
    record["profile_attestation"] = {
        "status": "attested",
        "source": "client/turn/requested execution",
    }
    return record


def persist_record(record_dir: Path, record: dict) -> Path:
    record_path = record_dir / f"{record['thread_id']}.json"
    write_file_durably(record_path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record_path


def post_persistence_refusal(
    thread_id: str,
    record_path: Path,
    *,
    cause: BbRefusal | None = None,
    detail: str | None = None,
) -> BbRefusal:
    if cause is not None:
        return BbRefusal(
            cause.reason,
            f"bb thread {thread_id} exists but its executed profile is unattested; "
            f"recorded at {record_path}: {cause.detail}",
            native_thread_id=thread_id,
        )
    return BbRefusal(
        REFUSAL_ORPHANED_THREAD,
        f"bb thread {thread_id} and its assignment record at {record_path} exist, "
        f"but {detail or 'a later operation failed'}",
        native_thread_id=thread_id,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--assignment-kind", choices=("read-only", "writing"), required=True)
    result.add_argument("--collab-project", required=True)
    result.add_argument("--repo-target")
    result.add_argument("--project", required=True, help="BB project ID")
    result.add_argument("--provider")
    result.add_argument("--model")
    result.add_argument("--reasoning-level")
    result.add_argument("--base-sha", required=True)
    result.add_argument("--permission-mode")
    result.add_argument("--title")
    result.add_argument("--prompt", required=True)
    result.add_argument("--json", action="store_true")
    isolation = result.add_mutually_exclusive_group()
    isolation.add_argument("--new-environment", choices=("worktree",))
    isolation.add_argument("--environment")
    return result


def main(argv: list[str] | None = None) -> SpawnSuccess | BbRefusal:
    args = parser().parse_args(argv)
    missing = [
        flag
        for flag, value in (
            ("--provider", args.provider),
            ("--model", args.model),
            ("--reasoning-level", args.reasoning_level),
        )
        if not value
    ]
    if missing:
        raise Refusal("frozen assignment triple is incomplete; missing " + ", ".join(missing))
    if args.assignment_kind == "writing" and (
        args.provider, args.model
    ) in EXCLUDED_WRITING_MODELS:
        raise Refusal(f"{args.provider} / {args.model} is excluded from writing assignments")
    if not args.new_environment and not args.environment:
        raise Refusal(
            "assignment isolation is required; pass --new-environment worktree "
            "or an explicit --environment"
        )
    if SAFE_ID_RE.fullmatch(args.collab_project) is None:
        raise Refusal("--collab-project must be one safe project-id segment")

    context = project_context(args.collab_project, args.repo_target)
    base_sha = validate_base(context.repo_root, args.base_sha)
    profile = BbProfile(args.provider, args.model, args.reasoning_level)
    client = BbClient(
        subprocess_transport(["bb"]),
        enabled=True,
        timeout_seconds=COMMAND_TIMEOUT_SECONDS,
    )
    outcome = client.spawn(
        project_id=args.project,
        prompt=args.prompt,
        profile=profile,
        environment_id=args.environment,
        new_worktree_base_sha=base_sha if args.new_environment else None,
        permission_mode=args.permission_mode,
        title=args.title,
    )
    if isinstance(outcome, BbRefusal):
        if outcome.native_thread_id is None:
            return outcome
        record = assignment_record(
            args=args,
            context=context,
            base_sha=base_sha,
            thread_id=outcome.native_thread_id,
            environment_id=None,
            attestation=outcome,
        )
        try:
            record_path = persist_record(context.record_dir, record)
        except OSError as error:
            return BbRefusal(
                REFUSAL_ORPHANED_THREAD,
                f"bb thread {outcome.native_thread_id} exists but its assignment "
                f"record could not be persisted under {context.record_dir}: {error}",
                native_thread_id=outcome.native_thread_id,
            )
        return post_persistence_refusal(
            outcome.native_thread_id, record_path, cause=outcome
        )

    record = assignment_record(
        args=args,
        context=context,
        base_sha=base_sha,
        thread_id=outcome.thread_id,
        environment_id=outcome.environment_id,
        attestation=None,
    )
    record_path = context.record_dir / f"{outcome.thread_id}.json"
    payload = {
        "id": outcome.thread_id,
        "environmentId": outcome.environment_id,
        "projectId": outcome.project_id,
        "providerId": outcome.provider_id,
        "status": outcome.status,
        "assignmentRecord": {**record, "path": str(record_path)},
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        persist_record(context.record_dir, record)
    except OSError as error:
        return BbRefusal(
            REFUSAL_ORPHANED_THREAD,
            f"bb thread {outcome.thread_id} exists but its assignment record could "
            f"not be persisted under {context.record_dir}: {error}",
            native_thread_id=outcome.thread_id,
        )
    return SpawnSuccess(rendered, outcome.thread_id, record_path)


def emit_refusal(refusal: BbRefusal) -> int:
    retry_suppressed = (
        refusal.native_thread_id is not None or refusal.reason == REFUSAL_AMBIGUOUS
    )
    identity = (
        f" native_thread_id={refusal.native_thread_id}"
        if refusal.native_thread_id is not None
        else ""
    )
    prefix = "DO NOT RETRY" if retry_suppressed else "REFUSED"
    try:
        sys.stderr.write(f"{prefix}: {refusal.reason}{identity}: {refusal.detail}\n")
        sys.stderr.flush()
    except (BrokenPipeError, OSError):
        pass
    return 3 if retry_suppressed else 2


def cli() -> int:
    try:
        outcome = main()
    except Refusal as error:
        try:
            sys.stderr.write(f"REFUSED: {error}\n")
            sys.stderr.flush()
        except (BrokenPipeError, OSError):
            pass
        return 2
    if isinstance(outcome, BbRefusal):
        return emit_refusal(outcome)
    try:
        sys.stdout.write(outcome.output)
        sys.stdout.flush()
    except (BrokenPipeError, OSError) as error:
        return emit_refusal(
            post_persistence_refusal(
                outcome.thread_id,
                outcome.record_path,
                detail=f"the success response could not be written: {error}",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
