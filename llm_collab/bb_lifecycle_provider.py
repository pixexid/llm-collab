"""MANAGED-START authority for bb-hosted worker sessions (GH-563 Slice 1A).

bb creates and owns its own provider sessions, so llm-collab drives the start
rather than attaching to a session someone else made. That makes this a
``managed`` provider whose descriptor must advertise ``start``:
``reserve_managed_start`` and ``complete_managed_start`` both validate the
descriptor against ``frozenset({"start"})``, so a provider without it is refused
by the store before any native call happens.

That is why this is NOT modelled on ``CodexLifecycleProvider``, whose descriptor
is ``["reserve","attach"]`` — an identity-only attester for a desktop session
that already exists. Copying it would produce a provider that fails at the very
reservation the bb lane depends on.

It is equally not ``FakeLifecycleProvider``: inheriting its broad default
operation set would advertise ``heartbeat``, ``retire`` and ``open_ui`` that this
provider does not implement. The set here is the exact minimum the bb flow needs.

Slice 1A is deliberately unreachable — nothing routes to this provider yet. It
exists so Slice 1B has a settled identity contract to reserve against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .session_lifecycle import (
    LifecycleSubject,
    RuntimeHomeIdentity,
    SessionAuthority,
    SessionLifecycleError,
    TrustedProjectRoot,
    _repository_binding,
    build_session_ref,
)

# Exactly what the bb managed-start flow uses, and nothing more. `start` is
# mandatory (the store enforces it); `reserve` and `inspect` are the other two
# operations Slice 1B calls. Anything else would be advertising a capability
# this provider does not implement.
BB_SUPPORTED_OPERATIONS_JSON = '["reserve","start","inspect"]'


@dataclass(frozen=True)
class BbLifecycleProvider:
    """Managed-start identity attester for a bb-hosted native session.

    Identity values are frozen. The real proof is ``build_session_ref``'s
    repository and runtime-home checks, fed by the bb thread id that the spawn
    returned — never by an id this provider derives or guesses.
    """

    provider_id: str = "provider_bb"
    provider_revision: str = "revision_1"
    authority_identity: str = "bb_managed_provider"
    capability_profile_id: str = "native_session_binding"
    capability_profile_revision: str = "revision_1"
    trust_class: str = "managed"
    supported_operations_json: str = BB_SUPPORTED_OPERATIONS_JSON
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
        # _repository_binding raises when the project root is absent or does not
        # match the subject scope. Refusing is the point: a bb thread attested
        # against the wrong project root would bind a worker to someone else's
        # repository.
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
        # bb owns its own UI. Presenting a thread is bb's concern, not a
        # lifecycle operation llm-collab performs, and `open_ui` is absent from
        # this provider's advertised operations. Fail closed rather than inherit
        # FakeLifecycleProvider's success-returning stub.
        raise SessionLifecycleError(
            "BbLifecycleProvider does not implement open_ui; bb owns thread presentation"
        )
