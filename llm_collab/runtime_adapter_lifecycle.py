"""Pure lifecycle policy for Runtime Adapter JSON-RPC V1.

This module decides health cadence and shutdown timing. It intentionally does
not spawn, terminate, sleep, schedule, persist, or touch runtime/project state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping

from llm_collab.runtime_adapter_requests import (
    HEALTH_DEADLINE_MS,
    METHOD_SHUTDOWN,
)


HEALTH_INTERVAL_MS = 10_000
HEALTH_FAILURE_THRESHOLD = 3
SHUTDOWN_DRAIN_MS = 10_000
SHUTDOWN_HARD_KILL_MS = 15_000

SHUTDOWN_IN_PROGRESS = "SHUTDOWN_IN_PROGRESS"
HEALTH_TIMEOUT = "HEALTH_TIMEOUT"
INVALID_HEALTH_RESPONSE = "INVALID_HEALTH_RESPONSE"
ADAPTER_UNHEALTHY = "ADAPTER_UNHEALTHY"


@dataclass(frozen=True)
class EndpointIdentity:
    protocol_version: int
    adapter_id: str
    adapter_revision: str
    manifest_id: str
    manifest_revision: str
    profile_id: str
    endpoint_id: str
    workspace_id: str
    scope_identity: str
    capability_set_id: str
    capability_set_revision: str
    project_id: str | None = None

    def health_result(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "status": "healthy",
            "protocol_version": self.protocol_version,
            "adapter_id": self.adapter_id,
            "adapter_revision": self.adapter_revision,
            "manifest_id": self.manifest_id,
            "manifest_revision": self.manifest_revision,
            "profile_id": self.profile_id,
            "endpoint_id": self.endpoint_id,
            "workspace_id": self.workspace_id,
            "scope_identity": self.scope_identity,
            "capability_set_id": self.capability_set_id,
            "capability_set_revision": self.capability_set_revision,
        }
        if self.project_id is not None:
            payload["project_id"] = self.project_id
        return MappingProxyType(payload)


@dataclass(frozen=True)
class UnhealthyDecision:
    adapter_id: str
    adapter_revision: str
    manifest_id: str
    manifest_revision: str
    profile_id: str
    endpoint_id: str
    workspace_id: str
    scope_identity: str
    project_id: str | None
    reason: str
    decided_at_ms: int
    failure_count: int
    unresolved_attempts: tuple[str, ...]


@dataclass(frozen=True)
class LifecycleDecision:
    kind: str
    actions: tuple[str, ...] = ()
    fault: str | None = None
    next_health_due_ms: int | None = None
    drain_deadline_ms: int | None = None
    hard_kill_deadline_ms: int | None = None
    unhealthy: UnhealthyDecision | None = None
    unresolved_attempts: tuple[str, ...] = ()
    authoritative_outcome: bool = False


@dataclass(frozen=True)
class LifecycleTransition:
    state: LifecycleState
    decision: LifecycleDecision


@dataclass(frozen=True)
class HealthRequest:
    request_id: str | int | float
    dispatched_at_ms: int


@dataclass(frozen=True)
class LifecycleState:
    identity: EndpointIdentity
    next_health_due_ms: int | None
    consecutive_health_failures: int = 0
    in_flight_health: HealthRequest | None = None
    expired_health_requests: frozenset[str | int | float] = frozenset()
    possibly_accepted_attempts: tuple[str, ...] = ()
    shutdown_started_at_ms: int | None = None
    unhealthy: UnhealthyDecision | None = None

    @classmethod
    def initialized(
        cls,
        *,
        identity: EndpointIdentity,
        initialized_at_ms: int,
        consecutive_health_failures: int = 0,
        possibly_accepted_attempts: tuple[str, ...] = (),
    ) -> LifecycleState:
        _validate_milliseconds(initialized_at_ms, "initialized_at_ms")
        _validate_non_negative(consecutive_health_failures, "consecutive_health_failures")
        return cls(
            identity=identity,
            next_health_due_ms=initialized_at_ms + HEALTH_INTERVAL_MS,
            consecutive_health_failures=consecutive_health_failures,
            possibly_accepted_attempts=tuple(possibly_accepted_attempts),
        )

    def replacement_initialized(self, *, initialized_at_ms: int) -> LifecycleTransition:
        _validate_milliseconds(initialized_at_ms, "initialized_at_ms")
        if self.unhealthy is not None:
            return LifecycleTransition(
                self,
                LifecycleDecision(
                    "defer_replacement_to_recovery",
                    fault=ADAPTER_UNHEALTHY,
                    unhealthy=self.unhealthy,
                ),
            )
        state = replace(
            self,
            next_health_due_ms=initialized_at_ms + HEALTH_INTERVAL_MS,
            in_flight_health=None,
        )
        return LifecycleTransition(
            state,
            LifecycleDecision("replacement_initialized", next_health_due_ms=state.next_health_due_ms),
        )

    def begin_health(
        self,
        *,
        request_id: str | int | float,
        now_ms: int,
    ) -> LifecycleTransition:
        _validate_request_id(request_id)
        _validate_milliseconds(now_ms, "now_ms")
        if self.unhealthy is not None:
            return LifecycleTransition(
                self,
                LifecycleDecision(
                    "adapter_unhealthy",
                    fault=ADAPTER_UNHEALTHY,
                    unhealthy=self.unhealthy,
                ),
            )
        if self.shutdown_started_at_ms is not None:
            return LifecycleTransition(
                self,
                LifecycleDecision(
                    "refuse_new_work",
                    fault=SHUTDOWN_IN_PROGRESS,
                    actions=("refuse_new_work",),
                ),
            )
        if self.in_flight_health is not None:
            return LifecycleTransition(
                self,
                LifecycleDecision("health_already_in_flight"),
            )
        if self.next_health_due_ms is None or now_ms < self.next_health_due_ms:
            return LifecycleTransition(
                self,
                LifecycleDecision("health_not_due", next_health_due_ms=self.next_health_due_ms),
            )
        state = replace(
            self,
            in_flight_health=HealthRequest(request_id, now_ms),
            next_health_due_ms=None,
        )
        return LifecycleTransition(
            state,
            LifecycleDecision("dispatch_health", actions=("dispatch_health",)),
        )

    def complete_health(
        self,
        *,
        request_id: str | int | float,
        completed_at_ms: int,
        result: Mapping[str, Any],
    ) -> LifecycleTransition:
        _validate_request_id(request_id)
        _validate_milliseconds(completed_at_ms, "completed_at_ms")
        if self.unhealthy is not None:
            return LifecycleTransition(
                self,
                LifecycleDecision(
                    "adapter_unhealthy",
                    fault=ADAPTER_UNHEALTHY,
                    unhealthy=self.unhealthy,
                ),
            )
        if self.in_flight_health is None or self.in_flight_health.request_id != request_id:
            return LifecycleTransition(self, LifecycleDecision("unknown_health_request"))
        if dict(result) == dict(self.identity.health_result()):
            state = replace(
                self,
                consecutive_health_failures=0,
                in_flight_health=None,
                next_health_due_ms=completed_at_ms + HEALTH_INTERVAL_MS,
            )
            return LifecycleTransition(
                state,
                LifecycleDecision(
                    "health_ok",
                    next_health_due_ms=completed_at_ms + HEALTH_INTERVAL_MS,
                ),
            )
        return self._record_health_failure(
            reason=INVALID_HEALTH_RESPONSE,
            at_ms=completed_at_ms,
            in_flight_health=None,
            next_health_due_ms=None,
            actions=("close_connection", "terminate_process"),
        )

    def expire_health(
        self,
        *,
        request_id: str | int | float,
        now_ms: int,
    ) -> LifecycleTransition:
        _validate_request_id(request_id)
        _validate_milliseconds(now_ms, "now_ms")
        if request_id in self.expired_health_requests:
            return LifecycleTransition(self, LifecycleDecision("health_expiry_already_recorded"))
        if self.in_flight_health is None or self.in_flight_health.request_id != request_id:
            return LifecycleTransition(self, LifecycleDecision("unknown_health_request"))
        if now_ms - self.in_flight_health.dispatched_at_ms < HEALTH_DEADLINE_MS:
            return LifecycleTransition(self, LifecycleDecision("health_not_expired"))
        return self._record_health_failure(
            reason=HEALTH_TIMEOUT,
            at_ms=now_ms,
            in_flight_health=None,
            next_health_due_ms=None,
            expired_health_requests=self.expired_health_requests | frozenset((request_id,)),
            actions=("close_connection", "terminate_process"),
        )

    def begin_shutdown(self, *, now_ms: int) -> LifecycleTransition:
        _validate_milliseconds(now_ms, "now_ms")
        if self.shutdown_started_at_ms is not None:
            return LifecycleTransition(
                self,
                LifecycleDecision("shutdown_already_started", fault=SHUTDOWN_IN_PROGRESS),
            )
        state = replace(self, shutdown_started_at_ms=now_ms, next_health_due_ms=None)
        return LifecycleTransition(
            state,
            LifecycleDecision(
                "shutdown_started",
                actions=("stop_admitting_new_work",),
                drain_deadline_ms=now_ms + SHUTDOWN_DRAIN_MS,
                hard_kill_deadline_ms=now_ms + SHUTDOWN_HARD_KILL_MS,
            ),
        )

    def classify_later_work(self, *, method: str) -> LifecycleDecision:
        if self.unhealthy is not None:
            return LifecycleDecision(
                "refuse_new_work",
                fault=ADAPTER_UNHEALTHY,
                actions=("refuse_new_work",),
                unhealthy=self.unhealthy,
            )
        if self.shutdown_started_at_ms is None:
            return LifecycleDecision("admission_open")
        if method == METHOD_SHUTDOWN:
            return LifecycleDecision("defer_shutdown_capacity_to_request_policy")
        return LifecycleDecision(
            "refuse_new_work",
            fault=SHUTDOWN_IN_PROGRESS,
            actions=("refuse_new_work",),
        )

    def classify_shutdown_progress(
        self,
        *,
        now_ms: int,
        process_running: bool,
    ) -> LifecycleDecision:
        _validate_milliseconds(now_ms, "now_ms")
        if self.shutdown_started_at_ms is None:
            return LifecycleDecision("shutdown_not_started")
        drain_deadline = self.shutdown_started_at_ms + SHUTDOWN_DRAIN_MS
        hard_kill_deadline = self.shutdown_started_at_ms + SHUTDOWN_HARD_KILL_MS
        if now_ms < drain_deadline:
            return LifecycleDecision(
                "draining",
                drain_deadline_ms=drain_deadline,
                hard_kill_deadline_ms=hard_kill_deadline,
            )
        if now_ms < hard_kill_deadline:
            return LifecycleDecision(
                "drain_deadline_reached",
                actions=("continue_drain_without_outcome",),
                drain_deadline_ms=drain_deadline,
                hard_kill_deadline_ms=hard_kill_deadline,
                unresolved_attempts=self.possibly_accepted_attempts,
                authoritative_outcome=False,
            )
        if process_running:
            return LifecycleDecision(
                "hard_kill_due",
                actions=("hard_kill_process", "continue_stderr_drain"),
                drain_deadline_ms=drain_deadline,
                hard_kill_deadline_ms=hard_kill_deadline,
                unresolved_attempts=self.possibly_accepted_attempts,
                authoritative_outcome=False,
            )
        return LifecycleDecision(
            "shutdown_complete",
            drain_deadline_ms=drain_deadline,
            hard_kill_deadline_ms=hard_kill_deadline,
            authoritative_outcome=False,
        )

    def _record_health_failure(
        self,
        *,
        reason: str,
        at_ms: int,
        in_flight_health: HealthRequest | None,
        next_health_due_ms: int | None,
        actions: tuple[str, ...],
        expired_health_requests: frozenset[str | int | float] | None = None,
    ) -> LifecycleTransition:
        failure_count = self.consecutive_health_failures + 1
        unhealthy = (
            _derive_unhealthy_decision(
                self.identity,
                reason=reason,
                at_ms=at_ms,
                failure_count=failure_count,
                unresolved_attempts=self.possibly_accepted_attempts,
            )
            if failure_count >= HEALTH_FAILURE_THRESHOLD
            else None
        )
        state = replace(
            self,
            consecutive_health_failures=failure_count,
            in_flight_health=in_flight_health,
            next_health_due_ms=next_health_due_ms,
            unhealthy=self.unhealthy or unhealthy,
            expired_health_requests=(
                self.expired_health_requests
                if expired_health_requests is None
                else expired_health_requests
            ),
        )
        decision_kind = "adapter_unhealthy" if unhealthy is not None else "health_failed"
        decision_actions = actions + (("mark_unhealthy",) if unhealthy is not None else ())
        return LifecycleTransition(
            state,
            LifecycleDecision(
                decision_kind,
                actions=decision_actions,
                fault=ADAPTER_UNHEALTHY if unhealthy is not None else reason,
                unhealthy=unhealthy,
            ),
        )


def _derive_unhealthy_decision(
    identity: EndpointIdentity,
    *,
    reason: str,
    at_ms: int,
    failure_count: int,
    unresolved_attempts: tuple[str, ...],
) -> UnhealthyDecision:
    return UnhealthyDecision(
        adapter_id=identity.adapter_id,
        adapter_revision=identity.adapter_revision,
        manifest_id=identity.manifest_id,
        manifest_revision=identity.manifest_revision,
        profile_id=identity.profile_id,
        endpoint_id=identity.endpoint_id,
        workspace_id=identity.workspace_id,
        scope_identity=identity.scope_identity,
        project_id=identity.project_id,
        reason=reason,
        decided_at_ms=at_ms,
        failure_count=failure_count,
        unresolved_attempts=tuple(unresolved_attempts),
    )


def _validate_request_id(value: Any) -> None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TypeError("request id must be a non-bool string or number")


def _validate_milliseconds(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_non_negative(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
