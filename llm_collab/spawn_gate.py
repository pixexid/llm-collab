"""Preflight and durable assignment record for one BB spawn."""

from __future__ import annotations

import json
import re
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from llm_collab.bb_client import (
    REFUSAL_AMBIGUOUS,
    REFUSAL_ORPHANED_THREAD,
    BbProfile,
    BbRefusal,
    BbResponseReadError,
    BbProjectIdRefused,
    BbThread,
    BbTransport,
    BbTransportResult,
    BbTransportTimeout,
    bb_project_id_from_project,
    subprocess_transport,
)

GIT_MAX_RESPONSE_CHARS = 64 * 1024
GIT_TIMEOUT_SECONDS = 60.0
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
_CONSTRUCTOR_TOKEN = object()
_EXCLUDED_MODELS = frozenset({
    ("pi", "meta/muse-spark-1.2-contributor"),
    ("pi", "zai/glm-5.2"),
})


@dataclass(frozen=True)
class GateRefusal:
    """A clean pre-spawn refusal. Retrying cannot duplicate a thread."""

    reason: str
    detail: str


@dataclass(frozen=True)
class NewWorktree:
    pass


@dataclass(frozen=True)
class Attached:
    environment_id: str


@dataclass(frozen=True)
class SpawnPlan:
    """A spawn whose profile, isolation, and exact current base are proven."""

    project_id: str
    native_project_id: str
    repo_path: Path
    base_sha: str
    environment: NewWorktree | Attached
    profile: BbProfile
    permission_mode: str | None
    title: str | None
    prompt: str
    _constructor_token: InitVar[object | None] = None

    def __post_init__(self, _constructor_token: object | None) -> None:
        if _constructor_token is not _CONSTRUCTOR_TOKEN:
            raise TypeError("SpawnPlan values must be produced by plan_spawn")


class _ReturnedSpawnRefusal(RuntimeError):
    def __init__(self, refusal: BbRefusal) -> None:
        super().__init__(refusal.detail)
        self.refusal = refusal


class _GateFailure(RuntimeError):
    def __init__(self, refusal: GateRefusal) -> None:
        super().__init__(refusal.detail)
        self.refusal = refusal


def _git_read(
    repo_path: Path,
    argv: Sequence[str],
    *,
    transport: BbTransport,
) -> BbTransportResult:
    """Run one bounded Git read, always scoped to the registered repository."""
    command = ["-C", str(repo_path), *argv]
    try:
        return transport(command, GIT_TIMEOUT_SECONDS)
    except (BbResponseReadError, BbTransportTimeout, OSError) as error:
        raise _GateFailure(GateRefusal(
            "git_read_failed", f"git {' '.join(command[:5])} failed: {error}"
        )) from error


