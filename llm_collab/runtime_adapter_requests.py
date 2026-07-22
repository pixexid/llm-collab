"""Request admission policy for Runtime Adapter JSON-RPC V1.

This module is intentionally pure and in-memory. It does not spawn adapters,
schedule timers, read wall-clock time, persist state, or touch canonical,
ledger, inbox, registry, daemon, or project-state surfaces.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


METHOD_DELIVER = "runtime.deliver"
METHOD_CANCEL = "runtime.cancel"
METHOD_RECONCILE = "runtime.reconcile"
METHOD_HEALTH = "runtime.health"
METHOD_SHUTDOWN = "runtime.shutdown"

POST_INITIALIZE_METHODS = frozenset(
    (
        METHOD_DELIVER,
        METHOD_CANCEL,
        METHOD_RECONCILE,
        METHOD_HEALTH,
        METHOD_SHUTDOWN,
    )
)

MAX_IN_FLIGHT_REQUESTS = 32
MAX_IN_FLIGHT_DELIVERIES = 28
MAX_IN_FLIGHT_CANCEL_REQUESTS = 1
MAX_IN_FLIGHT_RECONCILE_REQUESTS = 1
MAX_IN_FLIGHT_HEALTH_REQUESTS = 1
MAX_IN_FLIGHT_SHUTDOWN_REQUESTS = 1

REQUEST_DEADLINE_MS = 30_000
HEALTH_DEADLINE_MS = 5_000
HANDSHAKE_DEADLINE_MS = 5_000

TOO_MANY_IN_FLIGHT = "TOO_MANY_IN_FLIGHT"
REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
HANDSHAKE_TIMEOUT = "HANDSHAKE_TIMEOUT"
REQUEST_CANCELLED = "REQUEST_CANCELLED"
INVALID_DELIVERY = "INVALID_DELIVERY"
INVALID_REQUEST = "INVALID_REQUEST"
RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


_METHOD_CAPACITIES: Mapping[str, int] = MappingProxyType(
    {
        METHOD_DELIVER: MAX_IN_FLIGHT_DELIVERIES,
        METHOD_CANCEL: MAX_IN_FLIGHT_CANCEL_REQUESTS,
        METHOD_RECONCILE: MAX_IN_FLIGHT_RECONCILE_REQUESTS,
        METHOD_HEALTH: MAX_IN_FLIGHT_HEALTH_REQUESTS,
        METHOD_SHUTDOWN: MAX_IN_FLIGHT_SHUTDOWN_REQUESTS,
    }
)


@dataclass(frozen=True)
class DeliveryRef:
    session_ref: Mapping[str, Any]
    original_request_id: str | int | float
    delivery_id: str
    attempt_id: str


@dataclass(frozen=True)
class AdmissionResult:
    accepted: bool
    method: str
    request_id: str | int | float
    fault: str | None = None
    deadline_ms: int | None = None


@dataclass(frozen=True)
class DeadlineResult:
    expired: bool
    fault: str | None = None
    should_close: bool = False
    unresolved_request_id: str | int | float | None = None
    automatic_retry: bool = False


@dataclass(frozen=True)
class CancelResult:
    ok: bool
    fault: str | None = None
    original_request_id: str | int | float | None = None
    delivery_id: str | None = None
    attempt_id: str | None = None
    status: str | None = None
    original_fault: str | None = None
    unresolved: bool = False
    state_advanced: bool = False


@dataclass(frozen=True)
class PolicySnapshot:
    in_flight_by_method: Mapping[str, tuple[str | int | float, ...]]
    pending_deliveries: Mapping[str | int | float, DeliveryRef]
    terminal_cancelled: Mapping[tuple[Any, ...], CancelResult]
    unresolved: tuple[str | int | float, ...]


class _NamedPool:
    """One method's non-borrowable capacity pool."""

    def __init__(self, method: str, capacity: int):
        self.method = method
        self.capacity = capacity
        self._request_ids: set[str | int | float] = set()

    def can_admit(self) -> bool:
        return len(self._request_ids) < self.capacity

    def admit(self, request_id: str | int | float) -> bool:
        if not self.can_admit() or request_id in self._request_ids:
            return False
        self._request_ids.add(request_id)
        return True

    def release(self, request_id: str | int | float) -> None:
        self._request_ids.discard(request_id)

    def contains(self, request_id: str | int | float) -> bool:
        return request_id in self._request_ids

    def snapshot(self) -> tuple[str | int | float, ...]:
        return tuple(sorted(self._request_ids, key=repr))


