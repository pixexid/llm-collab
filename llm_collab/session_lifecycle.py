"""Deterministic Phase 3.5 lifecycle challenge core."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import secrets
from typing import Callable, Mapping, TYPE_CHECKING

from llm_collab.codex_runtime_home import RuntimeHomeIdentity
from llm_collab.codex_app_server_live_probe import CodexAppServerExactThreadResult
from llm_collab.codex_session_ref import (
    RepositoryBinding,
    RepositoryDescriptorChain,
    SessionAuthority,
    build_session_ref,
    derive_session_owner_key,
)
from llm_collab.ledger import LedgerStore
from llm_collab.ledger.store import CanonicalConflictError

if TYPE_CHECKING:
    from llm_collab.claude_attach_evidence import ClaudeAttachEvidenceResult


class SessionLifecycleError(ValueError):
    """Raised when lifecycle attestation or state transition fails closed."""


DEFAULT_SUPPORTED_OPERATIONS_JSON = (
    '["reserve","start","attach","inspect","heartbeat","retire","open_ui"]'
)


@dataclass(frozen=True)
class TrustedProjectRoot:
    project_id: str
    repo_id: str
    repo_root: str
    cwd: str
    descriptor_chain: RepositoryDescriptorChain | None = None

    def repository_binding(self) -> RepositoryBinding:
        return RepositoryBinding(
            project_id=self.project_id,
            repo_id=self.repo_id,
            repo_root=self.repo_root,
            cwd=self.cwd,
            descriptor_chain=self.descriptor_chain,
        )


@dataclass(frozen=True)
class CodexCreationProvenance:
    source: str
    server_correlation_id: str | None
    native_thread_id: str
    approval_policy: str | Mapping[str, object]
    sandbox: Mapping[str, object]
    model: str
    cwd: str


@dataclass(frozen=True)
class CodexThreadReadBack:
    operation: str
    native_thread_id: str
    endpoint_id: str
    runtime_instance_id: str
    runtime_home_id: str
    runtime_home_realpath: str
    project_id: str
    repo_id: str
    canonical_cwd: str
    provider_revision: str


@dataclass(frozen=True)
class CodexStartEvidence:
    """Closed, create-only evidence for one managed Codex thread.

    This value is deliberately separate from the attach probe result: a native
    start correlation and an independent exact read-back are both required.
    It proves no binding and performs no native operation.
    """

    native_thread_id: str
    endpoint_id: str
    runtime_instance_id: str
    runtime_home_id: str
    runtime_home_realpath: str
    project_id: str
    repo_id: str
    canonical_cwd: str
    provider_revision: str
    creation: CodexCreationProvenance
    read_back: CodexThreadReadBack


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
class ManagedStartRequest:
    workspace_id: str
    scope_kind: str
    scope_identity: str
    conversation_id: str
    participant_id: str
    agent_id: str
    endpoint_id: str
    runtime_instance_id: str


class ManagedStartResponseLost(SessionLifecycleError):
    """The native start request may have succeeded but its response was lost."""


@dataclass(frozen=True)
class LifecycleChallenge:
    challenge_id: str
    challenge_token: str
    expires_at_utc: str


_CODEX_START_EVIDENCE_FIELDS = frozenset(
    {
        "native_thread_id",
        "endpoint_id",
        "runtime_instance_id",
        "runtime_home_id",
        "runtime_home_realpath",
        "project_id",
        "repo_id",
        "canonical_cwd",
        "provider_revision",
        "creation_provenance",
        "read_back",
    }
)
_CREATION_PROVENANCE_FIELDS = frozenset(
    {"source", "native_thread_id", "approval_policy", "sandbox", "model", "cwd"}
)
_CREATION_PROVENANCE_OPTIONAL_FIELDS = frozenset({"server_correlation_id"})
_READ_BACK_FIELDS = frozenset(
    {
        "operation",
        "native_thread_id",
        "endpoint_id",
        "runtime_instance_id",
        "runtime_home_id",
        "runtime_home_realpath",
        "project_id",
        "repo_id",
        "canonical_cwd",
        "provider_revision",
    }
)
_RESERVED_CODEX_IDENTITIES = frozenset({"*", "current", "frontmost", "latest", "newest"})


def validate_codex_start_evidence(
    candidate: Mapping[str, object],
    *,
    runtime_home: RuntimeHomeIdentity,
    trusted_project_root: TrustedProjectRoot,
    expected_endpoint_id: str,
    expected_runtime_instance_id: str,
    provider_revision: str,
) -> CodexStartEvidence:
    """Validate one create-only Codex start result against trusted launch inputs."""
    if not isinstance(candidate, Mapping) or set(candidate) != _CODEX_START_EVIDENCE_FIELDS:
        raise SessionLifecycleError("Codex start evidence has an unexpected shape")
    if not isinstance(runtime_home, RuntimeHomeIdentity):
        raise SessionLifecycleError("runtime_home must be a RuntimeHomeIdentity")
    expected_cwd = _trusted_canonical_cwd(trusted_project_root)
    expected = {
        "endpoint_id": expected_endpoint_id,
        "runtime_instance_id": expected_runtime_instance_id,
        "runtime_home_id": runtime_home.runtime_home_id,
        "runtime_home_realpath": runtime_home.runtime_home_realpath,
        "project_id": trusted_project_root.project_id,
        "repo_id": trusted_project_root.repo_id,
        "canonical_cwd": expected_cwd,
        "provider_revision": provider_revision,
    }
    for field, expected_value in expected.items():
        actual = _start_text(candidate.get(field), field)
        if actual != _start_text(expected_value, field):
            raise SessionLifecycleError(f"Codex start evidence {field} does not match trusted inputs")

    native_thread_id = _start_text(candidate.get("native_thread_id"), "native_thread_id")
    provenance = candidate["creation_provenance"]
    if not isinstance(provenance, Mapping):
        raise SessionLifecycleError("Codex start creation provenance is incomplete")
    provenance_fields = set(provenance)
    if provenance_fields - (_CREATION_PROVENANCE_FIELDS | _CREATION_PROVENANCE_OPTIONAL_FIELDS):
        raise SessionLifecycleError("Codex start creation provenance is incomplete")
    if provenance_fields - _CREATION_PROVENANCE_OPTIONAL_FIELDS != _CREATION_PROVENANCE_FIELDS:
        raise SessionLifecycleError("Codex start creation provenance is incomplete")
    if provenance.get("source") != "managed_thread_start":
        raise SessionLifecycleError("Codex start creation source is invalid")
    server_correlation_id = provenance.get("server_correlation_id")
    if server_correlation_id is not None:
        server_correlation_id = _start_text(server_correlation_id, "server_correlation_id")
    approval_policy = _codex_approval_policy(provenance.get("approval_policy"))
    sandbox = _codex_sandbox(provenance.get("sandbox"))
    model = _start_text(provenance.get("model"), "creation model")
    cwd = _start_text(provenance.get("cwd"), "creation cwd")
    if cwd != expected_cwd:
        raise SessionLifecycleError("Codex creation cwd does not match trusted cwd")
    creation = CodexCreationProvenance(
        source="managed_thread_start",
        server_correlation_id=server_correlation_id,
        native_thread_id=_start_text(provenance.get("native_thread_id"), "creation native_thread_id"),
        approval_policy=approval_policy,
        sandbox=sandbox,
        model=model,
        cwd=cwd,
    )
    if creation.native_thread_id != native_thread_id:
        raise SessionLifecycleError("Codex creation provenance thread id mismatch")

    read_back_value = candidate["read_back"]
    if not isinstance(read_back_value, Mapping) or set(read_back_value) != _READ_BACK_FIELDS:
        raise SessionLifecycleError("Codex start read-back evidence is incomplete")
    read_back = CodexThreadReadBack(
        operation=_start_text(read_back_value.get("operation"), "read-back operation"),
        native_thread_id=_start_text(
            read_back_value.get("native_thread_id"), "read-back native_thread_id"
        ),
        endpoint_id=_start_text(read_back_value.get("endpoint_id"), "read-back endpoint_id"),
        runtime_instance_id=_start_text(
            read_back_value.get("runtime_instance_id"), "read-back runtime_instance_id"
        ),
        runtime_home_id=_start_text(
            read_back_value.get("runtime_home_id"), "read-back runtime_home_id"
        ),
        runtime_home_realpath=_start_text(
            read_back_value.get("runtime_home_realpath"), "read-back runtime_home_realpath"
        ),
        project_id=_start_text(read_back_value.get("project_id"), "read-back project_id"),
        repo_id=_start_text(read_back_value.get("repo_id"), "read-back repo_id"),
        canonical_cwd=_start_text(read_back_value.get("canonical_cwd"), "read-back canonical_cwd"),
        provider_revision=_start_text(
            read_back_value.get("provider_revision"), "read-back provider_revision"
        ),
    )
    if read_back.operation != "thread_read":
        raise SessionLifecycleError("Codex start read-back operation is invalid")
    read_back_fields = {
        "native_thread_id": native_thread_id,
        **expected,
    }
    if any(getattr(read_back, field) != value for field, value in read_back_fields.items()):
        raise SessionLifecycleError("Codex start read-back does not match trusted evidence")
    return CodexStartEvidence(
        native_thread_id=native_thread_id,
        endpoint_id=expected_endpoint_id,
        runtime_instance_id=expected_runtime_instance_id,
        runtime_home_id=runtime_home.runtime_home_id,
        runtime_home_realpath=runtime_home.runtime_home_realpath,
        project_id=trusted_project_root.project_id,
        repo_id=trusted_project_root.repo_id,
        canonical_cwd=expected_cwd,
        provider_revision=provider_revision,
        creation=creation,
        read_back=read_back,
    )


def _codex_approval_policy(value: object) -> str | Mapping[str, object]:
    if isinstance(value, str):
        if value not in {"untrusted", "on-request", "never"}:
            raise SessionLifecycleError("Codex creation approval policy is invalid")
        return value
    if not isinstance(value, Mapping) or set(value) != {"granular"}:
        raise SessionLifecycleError("Codex creation approval policy is invalid")
    granular = value.get("granular")
    if not isinstance(granular, Mapping):
        raise SessionLifecycleError("Codex creation approval policy is invalid")
    required = {"mcp_elicitations", "rules", "sandbox_approval"}
    optional = {"request_permissions", "skill_approval"}
    if set(granular) - (required | optional) or not required <= set(granular):
        raise SessionLifecycleError("Codex creation approval policy is invalid")
    if any(not isinstance(granular[key], bool) for key in granular):
        raise SessionLifecycleError("Codex creation approval policy is invalid")
    return dict(value)


def _codex_sandbox(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
        raise SessionLifecycleError("Codex creation sandbox is invalid")
    allowed = {"dangerFullAccess", "readOnly", "externalSandbox", "workspaceWrite"}
    if value["type"] not in allowed:
        raise SessionLifecycleError("Codex creation sandbox is invalid")
    return dict(value)


def _start_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        or value.casefold() in _RESERVED_CODEX_IDENTITIES
    ):
        raise SessionLifecycleError(f"Codex start evidence {field} must be exact text")
    return value


def _trusted_canonical_cwd(trusted_project_root: TrustedProjectRoot) -> str:
    if not isinstance(trusted_project_root, TrustedProjectRoot):
        raise SessionLifecycleError("trusted_project_root is required")
    try:
        if trusted_project_root.descriptor_chain is None:
            repo_root = os.path.realpath(trusted_project_root.repo_root)
            cwd = os.path.realpath(trusted_project_root.cwd)
            if not os.path.isdir(repo_root) or not os.path.isdir(cwd):
                raise SessionLifecycleError("trusted project paths must be directories")
        else:
            repo_root, cwd = trusted_project_root.descriptor_chain.canonical_paths()
            if (repo_root != trusted_project_root.repo_root
                    or cwd != trusted_project_root.cwd):
                raise SessionLifecycleError("trusted descriptor paths do not match")
        if os.path.commonpath((repo_root, cwd)) != repo_root:
            raise SessionLifecycleError("trusted cwd must be under the repository root")
    except (TypeError, ValueError) as error:
        raise SessionLifecycleError("trusted project paths are invalid") from error
    return cwd


@dataclass(frozen=True)
class FakeLifecycleProvider:
    """START-FIXTURE provider for tests and the inert managed-start saga.

    This fixture builds ``SessionRefV1`` directly, advertises the broad default
    operations (including ``start`` and presentation-only ``open_ui``), and is
    deliberately not the pattern for a real attached-session provider. New
    attached-session providers such as Codex, Claude, or OpenCode MUST use the
    identity-only attester role below instead.
    """

    provider_id: str = "provider_codex"
    provider_revision: str = "revision_1"
    authority_identity: str = "fake_lifecycle_provider"
    capability_profile_id: str = "native_session_binding"
    capability_profile_revision: str = "revision_1"
    trust_class: str = "managed"
    supported_operations_json: str = DEFAULT_SUPPORTED_OPERATIONS_JSON
    challenge_algorithm: str = "sha256"
    challenge_ttl_seconds: int = 60

    def authority(self) -> SessionAuthority:
        return SessionAuthority(
            authority_kind="native_runtime",
            identity=self.authority_identity,
            implementation_revision=self.provider_revision,
            capability_profile_id=self.capability_profile_id,
            capability_profile_revision=self.capability_profile_revision,
        )

    def descriptor(self) -> Mapping[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_revision": self.provider_revision,
            "trust_class": self.trust_class,
            "supported_operations_json": self.supported_operations_json,
            "challenge_algorithm": self.challenge_algorithm,
            "challenge_ttl_seconds": self.challenge_ttl_seconds,
        }

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


@dataclass(frozen=True)
class PiLifecycleProvider:
    """NATIVE-ATTACHED authority for production Pi extension sessions.

    Pi is a native attachment (``native_attached``), not a lifecycle process
    llm-collab manages. It independently owns its authority/descriptor/attest
    plumbing and keeps presentation-only ``open_ui``; it is deliberately not
    the identity-only exact-probe pattern used by Codex, Claude, or OpenCode.
    Identity values are frozen; the real proof is build_session_ref's
    repository/runtime-home checks, which the extension-supplied endpoint/native
    ids and cwd feed. The extension owns those ids; reserve/consume freeze them,
    and the bridge derives no second identity.
    """

    provider_id: str = "provider_pi"
    provider_revision: str = "revision_1"
    authority_identity: str = "pi_extension_provider"
    capability_profile_id: str = "native_session_binding"
    capability_profile_revision: str = "revision_1"
    trust_class: str = "native_attached"
    supported_operations_json: str = DEFAULT_SUPPORTED_OPERATIONS_JSON
    challenge_algorithm: str = "sha256"
    challenge_ttl_seconds: int = 60

    def authority(self) -> SessionAuthority:
        return SessionAuthority(
            authority_kind="native_runtime",
            identity=self.authority_identity,
            implementation_revision=self.provider_revision,
            capability_profile_id=self.capability_profile_id,
            capability_profile_revision=self.capability_profile_revision,
        )

    def descriptor(self) -> Mapping[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_revision": self.provider_revision,
            "trust_class": self.trust_class,
            "supported_operations_json": self.supported_operations_json,
            "challenge_algorithm": self.challenge_algorithm,
            "challenge_ttl_seconds": self.challenge_ttl_seconds,
        }

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


@dataclass(frozen=True, kw_only=True)
class CodexLifecycleProvider(FakeLifecycleProvider):
    """IDENTITY-ONLY exact-thread attester for Codex attached sessions.

    This is the required pattern for new attached-session providers, including
    the incoming Claude provider and a future OpenCode provider: call an
    injected exact-evidence source FIRST, compare the exact native ID, then
    delegate SessionRefV1 construction. The supported operations are exactly
    ``["reserve", "attach"]`` and ``open_ui`` fails closed. It must not be
    treated as the START-FIXTURE role inherited for code reuse.

    Proves exact native-thread IDENTITY / addressability only, through an INJECTED
    exact-thread probe the caller has already bound to the trusted endpoint/runtime
    home. ``attest`` calls the probe with ``subject.native_session_id`` and requires
    the returned thread id to match before delegating SessionRefV1 construction to
    the inherited ``FakeLifecycleProvider.attest`` (no copied
    authority/descriptor/repository-binding plumbing).

    This is the lifecycle identity/re-attestation precondition #94 consumes; it is
    NOT idle/admissibility state and is NOT authorization to turn/start. The merged
    probe_exact_thread proves identity only; the missing exact idle/admissibility
    observation is a later #93 slice. A probe exception fails closed and retains its
    real type/cause. No start/resume/fork/turn/open_ui; no delivery/receipt store.
    """

    exact_thread_probe: Callable[[str], CodexAppServerExactThreadResult]
    authority_identity: str = "codex_exact_thread_provider"
    supported_operations_json: str = '["reserve","attach"]'

    def attest(
        self,
        subject: LifecycleSubject,
        *,
        runtime_home: RuntimeHomeIdentity,
        observed_at_utc: str,
        correlation_id: str,
        trusted_project_root: TrustedProjectRoot | None = None,
    ) -> Mapping[str, object]:
        # Prove exact identity FIRST; require the returned id to match before any
        # binding/ledger mutation. A probe exception propagates (fail closed, real
        # type/cause); only a mismatch is normalized to SessionLifecycleError.
        proven = self.exact_thread_probe(subject.native_session_id)
        if not isinstance(proven, CodexAppServerExactThreadResult) or proven.thread_id != subject.native_session_id:
            raise SessionLifecycleError("exact-thread probe did not prove the subject native session id")
        return super().attest(
            subject,
            runtime_home=runtime_home,
            observed_at_utc=observed_at_utc,
            correlation_id=correlation_id,
            trusted_project_root=trusted_project_root,
        )

    def open_ui(self, subject: LifecycleSubject) -> dict[str, object]:
        # Identity-only provider: open_ui is not supported and must fail closed
        # rather than inherit FakeLifecycleProvider's success-returning stub.
        raise SessionLifecycleError(
            "CodexLifecycleProvider is identity-only; open_ui is not supported"
        )


@dataclass(frozen=True, kw_only=True)
class ClaudeLifecycleProvider(FakeLifecycleProvider):
    """Identity-only Claude attester over injected hook/channel evidence.

    This provider does not authenticate a real Claude session by itself. The
    real hook, channel, and CLAUDE_HOME identity binding remain gated; this
    class only enforces their injected evidence contract before SessionRefV1
    construction.
    """

    attach_evidence: Callable[
        [LifecycleSubject, TrustedProjectRoot | None, RuntimeHomeIdentity],
        "ClaudeAttachEvidenceResult",
    ]
    provider_id: str = "provider_claude"
    authority_identity: str = "claude_session_start_provider"
    supported_operations_json: str = '["reserve","attach"]'

    def attest(
        self,
        subject: LifecycleSubject,
        *,
        runtime_home: RuntimeHomeIdentity,
        observed_at_utc: str,
        correlation_id: str,
        trusted_project_root: TrustedProjectRoot | None = None,
    ) -> Mapping[str, object]:
        from llm_collab.claude_attach_evidence import ClaudeAttachEvidenceResult

        proven = self.attach_evidence(subject, trusted_project_root, runtime_home)
        expected_cwd = _trusted_canonical_cwd(trusted_project_root)
        try:
            cwd_matches = os.path.realpath(proven.validated_cwd) == expected_cwd
        except (AttributeError, TypeError, ValueError):
            cwd_matches = False
        if (
            not isinstance(proven, ClaudeAttachEvidenceResult)
            or proven.native_session_id != subject.native_session_id
            or not cwd_matches
        ):
            raise SessionLifecycleError(
                "Claude attach evidence did not prove the subject session and cwd"
            )
        return super().attest(
            subject,
            runtime_home=runtime_home,
            observed_at_utc=observed_at_utc,
            correlation_id=correlation_id,
            trusted_project_root=trusted_project_root,
        )

    def open_ui(self, subject: LifecycleSubject) -> dict[str, object]:
        raise SessionLifecycleError(
            "ClaudeLifecycleProvider is identity-only; open_ui is not supported"
        )


class SessionLifecycleCore:
    def __init__(
        self,
        provider: FakeLifecycleProvider,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.provider = provider
        self._token_factory = token_factory or _secret_token

    def register_participant(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        *,
        created_at_utc: str,
        registry_revision: str,
    ) -> None:
        """Idempotently pre-provision the conversation participant.

        Does NOT write the trusted provider registry -- that is a separate
        authority (P1-1): worker-start consumes an already-approved registry
        entry; it does not mint one. Required before ``reserve``/``consume``;
        re-registering the same participant key with the same identity is a no-op.
        """
        store.register_conversation_participant(
            workspace_id=subject.workspace_id,
            scope_kind=subject.scope_kind,
            scope_identity=subject.scope_identity,
            conversation_id=subject.conversation_id,
            participant_id=subject.participant_id,
            agent_id=subject.agent_id,
            created_at_utc=created_at_utc,
            registry_revision=registry_revision,
        )


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
        _require_project_subject(subject)
        # P1-1: require the provider to be pre-approved in the trusted registry
        # BEFORE any probe/attest — the verb does not mint it.
        descriptor = self.provider.descriptor()
        if not store.has_lifecycle_provider(
            workspace_id=subject.workspace_id,
            provider_id=descriptor["provider_id"],
            provider_revision=descriptor["provider_revision"],
        ):
            raise SessionLifecycleError(
                "provider is not pre-approved in the trusted registry"
            )
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
            provider_descriptor=self.provider.descriptor(),
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
        binding_state: str = "active",
    ) -> dict[str, object]:
        _require_project_subject(subject)
        session_ref = self.provider.attest(
            subject,
            runtime_home=runtime_home,
            observed_at_utc=consumed_at_utc,
            correlation_id=correlation_id,
            trusted_project_root=trusted_project_root,
        )
        session_owner_key = derive_session_owner_key(
            workspace_id=subject.workspace_id,
            endpoint_id=subject.endpoint_id,
            native_session_id=subject.native_session_id,
            runtime_home=runtime_home,
            authority=self.provider.authority(),
        )
        return store.consume_session_binding_challenge(
            workspace_id=subject.workspace_id,
            scope_kind=subject.scope_kind,
            scope_identity=subject.scope_identity,
            conversation_id=subject.conversation_id,
            participant_id=subject.participant_id,
            agent_id=subject.agent_id,
            challenge_id=challenge.challenge_id,
            challenge_token_sha256=_sha256_text(challenge.challenge_token),
            provider_id=self.provider.provider_id,
            provider_revision=self.provider.provider_revision,
            endpoint_id=subject.endpoint_id,
            session_ref_id=str(session_ref["session_ref_id"]),
            session_owner_key=session_owner_key,
            native_session_id=subject.native_session_id,
            runtime_instance_id=subject.runtime_instance_id,
            consumed_at_utc=consumed_at_utc,
            binding_state=binding_state,
        )

    def start_managed(
        self,
        store: LedgerStore,
        request: ManagedStartRequest,
        *,
        runtime_home: RuntimeHomeIdentity,
        trusted_project_root: TrustedProjectRoot,
        created_at_utc: str,
        expires_at_utc: str,
        correlation_id: str,
        start_native: Callable[[str], Mapping[str, object]],
    ) -> dict[str, object]:
        """Run the fake/provider start saga without enabling live transport."""
        if not isinstance(request, ManagedStartRequest):
            raise SessionLifecycleError("managed start request is invalid")
        if request.scope_kind != "project" or request.scope_identity != trusted_project_root.project_id:
            raise SessionLifecycleError("managed start request project does not match trusted root")
        if not isinstance(runtime_home, RuntimeHomeIdentity):
            raise SessionLifecycleError("runtime_home must be a RuntimeHomeIdentity")
        start_id = "start_" + secrets.token_hex(32)
        canonical_cwd = _trusted_canonical_cwd(trusted_project_root)
        store.reserve_managed_start(
            workspace_id=request.workspace_id,
            scope_kind=request.scope_kind,
            scope_identity=request.scope_identity,
            conversation_id=request.conversation_id,
            participant_id=request.participant_id,
            agent_id=request.agent_id,
            provider_descriptor=self.provider.descriptor(),
            endpoint_id=request.endpoint_id,
            runtime_instance_id=request.runtime_instance_id,
            runtime_home_id=runtime_home.runtime_home_id,
            runtime_home_realpath=runtime_home.runtime_home_realpath,
            project_id=trusted_project_root.project_id,
            repo_id=trusted_project_root.repo_id,
            canonical_cwd=canonical_cwd,
            start_id=start_id,
            start_correlation_id=correlation_id,
            expires_at_utc=expires_at_utc,
            created_at_utc=created_at_utc,
        )
        try:
            candidate = start_native(start_id)
        except ManagedStartResponseLost:
            store.mark_managed_start(
                workspace_id=request.workspace_id,
                start_id=start_id,
                state="ambiguous_start",
                updated_at_utc=created_at_utc,
                failure_reason="start response lost; exact native identity is unknown",
            )
            raise
        except Exception as error:
            store.mark_managed_start(
                workspace_id=request.workspace_id,
                start_id=start_id,
                state="failed",
                updated_at_utc=created_at_utc,
                failure_reason=type(error).__name__,
            )
            raise
        evidence: CodexStartEvidence | None = None
        session_ref_id: str | None = None
        try:
            evidence = validate_codex_start_evidence(
                candidate,
                runtime_home=runtime_home,
                trusted_project_root=trusted_project_root,
                expected_endpoint_id=request.endpoint_id,
                expected_runtime_instance_id=request.runtime_instance_id,
                provider_revision=self.provider.provider_revision,
            )
            subject = LifecycleSubject(
                workspace_id=request.workspace_id,
                scope_kind=request.scope_kind,
                scope_identity=request.scope_identity,
                conversation_id=request.conversation_id,
                participant_id=request.participant_id,
                agent_id=request.agent_id,
                endpoint_id=request.endpoint_id,
                native_session_id=evidence.native_thread_id,
                runtime_instance_id=request.runtime_instance_id,
            )
            session_ref = self.provider.attest(
                subject,
                runtime_home=runtime_home,
                observed_at_utc=created_at_utc,
                correlation_id=correlation_id,
                trusted_project_root=trusted_project_root,
            )
            session_ref_id = str(session_ref["session_ref_id"])
            token = self._token_factory()
            if not isinstance(token, str) or not token:
                raise SessionLifecycleError("challenge token factory must return non-empty text")
            challenge_id = "challenge_" + _sha256_text(f"{start_id}|{token}")[:32]
            owner_key = derive_session_owner_key(
                workspace_id=subject.workspace_id,
                endpoint_id=subject.endpoint_id,
                native_session_id=subject.native_session_id,
                runtime_home=runtime_home,
                authority=self.provider.authority(),
            )
            binding = store.complete_managed_start(
                workspace_id=request.workspace_id,
                start_id=start_id,
                native_session_id=evidence.native_thread_id,
                session_ref_id=session_ref_id,
                session_owner_key=owner_key,
                evidence_sha256=codex_start_evidence_digest(evidence),
                provider_id=self.provider.provider_id,
                provider_revision=self.provider.provider_revision,
                endpoint_id=request.endpoint_id,
                runtime_instance_id=request.runtime_instance_id,
                runtime_home_id=runtime_home.runtime_home_id,
                runtime_home_realpath=runtime_home.runtime_home_realpath,
                project_id=trusted_project_root.project_id,
                repo_id=trusted_project_root.repo_id,
                canonical_cwd=canonical_cwd,
                challenge_id=challenge_id,
                challenge_token_sha256=_sha256_text(token),
                consumed_at_utc=created_at_utc,
            )
        except Exception as error:
            if evidence is None or session_ref_id is None:
                store.mark_managed_start(
                    workspace_id=request.workspace_id,
                    start_id=start_id,
                    state="failed",
                    updated_at_utc=created_at_utc,
                    failure_reason=type(error).__name__,
                )
            else:
                store.mark_managed_start(
                    workspace_id=request.workspace_id,
                    start_id=start_id,
                    state="orphaned",
                    updated_at_utc=created_at_utc,
                    failure_reason=type(error).__name__,
                    native_session_id=evidence.native_thread_id,
                    session_ref_id=session_ref_id,
                    evidence_sha256=codex_start_evidence_digest(evidence),
                )
            raise
        return {
            "start_id": start_id,
            "evidence": evidence,
            "session_ref": session_ref,
            "binding": binding,
        }

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

    def inspect_for_operator(
        self,
        store: LedgerStore,
        subject: LifecycleSubject,
        *,
        expected_binding_id: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, object]:
        """Return a fixed read-only operator projection for one participant binding."""

        if store._connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise SessionLifecycleError("operator inspection requires a query-only reader")
        resolved = self.inspect(
            store,
            subject,
            expected_binding_id=expected_binding_id,
            expected_generation=expected_generation,
        )
        return {
            "projection_kind": "session_lifecycle_operator_inspection_v1",
            "authority": "read_only_inspection",
            "resolved": resolved["resolved"],
            "reason": resolved["reason"],
            "workspace_id": resolved["workspace_id"],
            "scope_kind": resolved["scope_kind"],
            "scope_identity": resolved["scope_identity"],
            "conversation_id": resolved["conversation_id"],
            "participant_id": resolved["participant_id"],
            "binding_id": resolved.get("binding_id"),
            "generation": resolved.get("generation"),
            "state": resolved.get("state"),
            "mutation_capable": resolved.get("mutation_capable"),
            "provider_id": resolved.get("provider_id"),
            "provider_revision": resolved.get("provider_revision"),
            "endpoint_id": resolved.get("endpoint_id"),
            "session_ref_id": resolved.get("session_ref_id"),
            "native_session_id": resolved.get("native_session_id"),
            "runtime_instance_id": resolved.get("runtime_instance_id"),
        }

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
        _require_project_subject(subject)
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
        _require_project_subject(subject)
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
        _require_project_subject(subject)
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
        _require_project_subject(subject)
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
    _require_project_subject(subject)
    if trusted_project_root is None:
        raise SessionLifecycleError("trusted project root is required for project attestation")
    if trusted_project_root.project_id != subject.scope_identity:
        raise SessionLifecycleError("trusted project root does not match subject scope")
    return trusted_project_root.repository_binding()


def _require_project_subject(subject: LifecycleSubject) -> None:
    if subject.scope_kind != "project":
        raise SessionLifecycleError(
            "attached-session lifecycle registration requires project scope"
        )

def codex_start_evidence_digest(evidence: CodexStartEvidence) -> str:
    if not isinstance(evidence, CodexStartEvidence):
        raise SessionLifecycleError("evidence must be CodexStartEvidence")
    body = json.dumps(asdict(evidence), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _secret_token() -> str:
    return secrets.token_urlsafe(32)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SessionLifecycleError(f"{name} must be a positive integer")
    return value