def plan_spawn(
    *,
    assignment_kind: str,
    registry_entry: Mapping[str, Any] | None,
    repo_target: str | None,
    base_sha: str,
    environment: NewWorktree | Attached | None,
    provider: str | None,
    model: str | None,
    reasoning_level: str | None,
    permission_mode: str | None,
    title: str | None,
    prompt: str,
    transport: BbTransport | None = None,
) -> SpawnPlan | GateRefusal:
    """Return a validated plan or a clean refusal without calling BB or writing."""
    missing = [
        name
        for name, value in (
            ("provider", provider),
            ("model", model),
            ("reasoning_level", reasoning_level),
        )
        if not isinstance(value, str) or not value
    ]
    if missing:
        return GateRefusal(
            "incomplete_profile",
            f"frozen assignment triple is incomplete; missing {', '.join(missing)}",
        )
    if assignment_kind not in {"read-only", "writing"}:
        return GateRefusal("invalid_assignment_kind", f"unknown assignment kind {assignment_kind!r}")
    if (provider, model) in _EXCLUDED_MODELS:
        return GateRefusal(
            "excluded_model",
            f"{provider} / {model} is excluded from {assignment_kind} assignments",
        )
    if not isinstance(environment, (NewWorktree, Attached)):
        return GateRefusal(
            "isolation_required",
            "assignment isolation requires a new worktree or attached environment",
        )
    if isinstance(environment, Attached) and not environment.environment_id:
        return GateRefusal("isolation_required", "attached environment id is empty")
    if _SHA_RE.fullmatch(base_sha) is None:
        return GateRefusal(
            "invalid_base_sha",
            f"base {base_sha!r} is a branch name or malformed revision; expected 40 hex",
        )

    if not isinstance(registry_entry, Mapping):
        return GateRefusal("registry_project_invalid", "registered project is not an object")
    project_id = registry_entry.get("id")
    if not isinstance(project_id, str) or not project_id:
        return GateRefusal("registry_project_invalid", "registered project has no id")
    base_ref = registry_entry.get("default_branch_base")
    if not isinstance(base_ref, str) or not base_ref.strip():
        return GateRefusal(
            "registry_base_missing", "registered project has no default_branch_base"
        )
    base_ref = base_ref.strip()
    bb = registry_entry.get("bb")
    if not isinstance(bb, Mapping) or bb.get("enabled") is not True:
        return GateRefusal("bb_disabled", "bb adapter is not enabled for this project")

    repos = registry_entry.get("repos")
    keys = sorted(key for key in repos if isinstance(key, str) and key) if isinstance(repos, Mapping) else []
    if not keys:
        return GateRefusal("registry_repo_missing", "registered project has no repositories")
    if repo_target is None and len(keys) != 1:
        return GateRefusal(
            "registry_repo_ambiguous",
            f"repo target is required; valid keys: {', '.join(keys)}",
        )
    selected = repo_target or keys[0]
    if selected not in keys:
        return GateRefusal(
            "registry_repo_missing",
            f"repo target {selected!r} is not a configured path",
        )
    try:
        native_project_id = bb_project_id_from_project(
            registry_entry, project_id, selected
        )
    except BbProjectIdRefused as error:
        if not error.raw_nonempty:
            return GateRefusal(
                "registry_bb_project_invalid", f"{error.field} is invalid"
            )
        return GateRefusal(
            "registry_bb_project_invalid",
            f"{error.field} {error.value!r} has surrounding whitespace; "
            "refusing (match raw, reject padded)",
        )
    repo_path = repos[selected]
    if not isinstance(repo_path, Path) or not repo_path.is_absolute():
        return GateRefusal("registry_repo_missing", f"repo target {selected!r} did not resolve")

    git_transport = transport or subprocess_transport(
        ["git"], max_response_chars=GIT_MAX_RESPONSE_CHARS
    )

    def git(argv: Sequence[str], allowed: tuple[int, ...] = (0,)) -> BbTransportResult:
        result = _git_read(repo_path, argv, transport=git_transport)
        if result.exit_code not in allowed:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_code}"
            raise _GateFailure(GateRefusal(
                "git_read_failed",
                f"git {' '.join(argv[:3])} failed with exit {result.exit_code}: {detail}",
            ))
        return result

    exact_base = base_sha.lower()
    try:
        git(["fetch", "--quiet", "origin", base_ref])
        resolved = git(["rev-parse", "--verify", f"{exact_base}^{{commit}}"])
        if resolved.stdout.strip().lower() != exact_base:
            return GateRefusal("invalid_base_sha", f"base {exact_base} did not resolve exactly")
        origin_ref = f"origin/{base_ref}"
        origin = git(["rev-parse", "--verify", f"{origin_ref}^{{commit}}"])
        origin_base = origin.stdout.strip().lower()
        if _SHA_RE.fullmatch(origin_base) is None:
            return GateRefusal(
                "git_read_failed", f"git returned a malformed {origin_ref} SHA"
            )
        ancestor = git(
            ["merge-base", "--is-ancestor", exact_base, origin_base], allowed=(0, 1)
        )
        if ancestor.exit_code == 1:
            return GateRefusal(
                "base_not_ancestor",
                f"base {exact_base} is not an ancestor of {origin_ref} {origin_base}",
            )
        drift = git(["rev-list", "--count", f"{exact_base}..{origin_base}"])
    except _GateFailure as error:
        return error.refusal
    try:
        behind = int(drift.stdout.strip())
    except ValueError:
        return GateRefusal("git_read_failed", "git rev-list returned a non-integer count")
    if behind:
        return GateRefusal(
            "base_behind_origin",
            f"base {exact_base} is {behind} commit{'s' if behind != 1 else ''} "
            f"behind {origin_ref} {origin_base}",
        )
    return SpawnPlan(
        project_id,
        native_project_id,
        repo_path,
        exact_base,
        environment,
        BbProfile(provider, model, reasoning_level),
        permission_mode,
        title,
        prompt,
        _constructor_token=_CONSTRUCTOR_TOKEN,
    )


