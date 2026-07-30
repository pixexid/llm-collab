"""Closed validator for injected Claude SessionStart and channel evidence.

This is a contract test seam, not real Claude authentication. It accepts only
an injected, verifier-origin-bound source and remains inert until real
SessionStart, channel, and CLAUDE_HOME identity integrations are separately
gated.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from llm_collab.codex_runtime_home import RuntimeHomeIdentity


_HOOK_FIELDS = frozenset(
    (
        "native_session_id",
        "cwd",
        "transcript_path",
        "transcript_sha256",
        "source",
        "proven_at_utc",
        "provider_revision",
    )
)
_CHANNEL_FIELDS = frozenset(("endpoint_id", "runtime_instance_id"))
_HOOK_SOURCES = frozenset(("startup", "resume"))


class ClaudeAttachEvidenceError(ValueError):
    """Raised when injected Claude evidence does not match trusted inputs."""


@dataclass(frozen=True)
class ClaudeAttachEvidenceResult:
    native_session_id: str
    validated_cwd: str
    transcript_path: str
    transcript_sha256: str
    hook_source: str
    channel_endpoint_id: str
    channel_runtime_instance_id: str
    proven_at_utc: str
    provider_revision: str


class ClaudeAttachEvidenceValidator:
    """Validate one injected hook/channel evidence pair exactly once."""

    def __init__(
        self,
        source: Callable[[], object],
        *,
        verifier_origin: object,
        provider_revision: str,
    ) -> None:
        if not callable(source):
            raise TypeError("Claude attach evidence source must be callable")
        self._source = source
        self._verifier_origin = verifier_origin
        self._provider_revision = provider_revision
        self._used = False

    def __call__(
        self,
        subject: Any,
        trusted_project_root: Any,
        runtime_home: RuntimeHomeIdentity,
    ) -> ClaudeAttachEvidenceResult:
        if self._used:
            raise ClaudeAttachEvidenceError(
                "Claude attach evidence source is single-use"
            )
        self._used = True
        candidate = self._source()
        if (
            not isinstance(candidate, tuple)
            or len(candidate) != 3
            or candidate[0] is not self._verifier_origin
        ):
            raise ClaudeAttachEvidenceError(
                "Claude attach evidence lacks verifier origin"
            )
        hook = _closed_mapping(candidate[1], _HOOK_FIELDS, "SessionStart")
        channel = _closed_mapping(candidate[2], _CHANNEL_FIELDS, "Claude channel")

        expected_cwd = _trusted_cwd(trusted_project_root)
        native_session_id = _text(hook["native_session_id"], "native_session_id")
        validated_cwd = _realpath(hook["cwd"], "cwd")
        if native_session_id != subject.native_session_id:
            raise ClaudeAttachEvidenceError("Claude native session id mismatch")
        if validated_cwd != expected_cwd:
            raise ClaudeAttachEvidenceError("Claude cwd mismatch")

        transcript_path = _transcript_path(
            hook["transcript_path"], runtime_home
        )
        transcript_sha256 = _text(
            hook["transcript_sha256"], "transcript_sha256"
        )
        if transcript_sha256 != _sha256_file(transcript_path):
            raise ClaudeAttachEvidenceError("Claude transcript digest mismatch")

        hook_source = _text(hook["source"], "source")
        if hook_source not in _HOOK_SOURCES:
            raise ClaudeAttachEvidenceError("Claude hook source is invalid")
        channel_endpoint_id = _text(channel["endpoint_id"], "endpoint_id")
        channel_runtime_instance_id = _text(
            channel["runtime_instance_id"], "runtime_instance_id"
        )
        if channel_endpoint_id != subject.endpoint_id:
            raise ClaudeAttachEvidenceError("Claude channel endpoint mismatch")
        if channel_runtime_instance_id != subject.runtime_instance_id:
            raise ClaudeAttachEvidenceError(
                "Claude channel runtime instance mismatch"
            )

        proven_at_utc = _text(hook["proven_at_utc"], "proven_at_utc")
        provider_revision = _text(
            hook["provider_revision"], "provider_revision"
        )
        if provider_revision != self._provider_revision:
            raise ClaudeAttachEvidenceError("Claude provider revision mismatch")
        return ClaudeAttachEvidenceResult(
            native_session_id=native_session_id,
            validated_cwd=validated_cwd,
            transcript_path=transcript_path,
            transcript_sha256=transcript_sha256,
            hook_source=hook_source,
            channel_endpoint_id=channel_endpoint_id,
            channel_runtime_instance_id=channel_runtime_instance_id,
            proven_at_utc=proven_at_utc,
            provider_revision=provider_revision,
        )


def _closed_mapping(
    value: object, fields: frozenset[str], label: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ClaudeAttachEvidenceError(
            f"{label} evidence has an unexpected shape"
        )
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ClaudeAttachEvidenceError(
            f"Claude attach evidence {field} is invalid"
        )
    return value


def _realpath(value: object, field: str) -> str:
    raw = _text(value, field)
    if not os.path.isabs(raw):
        raise ClaudeAttachEvidenceError(
            f"Claude attach evidence {field} must be absolute"
        )
    return os.path.realpath(raw)


def _trusted_cwd(trusted_project_root: Any) -> str:
    try:
        repo_root = os.path.realpath(trusted_project_root.repo_root)
        cwd = os.path.realpath(trusted_project_root.cwd)
        if not os.path.isdir(repo_root) or not os.path.isdir(cwd):
            raise ClaudeAttachEvidenceError(
                "trusted project paths must be directories"
            )
        if os.path.commonpath((repo_root, cwd)) != repo_root:
            raise ClaudeAttachEvidenceError(
                "trusted cwd must be under repository root"
            )
        return cwd
    except (AttributeError, TypeError, ValueError) as error:
        raise ClaudeAttachEvidenceError(
            "trusted project paths are invalid"
        ) from error


def _transcript_path(
    value: object, runtime_home: RuntimeHomeIdentity
) -> str:
    if not isinstance(runtime_home, RuntimeHomeIdentity):
        raise ClaudeAttachEvidenceError(
            "runtime_home must be a RuntimeHomeIdentity"
        )
    transcript = _realpath(value, "transcript_path")
    projects = os.path.join(
        os.path.realpath(runtime_home.runtime_home_realpath), "projects"
    )
    try:
        relative = Path(transcript).relative_to(projects)
    except ValueError as error:
        raise ClaudeAttachEvidenceError(
            "Claude transcript must be under CLAUDE_HOME/projects"
        ) from error
    if len(relative.parts) < 2 or not os.path.isfile(transcript):
        raise ClaudeAttachEvidenceError(
            "Claude transcript must be under one project slug"
        )
    return transcript


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
