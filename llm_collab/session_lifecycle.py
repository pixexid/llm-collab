"""Deterministic Phase 3.5 lifecycle challenge core."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
from typing import Callable, Mapping

from llm_collab.codex_runtime_home import RuntimeHomeIdentity
from llm_collab.codex_session_ref import (
    RepositoryBinding,
    SessionAuthority,
    build_session_ref,
)
from llm_collab.ledger import LedgerStore
from llm_collab.ledger.store import CanonicalConflictError


class SessionLifecycleError(ValueError):
    """Raised when lifecycle attestation or state transition fails closed."""


@dataclass(frozen=True)
class TrustedProjectRoot:
    project_id: str
    repo_id: str
    repo_root: str
    cwd: str

    def repository_binding(self) -> RepositoryBinding:
        return RepositoryBinding(
            project_id=self.project_id,
            repo_id=self.repo_id,
            repo_root=self.repo_root,
            cwd=self.cwd,
        )


@dataclass(frozen=True)
class LifecycleSubject:
    workspace_id: str
    scope_kind: str
    scope_identity: str
    conversation_id: str
    participant_id: str
    agent_id: str
    endpoint_id: str
    native_session_id: str
    runtime_instance_id: str

    def scope(self) -> dict[str, str]:
        if self.scope_kind == "project":
            return {"kind": "project", "project_id": self.scope_identity}
        if self.scope_kind == "workspace":
            return {"kind": "workspace"}
        raise SessionLifecycleError("scope_kind must be project or workspace")


@dataclass(frozen=True)
class LifecycleChallenge:
    challenge_id: str
    challenge_token: str
    expires_at_utc: str


@dataclass(frozen=True)
class FakeLifecycleProvider:
    provider_id: str = "provider_codex"
    provider_revision: str = "revision_1"
    authority_identity: str = "fake_lifecycle_provider"
    capability_profile_id: str = "native_session_binding"
    capability_profile_revision: str = "revision_1"

    def authority(self) -> SessionAuthority:
        return SessionAuthority(
            authority_kind="native_runtime",
            identity=self.authority_identity,
            implementation_revision=self.provider_revision,
            capability_profile_id=self.capability_profile_id,
            capability_profile_revision=self.capability_profile_revision,
        )

    def attest(
        self,
        subject: LifecycleSubject,
        *,
        runtime_home: RuntimeHomeIdentity,
        observed_at_utc: str,
        correlation_id: str,
        trusted_project_root: TrustedProjectRoot | None = None,
    ) -> Mapping[str, object]:
        repository = _repository_binding(subject, trusted_project_root)
        return build_session_ref(
            workspace_id=subject.workspace_id,
            scope=subject.scope(),
            endpoint_id=subject.endpoint_id,
            native_session_id=subject.native_session_id,
            runtime_home=runtime_home,
            authority=self.authority(),
            observed_at_utc=observed_at_utc,
            correlation_id=correlation_id,
            repository_binding=repository,
        )

    def open_ui(self, subject: LifecycleSubject) -> dict[str, object]:
        return {
            "presentation_only": True,
            "workspace_id": subject.workspace_id,
            "scope_kind": subject.scope_kind,
            "scope_identity": subject.scope_identity,
            "conversation_id": subject.conversation_id,
            "participant_id": subject.participant_id,
        }


class SessionLifecycleCore:
    def __init__(
        self,
        provider: FakeLifecycleProvider,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.provider = provider
        self._token_factory = token_factory or _secret_token

    def reserve(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        *,
        runtime_home: RuntimeHomeIdentity,
        created_at_utc: str,
        expires_at_utc: str,
        correlation_id: str,
        trusted_project_root: TrustedProjectRoot | None = None,
    ) -> LifecycleChallenge:
        session_ref = self.provider.attest(
            subject,
            runtime_home=runtime_home,
            observed_at_utc=created_at_utc,
            correlation_id=correlation_id,
            trusted_project_root=trusted_project_root,
        )
        token = self._token_factory()
        if not isinstance(token, str) or not token:
            raise SessionLifecycleError("challenge token factory must return non-empty text")
        challenge_id = "challenge_" + _sha256_text(
            "|".join(
                (
                    subject.workspace_id,
                    subject.scope_kind,
                    subject.scope_identity,
                    subject.conversation_id,
                    subject.participant_id,
                    self.provider.provider_id,
                    token,
                )
            )
        )[:32]
        store.reserve_session_binding_challenge(
            workspace_id=subject.workspace_id,
            scope_kind=subject.scope_kind,
            scope_identity=subject.scope_identity,
            conversation_id=subject.conversation_id,
            participant_id=subject.participant_id,
            agent_id=subject.agent_id,
            provider_id=self.provider.provider_id,
            provider_revision=self.provider.provider_revision,
            endpoint_id=subject.endpoint_id,
            session_ref_id=str(session_ref["session_ref_id"]),
            native_session_id=subject.native_session_id,
            runtime_instance_id=subject.runtime_instance_id,
            challenge_id=challenge_id,
            challenge_token_sha256=_sha256_text(token),
            expires_at_utc=expires_at_utc,
            created_at_utc=created_at_utc,
        )
        return LifecycleChallenge(challenge_id, token, expires_at_utc)

    def consume(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        challenge: LifecycleChallenge,
        *,
        runtime_home: RuntimeHomeIdentity,
        consumed_at_utc: str,
        correlation_id: str,
        trusted_project_root: TrustedProjectRoot | None = None,
    ) -> dict[str, object]:
        session_ref = self.provider.attest(
            subject,
            runtime_home=runtime_home,
            observed_at_utc=consumed_at_utc,
            correlation_id=correlation_id,
            trusted_project_root=trusted_project_root,
        )
        return store.consume_session_binding_challenge(
            workspace_id=subject.workspace_id,
            scope_kind=subject.scope_kind,
            scope_identity=subject.scope_identity,
            conversation_id=subject.conversation_id,
            participant_id=subject.participant_id,
            challenge_id=challenge.challenge_id,
            challenge_token_sha256=_sha256_text(challenge.challenge_token),
            provider_id=self.provider.provider_id,
            provider_revision=self.provider.provider_revision,
            endpoint_id=subject.endpoint_id,
            session_ref_id=str(session_ref["session_ref_id"]),
            native_session_id=subject.native_session_id,
            runtime_instance_id=subject.runtime_instance_id,
            consumed_at_utc=consumed_at_utc,
        )

    def inspect(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        *,
        expected_binding_id: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, object]:
        return store.resolve_conversation_binding(
            workspace_id=subject.workspace_id,
            scope_kind=subject.scope_kind,
            scope_identity=subject.scope_identity,
            conversation_id=subject.conversation_id,
            participant_id=subject.participant_id,
            expected_binding_id=expected_binding_id,
            expected_generation=expected_generation,
        )

    def heartbeat(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        binding: Mapping[str, object],
        *,
        runtime_home: RuntimeHomeIdentity,
        observed_at_utc: str,
        correlation_id: str,
        trusted_project_root: TrustedProjectRoot | None = None,
    ) -> dict[str, object]:
        self.provider.attest(
            subject,
            runtime_home=runtime_home,
            observed_at_utc=observed_at_utc,
            correlation_id=correlation_id,
            trusted_project_root=trusted_project_root,
        )
        return self.inspect(
            store,
            subject,
            expected_binding_id=str(binding["binding_id"]),
            expected_generation=_positive_int(binding["generation"], "generation"),
        )

    def retire(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        binding: Mapping[str, object],
    ) -> dict[str, object]:
        store.update_conversation_binding_state(
            workspace_id=subject.workspace_id,
            scope_kind=subject.scope_kind,
            scope_identity=subject.scope_identity,
            conversation_id=subject.conversation_id,
            participant_id=subject.participant_id,
            binding_id=str(binding["binding_id"]),
            generation=_positive_int(binding["generation"], "generation"),
            state="retired",
        )
        return self.inspect(store, subject)

    def mark_restart_unverified(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        binding: Mapping[str, object],
        *,
        runtime_home: RuntimeHomeIdentity,
        observed_at_utc: str,
        correlation_id: str,
        trusted_project_root: TrustedProjectRoot | None = None,
    ) -> dict[str, object]:
        self.provider.attest(
            subject,
            runtime_home=runtime_home,
            observed_at_utc=observed_at_utc,
            correlation_id=correlation_id,
            trusted_project_root=trusted_project_root,
        )
        store.update_conversation_binding_state(
            workspace_id=subject.workspace_id,
            scope_kind=subject.scope_kind,
            scope_identity=subject.scope_identity,
            conversation_id=subject.conversation_id,
            participant_id=subject.participant_id,
            binding_id=str(binding["binding_id"]),
            generation=_positive_int(binding["generation"], "generation"),
            state="unverified",
        )
        return self.inspect(store, subject)

    def rebind(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        predecessor_binding: Mapping[str, object],
        successor_binding: Mapping[str, object],
        *,
        transition_kind: str,
        actor_id: str,
        reason: str,
        evidence: bytes,
        created_at_utc: str,
    ) -> dict[str, object]:
        return store.record_conversation_binding_transition(
            workspace_id=subject.workspace_id,
            scope_kind=subject.scope_kind,
            scope_identity=subject.scope_identity,
            conversation_id=subject.conversation_id,
            participant_id=subject.participant_id,
            predecessor_binding_id=str(predecessor_binding["binding_id"]),
            predecessor_generation=_positive_int(
                predecessor_binding["generation"], "predecessor_generation"
            ),
            successor_binding_id=str(successor_binding["binding_id"]),
            successor_generation=_positive_int(
                successor_binding["generation"], "successor_generation"
            ),
            transition_kind=transition_kind,
            actor_id=actor_id,
            reason=reason,
            evidence=evidence,
            created_at_utc=created_at_utc,
        )


def _repository_binding(
    subject: LifecycleSubject, trusted_project_root: TrustedProjectRoot | None
) -> RepositoryBinding | None:
    if subject.scope_kind == "workspace":
        return None
    if trusted_project_root is None:
        raise SessionLifecycleError("trusted project root is required for project attestation")
    if trusted_project_root.project_id != subject.scope_identity:
        raise SessionLifecycleError("trusted project root does not match subject scope")
    return trusted_project_root.repository_binding()

def _secret_token() -> str:
    return secrets.token_urlsafe(32)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SessionLifecycleError(f"{name} must be a positive integer")
    return value
