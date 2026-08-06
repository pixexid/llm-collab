"""bb provider side of the managed-start saga (GH-564 Slice 1B, AC1/AC2/AC6).

`start_managed` owns the fence, the saga states and the binding write. This module
supplies the three provider-shaped pieces it now injects:

* a `start_native` callable that performs exactly one bb spawn,
* an evidence validator that checks the native result against the trusted inputs,
* a digest over that evidence for the reservation row.

The Codex equivalents stay untouched. Nothing here reaches the ledger, the
watcher, or routing — this is the provider boundary and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from llm_collab.bb_client import (
    REFUSAL_AMBIGUOUS,
    BbClient,
    BbProfile,
    BbRefusal,
    BbThread,
)
from llm_collab.managed_start_errors import (
    ManagedStartOrphaned,
    ManagedStartResponseLost,
    SessionLifecycleError,
)

BB_START_SOURCE = "managed_bb_thread_start"


@dataclass(frozen=True)
class BbStartEvidence:
    """Closed, create-only evidence for one managed bb thread.

    Deliberately its own type rather than a reshaped `CodexStartEvidence`: the two
    providers attest different facts, and a shared type would let one provider's
    validator accept the other's evidence. The profile is carried here because
    AC6 requires the exact triple to reach validated evidence BEFORE the binding
    is written — it is attested, not merely requested.
    """

    native_thread_id: str
    project_id: str
    environment_id: str
    provider_id: str
    status: str
    endpoint_id: str
    runtime_instance_id: str
    provider: str
    model: str
    reasoning_level: str
    source: str


def bb_start_evidence_digest(evidence: object) -> str:
    """Digest for the reservation row.

    Mirrors the Codex serialisation exactly — `sort_keys=True`,
    `separators=(",", ":")` — so both providers write a comparable digest shape
    into the same column. It refuses a foreign evidence type rather than hashing
    whatever it is handed, for the same reason the Codex one does.
    """
    if not isinstance(evidence, BbStartEvidence):
        raise SessionLifecycleError("evidence must be BbStartEvidence")
    body = json.dumps(asdict(evidence), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def validate_bb_start_evidence(
    candidate: Mapping[str, object],
    *,
    expected_project_id: str,
    expected_endpoint_id: str,
    expected_runtime_instance_id: str,
    expected_profile: BbProfile,
) -> BbStartEvidence:
    """Validate one bb start result against the trusted launch inputs.

    Every field is compared to something the caller already knew. A validator that
    only checked shape would accept a thread from another project or another
    profile, which is precisely what the exact-identity contract forbids.
    """
    if not isinstance(candidate, Mapping):
        raise SessionLifecycleError("bb start evidence is not a mapping")

    def text(field: str) -> str:
        value = candidate.get(field)
        if not isinstance(value, str) or not value:
            raise SessionLifecycleError(f"bb start evidence {field} must be exact text")
        return value

    expected = {
        "project_id": expected_project_id,
        "endpoint_id": expected_endpoint_id,
        "runtime_instance_id": expected_runtime_instance_id,
        "provider": expected_profile.provider,
        "model": expected_profile.model,
        "reasoning_level": expected_profile.reasoning_level,
    }
    for field, expected_value in expected.items():
        if text(field) != expected_value:
            raise SessionLifecycleError(
                f"bb start evidence {field} does not match trusted inputs"
            )
    if text("source") != BB_START_SOURCE:
        raise SessionLifecycleError("bb start evidence source is invalid")

    return BbStartEvidence(
        native_thread_id=text("native_thread_id"),
        project_id=text("project_id"),
        environment_id=text("environment_id"),
        provider_id=text("provider_id"),
        status=text("status"),
        endpoint_id=text("endpoint_id"),
        runtime_instance_id=text("runtime_instance_id"),
        provider=text("provider"),
        model=text("model"),
        reasoning_level=text("reasoning_level"),
        source=text("source"),
    )


def bb_start_native(
    client: BbClient,
    *,
    project_id: str,
    prompt: str,
    profile: BbProfile,
    endpoint_id: str,
    runtime_instance_id: str,
) -> Callable[[str], Mapping[str, object]]:
    """One bb spawn per managed start, with refusals mapped to saga shapes.

    The mapping is the whole point, and it is why this is not a bare lambda:

    * `bb_ambiguous_outcome` — bb may have created the thread and the report was
      lost. Raised as `ManagedStartResponseLost` so `start_managed` records
      `ambiguous_start` and does NOT retry. A retry here is a second real thread.
    * any refusal carrying a `native_thread_id` — the thread exists but could not
      be handed back. Raised as `ManagedStartOrphaned` WITH that id so the saga
      records an orphan it can reconcile, rather than a clean failure.
    * everything else — no thread was created; a plain failure is honest.

    `start_managed` never retries `start_native`, so this callable is invoked at
    most once per reservation.
    """

    def start(_start_id: str) -> Mapping[str, object]:
        outcome = client.spawn(project_id=project_id, prompt=prompt, profile=profile)
        if isinstance(outcome, BbRefusal):
            if outcome.native_thread_id:
                raise ManagedStartOrphaned(
                    f"bb spawn refused after creating a thread: {outcome.detail}",
                    native_session_id=outcome.native_thread_id,
                )
            if outcome.reason == REFUSAL_AMBIGUOUS:
                raise ManagedStartResponseLost(
                    f"bb spawn outcome is ambiguous: {outcome.detail}"
                )
            raise SessionLifecycleError(f"bb spawn failed: {outcome.reason}: {outcome.detail}")
        if not isinstance(outcome, BbThread):
            raise SessionLifecycleError("bb spawn returned an unexpected type")
        return {
            "native_thread_id": outcome.thread_id,
            "project_id": outcome.project_id,
            "environment_id": outcome.environment_id,
            "provider_id": outcome.provider_id,
            "status": outcome.status,
            "endpoint_id": endpoint_id,
            "runtime_instance_id": runtime_instance_id,
            "provider": profile.provider,
            "model": profile.model,
            "reasoning_level": profile.reasoning_level,
            "source": BB_START_SOURCE,
        }

    return start
