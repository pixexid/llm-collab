"""First exact Codex delivery machinery (#94): one idle-thread next_turn.

INERT BY CONTRACT: nothing in production calls this module in this slice; the
daemon/worker-send wiring is a separately reviewed follow-up, as are busy
coalescing, steer, interrupt, UI refresh, correlation hardening, and the
canonical mutation lease. Every gate below fails closed with no partial
mutation: a delivery resolves the exact active binding and NEVER injects on a
busy or uncertain thread.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from llm_collab.canonical.legacy_packet_materialization import (
    materialize_selected_legacy_packet,
)
from llm_collab.codex_app_server_live_probe import (
    OBSERVATION_ADMISSIBLE,
    OBSERVATION_BUSY,
    CodexAppServerThreadObservation,
)
from llm_collab.codex_runtime_home import RuntimeHomeIdentity
from llm_collab.codex_session_ref import SessionAuthority
from llm_collab.ledger import LedgerStore
from llm_collab.session_lifecycle import (
    CodexLifecycleProvider,
    LifecycleSubject,
    TrustedProjectRoot,
)

OUTCOME_GATE_DISABLED = "gate_disabled"
OUTCOME_DEFERRED_BUSY = "deferred_busy"
OUTCOME_UNCERTAIN = "uncertain"
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REJECTED = "rejected_before_acceptance"
OUTCOME_AMBIGUOUS = "ambiguous"

_TERMINAL_TURN_METHODS = {"turn/completed", "turn/failed", "turn/cancelled"}
_TERMINAL_BY_METHOD = {
    "turn/completed": "completed",
    "turn/failed": "failed",
    "turn/cancelled": "cancelled",
}


class CodexDeliveryError(RuntimeError):
    pass


def deliver_next_turn_idle(
    store: LedgerStore,
    *,
    workspace_root: Path,
    session: Mapping[str, object],
    message: Mapping[str, object],
    subject: LifecycleSubject,
    provider: CodexLifecycleProvider,
    observe: Callable[[str], CodexAppServerThreadObservation],
    turn_transport: Any,
    runtime_home: RuntimeHomeIdentity,
    trusted_project_root: TrustedProjectRoot,
    observed_at_utc: str,
    correlation_id: str,
    model: str | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, object]:
    """Deliver one canonical message to one exact idle Codex thread, or do not.

    Order is non-negotiable: canonical write gate and binding-generation freeze
    first, exact-thread identity attestation second, admissibility observation
    third — a turn is started only when all three pass, and the outcome is
    receipted from native terminal evidence only.
    """
    # 1-2. Canonical message/delivery through the existing write gate, then the
    # binding-generation freeze. A mismatch/rebind or a disabled gate produces
    # no attempt and no dispatch.
    materialized = materialize_selected_legacy_packet(
        store,
        workspace_root=workspace_root,
        session=session,
        message=message,
    )
    if not materialized.get("materialized"):
        return {"outcome": OUTCOME_GATE_DISABLED, "materialized": False}
    message_id = str(materialized["message_id"])
    delivery_id = str(materialized["delivery_id"])
    attempt_id = str(materialized["attempt_id"])

    # 3. Exact-thread identity before ANY mutation; a probe failure raises and
    # retains its real type (fail closed by #415 contract).
    session_ref = provider.attest(
        subject,
        runtime_home=runtime_home,
        observed_at_utc=observed_at_utc,
        correlation_id=correlation_id,
        trusted_project_root=trusted_project_root,
    )
    session_ref_id = str(session_ref["session_ref_id"])

    # 4. Admissibility: inject ONLY on a proven admissible observation.
    observation = observe(subject.native_session_id)
    if (
        not isinstance(observation, CodexAppServerThreadObservation)
        or observation.thread_id != subject.native_session_id
    ):
        raise CodexDeliveryError("observation does not cover the exact bound thread")
    if observation.classification != OBSERVATION_ADMISSIBLE:
        state = (
            OUTCOME_DEFERRED_BUSY
            if observation.classification == OBSERVATION_BUSY
            else OUTCOME_AMBIGUOUS
        )
        receipt_id, _ = _append_native_receipt(
            store,
            subject=subject,
            provider=provider,
            message_id=message_id,
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            session_ref_id=session_ref_id,
            state=state,
            quality="best_effort",
            correlation_id=correlation_id,
            observed_at_utc=observed_at_utc,
            native_detail={"x_note_classification": observation.classification},
        )
        return {
            "outcome": OUTCOME_DEFERRED_BUSY if state == OUTCOME_DEFERRED_BUSY else OUTCOME_UNCERTAIN,
            "message_id": message_id,
            "delivery_id": delivery_id,
            "attempt_id": attempt_id,
            "receipt_id": receipt_id,
            "classification": observation.classification,
        }

    # 5. One turn on the exact thread over the provider-bound transport. The
    # read-gated probe _request is deliberately NOT reused; the frames below are
    # the delivery path's own. The prompt is a pointer, never the packet body.
    sender = str(message.get("frontmatter", {}).get("sender_agent_id", "unknown"))
    prompt = (
        f"[from {sender}] Read latest {session['agent_id']} packet in "
        f"{session['chat_id']}: {materialized['packet_relpath']}"
    )
    turn_transport.exchange(
        {
            "jsonrpc": "2.0",
            "id": "llm-collab-delivery-1",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "llm-collab-codex-delivery", "version": "0.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        }
    )
    turn_transport.notify({"jsonrpc": "2.0", "method": "initialized"})
    turn_transport.exchange(
        {
            "jsonrpc": "2.0",
            "id": "llm-collab-delivery-2",
            "method": "thread/resume",
            "params": {"threadId": subject.native_session_id},
        }
    )
    turn_payload: dict[str, Any] = {
        "threadId": subject.native_session_id,
        "input": [{"type": "text", "text": prompt}],
    }
    if model:
        turn_payload["model"] = model
    started = turn_transport.exchange(
        {
            "jsonrpc": "2.0",
            "id": "llm-collab-delivery-3",
            "method": "turn/start",
            "params": turn_payload,
        }
    )
    turn = (started.get("result") or {}).get("turn") if isinstance(started, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None

    terminal_status: str | None = None
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            frame = turn_transport.recv_json()
        except Exception:
            # A lost connection mid-turn is a lost response: ambiguous, never retried.
            break
        method = str(frame.get("method", "")) if isinstance(frame, dict) else ""
        if method in _TERMINAL_TURN_METHODS:
            terminal_status = _TERMINAL_BY_METHOD[method]
            break

    # 6. Receipt from NATIVE terminal evidence only; a lost response is
    # ambiguous and is never blindly retried (reconcile-first is a follow-up).
    if terminal_status == "completed":
        state, quality, outcome = OUTCOME_ACCEPTED, "authoritative", OUTCOME_ACCEPTED
    elif terminal_status == "failed":
        state, quality, outcome = (
            OUTCOME_REJECTED,
            "best_effort",
            OUTCOME_REJECTED,
        )
    else:
        state, quality, outcome = OUTCOME_AMBIGUOUS, "best_effort", OUTCOME_AMBIGUOUS
    receipt_id, _ = _append_native_receipt(
        store,
        subject=subject,
        provider=provider,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        session_ref_id=session_ref_id,
        state=state,
        quality=quality,
        correlation_id=correlation_id,
        observed_at_utc=observed_at_utc,
        native_detail={
            "x_note_turn_id": turn_id,
            "x_note_terminal_status": terminal_status,
        },
    )
    return {
        "outcome": outcome,
        "message_id": message_id,
        "delivery_id": delivery_id,
        "attempt_id": attempt_id,
        "receipt_id": receipt_id,
        "turn_id": turn_id,
        "terminal_status": terminal_status,
    }


def _append_native_receipt(
    store: LedgerStore,
    *,
    subject: LifecycleSubject,
    provider: CodexLifecycleProvider,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    session_ref_id: str,
    state: str,
    quality: str,
    correlation_id: str,
    observed_at_utc: str,
    native_detail: Mapping[str, object],
) -> tuple[str, bool]:
    from llm_collab.canonical.delivery import append_receipt

    evidence = _state_evidence(
        workspace_id=subject.workspace_id,
        project_id=subject.scope_identity,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        endpoint_id=subject.endpoint_id,
        session_ref_id=session_ref_id,
        native_session_id=subject.native_session_id,
        state=state,
        quality=quality,
        authority=provider.authority(),
        correlation_id=correlation_id,
        observed_at_utc=observed_at_utc,
        native_detail=native_detail,
    )
    return append_receipt(
        store,
        workspace_id=subject.workspace_id,
        scope_kind="project",
        scope_identity=subject.scope_identity,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        evidence=evidence,
        session_ref_id=session_ref_id,
        created_at_utc=observed_at_utc,
    )


def _state_evidence(
    *,
    workspace_id: str,
    project_id: str,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    endpoint_id: str,
    session_ref_id: str,
    state: str,
    quality: str,
    authority: SessionAuthority,
    correlation_id: str,
    observed_at_utc: str,
    native_detail: Mapping[str, object],
    native_session_id: str,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "scope": {"kind": "project", "project_id": project_id},
        "evidence_id": f"evidence_{correlation_id}_{state}",
        "evidence_kind": "native_delivery_state",
        "quality": quality,
        "state": state,
        "authority": {
            "authority_kind": authority.authority_kind,
            "identity": authority.identity,
            "implementation_revision": authority.implementation_revision,
            "capability_profile_id": authority.capability_profile_id,
            "capability_profile_revision": authority.capability_profile_revision,
        },
        "subject": {
            "message_id": message_id,
            "delivery_id": delivery_id,
            "attempt_id": attempt_id,
            "endpoint_id": endpoint_id,
            "session_ref_id": session_ref_id,
            "native_session_id": native_session_id,
        },
        "extensions": dict(native_detail),
        "correlation_id": correlation_id,
        "observed_at_utc": observed_at_utc,
    }
    projection = dict(evidence)
    body = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    evidence["integrity"] = "sha256:" + hashlib.sha256(body).hexdigest()
    return evidence
