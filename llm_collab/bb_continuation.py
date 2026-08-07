"""Continuation and replay for an already-bound bb thread (GH-566, Slice 1C).

First delivery is intentionally absent here.  The session binding is resolved
from the canonical ledger on every call; this module only caches the validated
native identity and its replay cursor.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping

from llm_collab.bb_client import (
    REFUSAL_AMBIGUOUS,
    REFUSAL_TIMED_OUT,
    BbClient,
    BbEvent,
    BbEventPage,
    BbQueued,
    BbRefusal,
)
from llm_collab.canonical.control import (
    append_acknowledgment_receipt,
    append_dead_letter_receipt,
    require_canonical_write_gate,
)
from llm_collab.canonical.codex_delivery import _state_evidence
from llm_collab.canonical.legacy_packet_materialization import (
    _latest_project_registry_revision,
)
from llm_collab.ledger import LedgerStore
from llm_collab.ledger.store import CanonicalIntegrityError
from llm_collab.session_lifecycle import BbLifecycleProvider


BB_CONTINUATION_QUEUED = "queued"
BB_CONTINUATION_DUPLICATE = "duplicate"
BB_CONTINUATION_AMBIGUOUS = "ambiguous"
BB_CONTINUATION_FAILED = "failed"
BB_CONTINUATION_COMPLETED = "completed"

_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{64}\Z")
_DELIVERY_ID = re.compile(r"delivery_[0-9a-f]{64}\Z")
_ATTEMPT_ID = re.compile(r"attempt_[0-9a-f]{64}\Z")


class BbContinuationRefused(RuntimeError):
    """A stale or incomplete exact continuation request."""


@dataclass(frozen=True)
class BbContinuationResult:
    state: str
    detail: str
    message_id: str | None = None
    delivery_id: str | None = None
    attempt_id: str | None = None
    receipt_id: str | None = None
    last_event_seq: int | None = None
    native_called: bool = False


@dataclass(frozen=True)
class BbObservationResult:
    state: str
    detail: str
    last_event_seq: int
    processed_events: int
    receipt_id: str | None = None


def client_from_project(project: Mapping[str, object]) -> BbClient:
    """Build the default-off client from the registered project's bb settings."""
    bb = project.get("bb")
    if not isinstance(bb, Mapping) or bb.get("enabled") is not True:
        raise BbContinuationRefused("bb adapter is not enabled for this project")
    executable = bb.get("executable", ["bb"])
    if (
        not isinstance(executable, list)
        or not executable
        or any(not isinstance(token, str) or not token for token in executable)
    ):
        raise BbContinuationRefused("bb.executable is invalid")
    timeout = bb.get("timeout_seconds", 30.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise BbContinuationRefused("bb.timeout_seconds is invalid")
    from llm_collab.bb_client import subprocess_transport

    return BbClient(
        subprocess_transport(executable),
        enabled=True,
        timeout_seconds=float(timeout),
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise BbContinuationRefused(f"{name} is missing")
    return value


def _context(store: LedgerStore, session: Mapping[str, object]) -> dict[str, object]:
    if session.get("status") not in {"active", "parked"}:
        raise BbContinuationRefused("bb session is not active")
    if session.get("project_id") is None or session.get("chat_id") is None:
        raise BbContinuationRefused("bb session has no project/chat identity")
    runtime = session.get("runtime")
    if not isinstance(runtime, Mapping) or runtime.get("family") != "bb":
        raise BbContinuationRefused("session is not a bb runtime")
    native_thread_id = _text(runtime.get("session_id"), "runtime.session_id")
    project_id = _text(session.get("project_id"), "session.project_id")
    conversation_id = _text(session.get("chat_id"), "session.chat_id")
    agent_id = _text(session.get("agent_id"), "session.agent_id")
    binding_id = _text(session.get("binding_id"), "session.binding_id")
    generation = session.get("binding_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise BbContinuationRefused("session.binding_generation is invalid")
    participant_id = "participant_" + agent_id
    resolved = store.resolve_conversation_binding(
        workspace_id=store.paths.workspace_id,
        scope_kind="project",
        scope_identity=project_id,
        conversation_id=conversation_id,
        participant_id=participant_id,
        expected_binding_id=binding_id,
        expected_generation=generation,
    )
    if not resolved.get("resolved"):
        raise BbContinuationRefused(
            f"bb binding refused: {resolved.get('reason', 'unresolved')}"
        )
    for field, expected in (
        ("native_session_id", native_thread_id),
        ("endpoint_id", session.get("endpoint_id")),
    ):
        if not isinstance(resolved.get(field), str) or resolved.get(field) != expected:
            raise BbContinuationRefused(f"bb binding {field} does not match the session")
    return {
        "workspace_id": store.paths.workspace_id,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "participant_id": participant_id,
        "agent_id": agent_id,
        "binding_id": binding_id,
        "binding_generation": generation,
        "native_thread_id": native_thread_id,
        "session_ref_id": _text(resolved.get("session_ref_id"), "binding.session_ref_id"),
        "endpoint_id": _text(resolved.get("endpoint_id"), "binding.endpoint_id"),
        "runtime_instance_id": _text(
            resolved.get("runtime_instance_id"), "binding.runtime_instance_id"
        ),
    }


def _ids(materialized: Mapping[str, object]) -> tuple[str, str, str]:
    values = (
        materialized.get("message_id"),
        materialized.get("delivery_id"),
        materialized.get("attempt_id"),
    )
    validators = (_MESSAGE_ID, _DELIVERY_ID, _ATTEMPT_ID)
    names = ("message_id", "delivery_id", "attempt_id")
    for value, validator, name in zip(values, validators, names):
        if not isinstance(value, str) or validator.fullmatch(value) is None:
            raise BbContinuationRefused(f"canonical materialization has invalid {name}")
    return values  # type: ignore[return-value]


def _correlation(attempt_id: str, suffix: str) -> str:
    return "bb_" + hashlib.sha256(f"{attempt_id}|{suffix}".encode()).hexdigest()[:32]


def _receipt_evidence(
    *,
    context: Mapping[str, object],
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    state: str,
    quality: str,
    correlation_id: str,
    observed_at_utc: str,
    detail: Mapping[str, object],
) -> dict[str, object]:
    return _state_evidence(
        workspace_id=str(context["workspace_id"]),
        project_id=str(context["project_id"]),
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        endpoint_id=str(context["endpoint_id"]),
        session_ref_id=str(context["session_ref_id"]),
        native_session_id=str(context["native_thread_id"]),
        state=state,
        quality=quality,
        authority=BbLifecycleProvider().authority(),
        correlation_id=correlation_id,
        observed_at_utc=observed_at_utc,
        native_detail=detail,
    )


def _append_receipt(
    store: LedgerStore,
    *,
    context: Mapping[str, object],
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    registry_revision: str,
    state: str,
    quality: str,
    correlation_id: str,
    observed_at_utc: str,
    detail: Mapping[str, object],
    in_transaction: bool = False,
) -> tuple[str, bool]:
    evidence = _receipt_evidence(
        context=context,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        state=state,
        quality=quality,
        correlation_id=correlation_id,
        observed_at_utc=observed_at_utc,
        detail=detail,
    )
    kwargs = dict(
        store=store,
        workspace_id=str(context["workspace_id"]),
        scope_kind="project",
        scope_identity=str(context["project_id"]),
        registry_revision=registry_revision,
        allow_canonical_write=True,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        evidence=evidence,
        session_ref_id=str(context["session_ref_id"]),
        created_at_utc=observed_at_utc,
        _in_transaction=in_transaction,
    )
    if state in {"accepted", "completed"}:
        return append_acknowledgment_receipt(**kwargs)
    return append_dead_letter_receipt(**kwargs)


def _observation(
    store: LedgerStore, context: Mapping[str, object], now: str
) -> dict[str, object]:
    return store.ensure_bb_thread_observation(
        workspace_id=str(context["workspace_id"]),
        scope_kind="project",
        scope_identity=str(context["project_id"]),
        conversation_id=str(context["conversation_id"]),
        participant_id=str(context["participant_id"]),
        binding_id=str(context["binding_id"]),
        binding_generation=int(context["binding_generation"]),
        native_thread_id=str(context["native_thread_id"]),
        session_ref_id=str(context["session_ref_id"]),
        updated_at_utc=now,
    )


def _advance(
    store: LedgerStore,
    context: Mapping[str, object],
    *,
    event_seq: int,
    dispatch_state: str,
    now: str,
    ids: tuple[str, str, str] | None = None,
) -> dict[str, object]:
    message_id, delivery_id, attempt_id = ids or (None, None, None)
    return store.advance_bb_thread_observation(
        workspace_id=str(context["workspace_id"]),
        scope_kind="project",
        scope_identity=str(context["project_id"]),
        conversation_id=str(context["conversation_id"]),
        participant_id=str(context["participant_id"]),
        binding_id=str(context["binding_id"]),
        binding_generation=int(context["binding_generation"]),
        native_thread_id=str(context["native_thread_id"]),
        session_ref_id=str(context["session_ref_id"]),
        event_seq=event_seq,
        dispatch_state=dispatch_state,
        updated_at_utc=now,
        last_message_id=message_id,
        last_delivery_id=delivery_id,
        last_attempt_id=attempt_id,
    )


def _ambiguous_without_retry(
    *,
    ids: tuple[str, str, str],
    detail: str,
    row: Mapping[str, object],
) -> BbContinuationResult:
    return BbContinuationResult(
        BB_CONTINUATION_AMBIGUOUS,
        detail,
        message_id=ids[0],
        delivery_id=ids[1],
        attempt_id=ids[2],
        last_event_seq=int(row["last_event_seq"]),
        native_called=False,
    )


def continue_bb_thread(
    store: LedgerStore,
    *,
    client: BbClient,
    session: Mapping[str, object],
    message: Mapping[str, object],
    materialized: Mapping[str, object],
    observed_at_utc: str,
) -> BbContinuationResult:
    """Send one canonical delivery to the exact stored bb thread.

    The observation row is marked queued before the native call.  If the process
    dies after bb accepts the message but before the receipt, the next attempt
    returns ``ambiguous`` and never sends a second turn.
    """
    context = _context(store, session)
    message_id, delivery_id, attempt_id = _ids(materialized)
    delivery = store.read_canonical_delivery(
        workspace_id=str(context["workspace_id"]),
        scope_kind="project",
        scope_identity=str(context["project_id"]),
        message_id=message_id,
        delivery_id=delivery_id,
    )
    if delivery is None:
        raise BbContinuationRefused("canonical delivery is missing")
    selected = delivery.get("selected_receipt")
    if isinstance(selected, Mapping):
        return BbContinuationResult(
            BB_CONTINUATION_DUPLICATE,
            "canonical delivery already has a receipt",
            message_id=message_id,
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            receipt_id=str(selected["receipt_id"]),
        )

    row = _observation(store, context, observed_at_utc)
    pending_ids = (
        row.get("last_message_id"),
        row.get("last_delivery_id"),
        row.get("last_attempt_id"),
    )
    if row.get("dispatch_state") in {"queued", "ambiguous"} and all(
        isinstance(value, str) for value in pending_ids
    ):
        return _ambiguous_without_retry(
            ids=(message_id, delivery_id, attempt_id),
            detail="a durable bb delivery has no receipt; reconcile before retry",
            row=row,
        )

    current_seq = int(row["last_event_seq"])
    _advance(
        store,
        context,
        event_seq=current_seq,
        dispatch_state="queued",
        now=observed_at_utc,
        ids=(message_id, delivery_id, attempt_id),
    )

    body = message.get("body")
    if not isinstance(body, str) or not body:
        raise BbContinuationRefused("canonical message body is empty")
    try:
        native = client.send(
            thread_id=str(context["native_thread_id"]),
            message=body,
            mode="queue-if-active",
        )
    except Exception as error:
        try:
            row = _advance(
                store,
                context,
                event_seq=current_seq,
                dispatch_state="ambiguous",
                now=observed_at_utc,
                ids=(message_id, delivery_id, attempt_id),
            )
        except Exception:
            row = {"last_event_seq": current_seq}
        return _ambiguous_without_retry(
            ids=(message_id, delivery_id, attempt_id),
            detail=f"bb send raised after the send boundary: {error}",
            row=row,
        )

    if isinstance(native, BbRefusal):
        ambiguous = native.reason in {REFUSAL_AMBIGUOUS, REFUSAL_TIMED_OUT}
        receipt_state = "ambiguous" if ambiguous else "rejected_before_acceptance"
        quality = "best_effort" if ambiguous else "authoritative"
        dispatch_state = "ambiguous" if ambiguous else "failed"
        try:
            receipt_id, _ = _append_receipt(
                store,
                context=context,
                message_id=message_id,
                delivery_id=delivery_id,
                attempt_id=attempt_id,
                registry_revision=str(materialized.get("registry_revision") or ""),
                state=receipt_state,
                quality=quality,
                correlation_id=_correlation(attempt_id, receipt_state),
                observed_at_utc=observed_at_utc,
                detail={"x_note_bb_refusal": native.reason, "x_note_detail": native.detail},
            )
        except Exception as error:
            try:
                row = _advance(
                    store,
                    context,
                    event_seq=current_seq,
                    dispatch_state="ambiguous",
                    now=observed_at_utc,
                    ids=(message_id, delivery_id, attempt_id),
                )
            except Exception:
                row = {"last_event_seq": current_seq}
            return _ambiguous_without_retry(
                ids=(message_id, delivery_id, attempt_id),
                detail=f"bb refusal was durable but its receipt was not: {error}",
                row=row,
            )
        _advance(
            store,
            context,
            event_seq=current_seq,
            dispatch_state=dispatch_state,
            now=observed_at_utc,
            ids=(message_id, delivery_id, attempt_id),
        )
        return BbContinuationResult(
            BB_CONTINUATION_AMBIGUOUS if ambiguous else BB_CONTINUATION_FAILED,
            native.detail,
            message_id=message_id,
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            receipt_id=receipt_id,
            last_event_seq=current_seq,
            native_called=True,
        )

    if not isinstance(native, BbQueued):
        try:
            row = _advance(
                store,
                context,
                event_seq=current_seq,
                dispatch_state="ambiguous",
                now=observed_at_utc,
                ids=(message_id, delivery_id, attempt_id),
            )
        except Exception:
            row = {"last_event_seq": current_seq}
        return _ambiguous_without_retry(
            ids=(message_id, delivery_id, attempt_id),
            detail="bb send returned an unrecognized result after the send boundary",
            row=row,
        )
    try:
        receipt_id, _ = _append_receipt(
            store,
            context=context,
            message_id=message_id,
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            registry_revision=str(materialized.get("registry_revision") or ""),
            state="accepted",
            quality="authoritative",
            correlation_id=_correlation(attempt_id, "accepted"),
            observed_at_utc=observed_at_utc,
            detail={"x_note_bb_mode": native.mode},
        )
    except Exception as error:
        try:
            row = _advance(
                store,
                context,
                event_seq=current_seq,
                dispatch_state="ambiguous",
                now=observed_at_utc,
                ids=(message_id, delivery_id, attempt_id),
            )
        except Exception:
            row = {"last_event_seq": current_seq}
        return _ambiguous_without_retry(
            ids=(message_id, delivery_id, attempt_id),
            detail=f"bb accepted the delivery but its receipt was not recorded: {error}",
            row=row,
        )
    _advance(
        store,
        context,
        event_seq=current_seq,
        dispatch_state="queued",
        now=observed_at_utc,
        ids=(message_id, delivery_id, attempt_id),
    )
    return BbContinuationResult(
        BB_CONTINUATION_QUEUED,
        "bb accepted queue-if-active delivery",
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        receipt_id=receipt_id,
        last_event_seq=current_seq,
        native_called=True,
    )


def _terminal_state(event: BbEvent) -> str | None:
    event_type = event.event_type.lower()
    if event_type.endswith("/completed") or event_type.endswith("/finished"):
        return "completed"
    if event_type.endswith("/failed") or event_type.endswith("/cancelled"):
        return "failed"
    return None


def _event_ids(event: BbEvent, row: Mapping[str, object]) -> tuple[str, str, str] | None:
    values = (
        event.data.get("canonical_message_id") or row.get("last_message_id"),
        event.data.get("delivery_id") or row.get("last_delivery_id"),
        event.data.get("attempt_id") or row.get("last_attempt_id"),
    )
    if not all(isinstance(value, str) for value in values):
        return None
    if not (
        _MESSAGE_ID.fullmatch(values[0])
        and _DELIVERY_ID.fullmatch(values[1])
        and _ATTEMPT_ID.fullmatch(values[2])
    ):
        return None
    return values  # type: ignore[return-value]


def observe_bb_thread(
    store: LedgerStore,
    *,
    client: BbClient,
    session: Mapping[str, object],
    observed_at_utc: str,
    registry_revision: str | None = None,
) -> BbObservationResult:
    """Replay after the committed cursor; websocket notifications never enter here."""
    context = _context(store, session)
    row = _observation(store, context, observed_at_utc)
    page = client.events_after(
        str(context["native_thread_id"]), int(row["last_event_seq"])
    )
    if isinstance(page, BbRefusal):
        return BbObservationResult(
            BB_CONTINUATION_AMBIGUOUS,
            f"bb replay refused: {page.reason}: {page.detail}",
            int(row["last_event_seq"]),
            0,
        )
    if not isinstance(page, BbEventPage):
        raise BbContinuationRefused("bb replay returned an unexpected page")
    if not page.events:
        return BbObservationResult(
            str(row["dispatch_state"]), "no events after committed cursor", int(row["last_event_seq"]), 0
        )

    terminal: tuple[BbEvent, str] | None = None
    for event in page.events:
        state = _terminal_state(event)
        if state is not None:
            terminal = (event, state)
    last_seq = page.events[-1].seq
    if terminal is None:
        next_row = _advance(
            store,
            context,
            event_seq=last_seq,
            dispatch_state=str(row["dispatch_state"]),
            now=observed_at_utc,
        )
        return BbObservationResult(
            str(next_row["dispatch_state"]),
            "replayed bb events",
            int(next_row["last_event_seq"]),
            len(page.events),
        )

    event, terminal_state = terminal
    ids = _event_ids(event, row)
    if ids is None:
        next_row = _advance(
            store,
            context,
            event_seq=last_seq,
            dispatch_state=terminal_state,
            now=observed_at_utc,
        )
        return BbObservationResult(
            terminal_state,
            "replayed terminal event without a canonical pending delivery",
            int(next_row["last_event_seq"]),
            len(page.events),
        )
    if registry_revision is None:
        registry_revision = _latest_project_registry_revision(
            store,
            workspace_id=str(context["workspace_id"]),
            project_id=str(context["project_id"]),
        )
    if not registry_revision:
        return BbObservationResult(
            BB_CONTINUATION_AMBIGUOUS,
            "terminal event is retained until canonical registry authority is available",
            int(row["last_event_seq"]),
            0,
        )
    delivery = store.read_canonical_delivery(
        workspace_id=str(context["workspace_id"]),
        scope_kind="project",
        scope_identity=str(context["project_id"]),
        message_id=ids[0],
        delivery_id=ids[1],
    )
    if delivery is None:
        raise CanonicalIntegrityError("bb terminal event references a missing delivery")
    selected = delivery.get("selected_receipt")
    can_upgrade_accepted = (
        isinstance(selected, Mapping)
        and terminal_state == "completed"
        and selected.get("state") == "accepted"
    )
    can_record_failed_accepted = (
        isinstance(selected, Mapping)
        and terminal_state == "failed"
        and selected.get("state") == "accepted"
    )
    if isinstance(selected, Mapping) and not (
        can_upgrade_accepted or can_record_failed_accepted
    ):
        next_row = _advance(
            store,
            context,
            event_seq=last_seq,
            dispatch_state=terminal_state,
            now=observed_at_utc,
            ids=ids,
        )
        return BbObservationResult(
            terminal_state,
            "terminal receipt already exists",
            int(next_row["last_event_seq"]),
            len(page.events),
            receipt_id=str(selected["receipt_id"]),
        )

    receipt_state = "completed" if terminal_state == "completed" else "ambiguous"
    quality = "authoritative" if receipt_state == "completed" else "best_effort"
    detail = {"x_note_bb_event_id": event.event_id, "x_note_bb_event_type": event.event_type}
    require_canonical_write_gate(
        store,
        workspace_id=str(context["workspace_id"]),
        scope_kind="project",
        scope_identity=str(context["project_id"]),
        registry_revision=registry_revision,
        allow_canonical_write=True,
    )
    store._connection.execute("BEGIN IMMEDIATE")
    try:
        receipt_id, _ = _append_receipt(
            store,
            context=context,
            message_id=ids[0],
            delivery_id=ids[1],
            attempt_id=ids[2],
            registry_revision=registry_revision,
            state=receipt_state,
            quality=quality,
            correlation_id=_correlation(ids[2], event.event_id),
            observed_at_utc=observed_at_utc,
            detail=detail,
            in_transaction=True,
        )
        next_row = store._advance_bb_thread_observation_locked(
            workspace_id=str(context["workspace_id"]),
            scope_kind="project",
            scope_identity=str(context["project_id"]),
            conversation_id=str(context["conversation_id"]),
            participant_id=str(context["participant_id"]),
            binding_id=str(context["binding_id"]),
            binding_generation=int(context["binding_generation"]),
            native_thread_id=str(context["native_thread_id"]),
            session_ref_id=str(context["session_ref_id"]),
            event_seq=last_seq,
            dispatch_state=terminal_state,
            updated_at_utc=observed_at_utc,
            last_message_id=ids[0],
            last_delivery_id=ids[1],
            last_attempt_id=ids[2],
        )
        store._connection.execute("COMMIT")
    except BaseException:
        if store._connection.in_transaction:
            store._connection.execute("ROLLBACK")
        raise
    return BbObservationResult(
        terminal_state,
        "replayed terminal event and recorded canonical receipt",
        int(next_row["last_event_seq"]),
        len(page.events),
        receipt_id=receipt_id,
    )


__all__ = [
    "BB_CONTINUATION_AMBIGUOUS",
    "BB_CONTINUATION_COMPLETED",
    "BB_CONTINUATION_DUPLICATE",
    "BB_CONTINUATION_FAILED",
    "BB_CONTINUATION_QUEUED",
    "BbContinuationRefused",
    "BbContinuationResult",
    "BbObservationResult",
    "client_from_project",
    "continue_bb_thread",
    "observe_bb_thread",
]