class RequestPolicy:
    """In-memory request admission, deadline, and cancellation policy."""

    def __init__(self) -> None:
        self._pools = {
            method: _NamedPool(method, capacity)
            for method, capacity in _METHOD_CAPACITIES.items()
        }
        self._pending_deliveries: dict[str | int | float, DeliveryRef] = {}
        self._terminal_cancelled: dict[tuple[Any, ...], CancelResult] = {}
        self._unresolved: set[str | int | float] = set()

    def begin_request(
        self,
        method: str,
        request_id: str | int | float,
        *,
        received_at_ms: int,
        delivery: DeliveryRef | None = None,
    ) -> AdmissionResult:
        _validate_request_id(request_id)
        _validate_milliseconds(received_at_ms, "received_at_ms")
        pool = self._pool_for(method)
        validated_delivery = self._validated_delivery(method, request_id, delivery)
        before = self.snapshot()
        if not pool.admit(request_id):
            self._restore_required_unchanged(before)
            return AdmissionResult(False, method, request_id, fault=TOO_MANY_IN_FLIGHT)
        if validated_delivery is not None:
            self._pending_deliveries[validated_delivery.original_request_id] = validated_delivery
        return AdmissionResult(
            True,
            method,
            request_id,
            deadline_ms=deadline_for_method(method),
        )

    def complete_request(self, method: str, request_id: str | int | float) -> None:
        self._pool_for(method).release(request_id)

    def classify_request_deadline(
        self,
        method: str,
        request_id: str | int | float,
        *,
        received_at_ms: int,
        now_ms: int,
    ) -> DeadlineResult:
        _validate_request_id(request_id)
        _validate_milliseconds(received_at_ms, "received_at_ms")
        _validate_milliseconds(now_ms, "now_ms")
        self._pool_for(method)
        if now_ms - received_at_ms < deadline_for_method(method):
            return DeadlineResult(False)
        self._unresolved.add(request_id)
        return DeadlineResult(
            True,
            fault=REQUEST_TIMEOUT,
            unresolved_request_id=request_id,
            automatic_retry=False,
        )

    def classify_handshake_deadline(
        self,
        *,
        process_started_at_ms: int,
        now_ms: int,
    ) -> DeadlineResult:
        _validate_milliseconds(process_started_at_ms, "process_started_at_ms")
        _validate_milliseconds(now_ms, "now_ms")
        if now_ms - process_started_at_ms < HANDSHAKE_DEADLINE_MS:
            return DeadlineResult(False)
        return DeadlineResult(True, fault=HANDSHAKE_TIMEOUT, should_close=True)

    def cancel_delivery(
        self,
        *,
        cancel_request_id: str | int | float,
        session_ref: Mapping[str, Any],
        original_request_id: str | int | float,
        delivery_id: str,
        attempt_id: str,
        acceptance_may_have_occurred: bool = False,
    ) -> CancelResult:
        _validate_request_id(cancel_request_id)
        _validate_request_id(original_request_id)
        if not self._pools[METHOD_CANCEL].contains(cancel_request_id):
            return CancelResult(False, fault=INVALID_REQUEST)
        key = _cancel_key(session_ref, original_request_id, delivery_id, attempt_id)
        if key in self._terminal_cancelled:
            return self._terminal_cancelled[key]
        pending = self._pending_deliveries.get(original_request_id)
        before = self.snapshot()
        if pending is None or key != _cancel_key(
            pending.session_ref,
            pending.original_request_id,
            pending.delivery_id,
            pending.attempt_id,
        ):
            self._restore_required_unchanged(before)
            return CancelResult(False, fault=INVALID_DELIVERY)
        if acceptance_may_have_occurred:
            self._unresolved.add(original_request_id)
            return CancelResult(
                False,
                fault=RECONCILIATION_REQUIRED,
                original_request_id=original_request_id,
                delivery_id=delivery_id,
                attempt_id=attempt_id,
                unresolved=True,
            )
        result = CancelResult(
            True,
            original_request_id=original_request_id,
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            status="cancelled",
            original_fault=REQUEST_CANCELLED,
            state_advanced=True,
        )
        del self._pending_deliveries[original_request_id]
        self._terminal_cancelled[key] = result
        return result

    @property
    def in_flight_count(self) -> int:
        return sum(len(pool.snapshot()) for pool in self._pools.values())

    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(
            in_flight_by_method=MappingProxyType(
                {method: pool.snapshot() for method, pool in self._pools.items()}
            ),
            pending_deliveries=MappingProxyType(dict(self._pending_deliveries)),
            terminal_cancelled=MappingProxyType(dict(self._terminal_cancelled)),
            unresolved=tuple(sorted(self._unresolved, key=repr)),
        )

    def _pool_for(self, method: str) -> _NamedPool:
        if method not in self._pools:
            raise ValueError(f"unknown post-initialize method: {method}")
        return self._pools[method]

    def _record_delivery(self, delivery: DeliveryRef) -> None:
        self._pending_deliveries[delivery.original_request_id] = self._validated_delivery(
            METHOD_DELIVER,
            delivery.original_request_id,
            delivery,
        )

    def _validated_delivery(
        self,
        method: str,
        request_id: str | int | float,
        delivery: DeliveryRef | None,
    ) -> DeliveryRef | None:
        if method != METHOD_DELIVER:
            if delivery is not None:
                raise ValueError("only runtime.deliver may carry delivery metadata")
            return None
        if delivery is None:
            raise ValueError("runtime.deliver requires delivery metadata")
        _validate_request_id(delivery.original_request_id)
        if delivery.original_request_id != request_id:
            raise ValueError("delivery original_request_id must equal the JSON-RPC request id")
        if delivery.original_request_id in self._pending_deliveries:
            raise ValueError("delivery request id is already pending")
        return DeliveryRef(
            session_ref=MappingProxyType(copy.deepcopy(dict(delivery.session_ref))),
            original_request_id=delivery.original_request_id,
            delivery_id=_non_empty_string(delivery.delivery_id, "delivery_id"),
            attempt_id=_non_empty_string(delivery.attempt_id, "attempt_id"),
        )

    def _restore_required_unchanged(self, before: PolicySnapshot) -> None:
        if self.snapshot() != before:
            raise AssertionError("request refusal mutated policy state")


def deadline_for_method(method: str) -> int:
    if method == METHOD_HEALTH:
        return HEALTH_DEADLINE_MS
    if method in POST_INITIALIZE_METHODS:
        return REQUEST_DEADLINE_MS
    raise ValueError(f"unknown post-initialize method: {method}")


def _cancel_key(
    session_ref: Mapping[str, Any],
    original_request_id: str | int | float,
    delivery_id: str,
    attempt_id: str,
) -> tuple[Any, ...]:
    return (
        _freeze_mapping(session_ref),
        original_request_id,
        _non_empty_string(delivery_id, "delivery_id"),
        _non_empty_string(attempt_id, "attempt_id"),
    )


def _freeze_mapping(value: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, Mapping):
        raise TypeError("session_ref must be a mapping")
    return tuple(sorted((key, _freeze_value(item)) for key, item in value.items()))


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError("session_ref contains unsupported value")


def _validate_request_id(value: Any) -> None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TypeError("request id must be a non-bool string or number")


def _validate_milliseconds(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value