def _post_spawn_refusal(error: BaseException, outcome: object) -> BbRefusal:
    """The one surface for every failure after the spawn phase begins."""
    if isinstance(error, _ReturnedSpawnRefusal):
        return error.refusal
    native_id = (
        outcome.thread_id
        if isinstance(outcome, BbThread)
        else outcome.native_thread_id
        if isinstance(outcome, BbRefusal)
        else None
    )
    detail = str(error) or type(error).__name__
    if native_id is not None:
        return BbRefusal(
            REFUSAL_ORPHANED_THREAD,
            f"bb thread {native_id} exists but assignment completion failed: {detail}",
            native_thread_id=native_id,
            task_attempted=True,
        )
    return BbRefusal(
        REFUSAL_AMBIGUOUS,
        f"assignment completion failed: {detail}; a bb thread may exist",
    )


def _classify_spawn_failure(
    error: BaseException, outcome: object
) -> tuple[BbRefusal, bool]:
    """Return the refusal and whether no task call is proven (safe to retry)."""
    refusal = _post_spawn_refusal(error, outcome)
    retryable = (
        isinstance(error, _ReturnedSpawnRefusal)
        and refusal.task_attempted is False
    )
    return refusal, retryable


def persist_assignment(
    plan: SpawnPlan,
    outcome: BbThread | BbRefusal,
    state_dir: Path,
    *,
    write_durably: Callable[[Path, str], None],
    watcher_gate: Mapping[str, Any] | None = None,
) -> Path:
    """Persist an attested assignment, or surface the client's classified refusal.

    `watcher_gate` carries the recorded delegation-gate override (GH-722) when
    one was used; it is written verbatim so the record proves the override.
    """
    if isinstance(outcome, BbRefusal):
        raise _ReturnedSpawnRefusal(outcome)
    thread_id = outcome.thread_id
    if not thread_id or "/" in thread_id or "\0" in thread_id or thread_id in {".", ".."}:
        error = ValueError(f"native thread id {thread_id!r} is not a safe filename segment")
        raise _ReturnedSpawnRefusal(_post_spawn_refusal(error, outcome))

    requested_profile = {
        "provider": plan.profile.provider,
        "model": plan.profile.model,
        "reasoning_level": plan.profile.reasoning_level,
    }
    environment = (
        {"kind": "new_worktree"}
        if isinstance(plan.environment, NewWorktree)
        else {"kind": "attached", "environment_id": plan.environment.environment_id}
    )
    record = {
        "version": 1,
        "recorded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_id": plan.project_id,
        "native_project_id": plan.native_project_id,
        "repo_path": str(plan.repo_path),
        "base_sha": plan.base_sha,
        "environment": environment,
        "permission_mode": plan.permission_mode,
        "title": plan.title,
        "prompt": plan.prompt,
        "thread_id": thread_id,
        "environment_id": outcome.environment_id,
        "requested_profile": requested_profile,
        "executed_profile": dict(requested_profile),
    }
    if watcher_gate is not None:
        record["watcher_gate"] = dict(watcher_gate)
    record_path = state_dir / f"{thread_id}.json"
    write_durably(record_path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record_path
