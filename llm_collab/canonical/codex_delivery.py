"""First exact Codex delivery machinery (#94): one idle-thread next_turn.

INERT BY CONTRACT: nothing in production calls this module in this slice; the
daemon/worker-send wiring is a separately reviewed follow-up, as are busy
coalescing, steer, interrupt, UI refresh, correlation hardening, and the
canonical mutation lease. Durable intent lands first (the canonical
message/delivery/attempt rows ARE the recovery record), exact-thread identity
is attested before any NATIVE side effect, and a turn is started only for a
proven idle/admissible thread — never on busy or uncertain. One authority: the
session, the lifecycle subject, and the resolved canonical binding must agree
exactly before anything happens.
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

    Order is non-negotiable: one exact identity join (session/subject/binding
    agree), then durable intent (canonical rows) with the binding-generation
    freeze, then exact-thread identity attestation, then admissibility — a turn
    is started only when all four pass, and the outcome is receipted from
    native terminal evidence for the SAME turn id only.
    """
    # 0. ONE authority: the caller session and the lifecycle subject must agree
    # with each other (and therefore with the binding materialize resolves) on
    # every identity field, before any side effect of any kind.
    _require_exact_join(store, session, subject)

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

    # 5. One turn on the exact thread over the provider-bound transport, under
    # ONE absolute deadline checked before every blocking call (the transport's
    # own recv timeout bounds each read; no second timeout layer). The read-gated
    # probe _request is deliberately NOT reused; the prompt is a pointer, never
    # the packet body.
    sender = str(message.get("frontmatter", {}).get("sender_agent_id", "unknown"))
    prompt = (
        f"[from {sender}] Read latest {session['agent_id']} packet in "
        f"{session['chat_id']}: {materialized['packet_relpath']}"
    )
    deadline = time.monotonic() + timeout_seconds

    def _exchange(frame: dict[str, Any]) -> Mapping[str, Any]:
        if time.monotonic() >= deadline:
            raise CodexDeliveryError("absolute delivery deadline exceeded before exchange")
        return turn_transport.exchange(frame)

    _exchange(
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
    _exchange(
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
    started = _exchange(
        {
            "jsonrpc": "2.0",
            "id": "llm-collab-delivery-3",
            "method": "turn/start",
            "params": turn_payload,
        }
    )
    turn = (started.get("result") or {}).get("turn") if isinstance(started, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        # turn/start did not return a usable turn identity: nothing authoritative
        # can ever be claimed for this attempt.
        turn_id = None

    terminal_status: str | None = None
    while turn_id is not None and time.monotonic() < deadline:
        try:
            frame = turn_transport.recv_json()
        except Exception:
            # A lost connection mid-turn is a lost response: ambiguous, never retried.
            break
        if time.monotonic() >= deadline:
            # Evidence that arrives after the absolute deadline is not this
            # attempt's evidence.
            break
        method = str(frame.get("method", "")) if isinstance(frame, dict) else ""
        if method not in _TERMINAL_TURN_METHODS:
            continue
        params = frame.get("params") if isinstance(frame, dict) else None
        terminal_turn = params.get("turn") if isinstance(params, dict) else None
        terminal_turn_id = terminal_turn.get("id") if isinstance(terminal_turn, dict) else None
        if terminal_turn_id != turn_id:
            # A terminal for any other (or no) turn is not this delivery's evidence.
            continue
        terminal_status = _TERMINAL_BY_METHOD[method]
        break

    # 6. Receipt from NATIVE terminal evidence only; a lost response is
    # ambiguous and is never blindly retried (reconcile-first is a follow-up).
    if terminal_status == "completed":
        state, quality, outcome = OUTCOME_ACCEPTED, "authoritative", OUTCOME_ACCEPTED
    else:
        # turn/failed or turn/cancelled after a started turn, a lost response, or
        # a missing turn id: a turn EXISTS (or may), so this is never
        # rejected_before_acceptance — that state would authorize a blind retry.
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


def _require_exact_join(
    store: LedgerStore,
    session: Mapping[str, object],
    subject: LifecycleSubject,
) -> None:
    """Fail closed unless the caller session and the lifecycle subject describe
    one exact worker: workspace, project, chat, agent, endpoint, and native
    thread must all agree before any side effect. Two independently supplied
    identities are never allowed to split authority across one delivery."""
    runtime = session.get("runtime") or {}
    mismatches = []
    if subject.workspace_id != store.paths.workspace_id:
        mismatches.append("workspace_id")
    if subject.scope_kind != "project" or subject.scope_identity != session.get("project_id"):
        mismatches.append("project_id")
    if subject.conversation_id != session.get("chat_id"):
        mismatches.append("chat_id")
    if subject.participant_id != "participant_" + str(session.get("agent_id")):
        mismatches.append("participant_id")
    if subject.agent_id != "agent_" + str(session.get("agent_id")):
        mismatches.append("agent_id")
    session_endpoint = session.get("endpoint_id")
    if session_endpoint is not None and subject.endpoint_id != session_endpoint:
        mismatches.append("endpoint_id")
    if subject.native_session_id != str(runtime.get("session_id") or ""):
        mismatches.append("native_session_id")
    if mismatches:
        raise CodexDeliveryError(
            "session/subject identity split: " + ", ".join(mismatches)
        )


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
