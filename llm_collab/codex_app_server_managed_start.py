"""Injected Codex App Server managed-start protocol adapter.

This module is deliberately transport-injected. It does not discover or open an
endpoint, read credentials, launch a process, or enable a lifecycle provider.
It performs one start and one exact read on one already-owned connection, then
returns child-1 evidence-shaped data to the child-2 saga.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from llm_collab.codex_app_server_live_probe import (
    THREAD_READ_METHOD,
    _minimal_initialize,
    _request,
    _required_mapping,
    _required_string,
    CodexAppServerLiveProbeError,
)


THREAD_START_METHOD = "thread/start"
_THREAD_START_ALLOWED_METHODS = frozenset((THREAD_START_METHOD, THREAD_READ_METHOD))
_THREAD_START_REQUIRED_FIELDS = frozenset(
    (
        "approvalPolicy",
        "approvalsReviewer",
        "cwd",
        "model",
        "modelProvider",
        "sandbox",
        "thread",
    )
)
_THREAD_START_OPTIONAL_FIELDS = frozenset(
    (
        "activePermissionProfile",
        "instructionSources",
        "multiAgentMode",
        "reasoningEffort",
        "runtimeWorkspaceRoots",
        "serviceTier",
    )
)
_THREAD_REQUIRED_FIELDS = frozenset(
    (
        "cliVersion",
        "createdAt",
        "cwd",
        "ephemeral",
        "id",
        "modelProvider",
        "preview",
        "sessionId",
        "source",
        "status",
        "turns",
        "updatedAt",
    )
)


class ManagedCodexStartError(CodexAppServerLiveProbeError):
    """Raised when the injected managed-start exchange fails closed."""


@dataclass(frozen=True)
class ManagedCodexStartConfig:
    """Trusted, pre-resolved facts used to build one start request.

    This is not a request DTO. There is intentionally no constructor from
    caller/chat/peer input, and the real endpoint/credential/process seam is
    deferred to held slice 3b.
    """

    endpoint_id: str
    runtime_instance_id: str
    runtime_home_id: str
    runtime_home_realpath: str
    project_id: str
    repo_id: str
    canonical_cwd: str
    provider_revision: str
    model: str
    model_provider: str
    approval_policy: str | Mapping[str, object]
    sandbox_request: str
    sandbox_response: Mapping[str, object]
    approvals_reviewer: str = "user"

    def start_params(self) -> dict[str, object]:
        return {
            "approvalPolicy": self.approval_policy,
            "cwd": self.canonical_cwd,
            "ephemeral": True,
            "model": self.model,
            "sandbox": self.sandbox_request,
        }


class ManagedCodexStartTransport:
    """Run the fake/protocol-only managed start on one injected connection."""

    def __init__(self, connection: Any, *, config: ManagedCodexStartConfig) -> None:
        if not hasattr(connection, "exchange") or not hasattr(connection, "notify"):
            raise TypeError("managed-start connection must expose exchange and notify")
        self.connection = connection
        self.config = config
        self._used = False

    def __call__(self, _start_id: str) -> Mapping[str, object]:
        if self._used:
            raise ManagedCodexStartError("managed-start connection is single-use")
        self._used = True
        try:
            _minimal_initialize(self.connection)
            start_result = _request(
                self.connection,
                2,
                THREAD_START_METHOD,
                self.config.start_params(),
                allowed_methods=_THREAD_START_ALLOWED_METHODS,
            )
            thread = self._validate_start_result(start_result)
            native_thread_id = _required_string(thread, "id")
            read_result = _request(
                self.connection,
                3,
                THREAD_READ_METHOD,
                {"threadId": native_thread_id, "includeTurns": False},
                require_jsonrpc=False,
                allowed_methods=_THREAD_START_ALLOWED_METHODS,
            )
            read_thread = self._validate_read_result(read_result, native_thread_id)
            return self._candidate(start_result, thread, read_thread, native_thread_id)
        except ManagedCodexStartError:
            raise
        except CodexAppServerLiveProbeError as error:
            raise ManagedCodexStartError(str(error)) from error
        except Exception as error:
            raise ManagedCodexStartError("managed-start exchange failed") from error

    def _validate_start_result(self, result: Mapping[str, Any]) -> Mapping[str, Any]:
        fields = set(result)
        if fields - (_THREAD_START_REQUIRED_FIELDS | _THREAD_START_OPTIONAL_FIELDS):
            raise ManagedCodexStartError("thread/start result has unexpected fields")
        if not _THREAD_START_REQUIRED_FIELDS <= fields:
            raise ManagedCodexStartError("thread/start result is missing required fields")
        if "threads" in result:
            raise ManagedCodexStartError("thread/start result contains ambiguous threads")
        if result["approvalPolicy"] != self.config.approval_policy:
            raise ManagedCodexStartError("thread/start approval policy mismatch")
        if result["cwd"] != self.config.canonical_cwd:
            raise ManagedCodexStartError("thread/start cwd mismatch")
        if result["model"] != self.config.model:
            raise ManagedCodexStartError("thread/start model mismatch")
        if result["modelProvider"] != self.config.model_provider:
            raise ManagedCodexStartError("thread/start model provider mismatch")
        if result["sandbox"] != dict(self.config.sandbox_response):
            raise ManagedCodexStartError("thread/start sandbox mismatch")
        if result["approvalsReviewer"] != self.config.approvals_reviewer:
            raise ManagedCodexStartError("thread/start approvals reviewer mismatch")
        if not isinstance(result["sandbox"], Mapping):
            raise ManagedCodexStartError("thread/start sandbox is invalid")
        return _required_mapping(result, "thread")

    def _validate_read_result(
        self, result: Mapping[str, Any], expected_thread_id: str
    ) -> Mapping[str, Any]:
        if set(result) != {"thread"}:
            raise ManagedCodexStartError("thread/read result has unexpected fields")
        thread = _required_mapping(result, "thread")
        if set(thread) < _THREAD_REQUIRED_FIELDS:
            raise ManagedCodexStartError("thread/read thread is missing required fields")
        if _required_string(thread, "id") != expected_thread_id:
            raise ManagedCodexStartError("thread/read returned a different thread id")
        if thread.get("cwd") != self.config.canonical_cwd:
            raise ManagedCodexStartError("thread/read cwd mismatch")
        if thread.get("modelProvider") != self.config.model_provider:
            raise ManagedCodexStartError("thread/read model provider mismatch")
        return thread

    def _candidate(
        self,
        start_result: Mapping[str, Any],
        start_thread: Mapping[str, Any],
        read_thread: Mapping[str, Any],
        native_thread_id: str,
    ) -> Mapping[str, object]:
        config = self.config
        return {
            "native_thread_id": native_thread_id,
            "endpoint_id": config.endpoint_id,
            "runtime_instance_id": config.runtime_instance_id,
            "runtime_home_id": config.runtime_home_id,
            "runtime_home_realpath": config.runtime_home_realpath,
            "project_id": config.project_id,
            "repo_id": config.repo_id,
            "canonical_cwd": config.canonical_cwd,
            "provider_revision": config.provider_revision,
            "creation_provenance": {
                "source": "managed_thread_start",
                "native_thread_id": _required_string(start_thread, "id"),
                "approval_policy": start_result["approvalPolicy"],
                "sandbox": dict(_required_mapping(start_result, "sandbox")),
                "model": _required_string(start_result, "model"),
                "cwd": _required_string(start_result, "cwd"),
            },
            "read_back": {
                "operation": "thread_read",
                "native_thread_id": _required_string(read_thread, "id"),
                "endpoint_id": config.endpoint_id,
                "runtime_instance_id": config.runtime_instance_id,
                "runtime_home_id": config.runtime_home_id,
                "runtime_home_realpath": config.runtime_home_realpath,
                "project_id": config.project_id,
                "repo_id": config.repo_id,
                "canonical_cwd": config.canonical_cwd,
                "provider_revision": config.provider_revision,
            },
        }
