from __future__ import annotations

import ast
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_lifecycle import (
    ADAPTER_UNHEALTHY,
    HEALTH_FAILURE_THRESHOLD,
    HEALTH_INTERVAL_MS,
    INVALID_HEALTH_RESPONSE,
    SHUTDOWN_DRAIN_MS,
    SHUTDOWN_HARD_KILL_MS,
    SHUTDOWN_IN_PROGRESS,
    EndpointIdentity,
    HealthRequest,
    LifecycleState,
)
from llm_collab.runtime_adapter_requests import HEALTH_DEADLINE_MS, METHOD_SHUTDOWN


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_lifecycle.py"
REQUESTS_PATH = ROOT / "llm_collab" / "runtime_adapter_requests.py"


def identity(**overrides: object) -> EndpointIdentity:
    values: dict[str, object] = {
        "protocol_version": 1,
        "adapter_id": "adapter_a",
        "adapter_revision": "adapter_rev_1",
        "manifest_id": "manifest_a",
        "manifest_revision": "manifest_rev_1",
        "profile_id": "profile_a",
        "endpoint_id": "endpoint_a",
        "workspace_id": "ws_alpha",
        "project_id": "amiga",
        "scope_identity": "workspace:ws_alpha|project:amiga",
        "capability_set_id": "caps_a",
        "capability_set_revision": "caps_rev_1",
    }
    values.update(overrides)
    return EndpointIdentity(**values)


def initialized(at: int = 1_000, **overrides: object) -> LifecycleState:
    return LifecycleState.initialized(
        identity=identity(),
        initialized_at_ms=at,
        **overrides,
    )


def dispatch_health(state: LifecycleState, request_id: str = "health-1", at: int = 11_000):
    transition = state.begin_health(request_id=request_id, now_ms=at)
    return transition.state


class HealthCadenceTests(unittest.TestCase):
    def test_lifecycle_protocol_constants_are_exact(self) -> None:
        self.assertEqual(HEALTH_INTERVAL_MS, 10_000)
        self.assertEqual(HEALTH_FAILURE_THRESHOLD, 3)
        self.assertEqual(SHUTDOWN_DRAIN_MS, 10_000)
        self.assertEqual(SHUTDOWN_HARD_KILL_MS, 15_000)
        self.assertLess(HEALTH_DEADLINE_MS, HEALTH_INTERVAL_MS)

    def test_first_health_is_due_interval_after_successful_initialization(self) -> None:
        state = initialized(at=200)

        self.assertEqual(state.next_health_due_ms, 200 + HEALTH_INTERVAL_MS)
        not_due = state.begin_health(request_id="health-1", now_ms=200 + HEALTH_INTERVAL_MS - 1)
        self.assertEqual(not_due.decision.kind, "health_not_due")
        due = state.begin_health(request_id="health-1", now_ms=200 + HEALTH_INTERVAL_MS)
        self.assertEqual(due.decision.kind, "dispatch_health")
        self.assertEqual(due.decision.actions, ("dispatch_health",))
        self.assertIsNone(due.state.next_health_due_ms)

    def test_later_health_cadence_is_anchored_to_completion_not_dispatch(self) -> None:
        state = initialized(at=1_000)
        in_flight = state.begin_health(request_id="health-1", now_ms=11_000).state

        completed = in_flight.complete_health(
            request_id="health-1",
            completed_at_ms=14_000,
            result=identity().health_result(),
        )

        self.assertEqual(completed.decision.kind, "health_ok")
        self.assertEqual(completed.state.next_health_due_ms, 14_000 + HEALTH_INTERVAL_MS)
        self.assertNotEqual(completed.state.next_health_due_ms, 11_000 + HEALTH_INTERVAL_MS)

    def test_health_timeout_ends_epoch_without_successor_or_catchup(self) -> None:
        state = dispatch_health(initialized(at=0), at=HEALTH_INTERVAL_MS)

        expired = state.expire_health(
            request_id="health-1",
            now_ms=HEALTH_INTERVAL_MS + HEALTH_DEADLINE_MS,
        )

        self.assertEqual(expired.decision.kind, "health_failed")
        self.assertEqual(expired.decision.actions, ("close_connection", "terminate_process"))
        self.assertIsNone(expired.state.next_health_due_ms)
        self.assertIsNone(expired.state.in_flight_health)

        replacement = expired.state.replacement_initialized(initialized_at_ms=99_000)
        self.assertEqual(replacement.decision.kind, "replacement_initialized")
        self.assertEqual(replacement.state.next_health_due_ms, 99_000 + HEALTH_INTERVAL_MS)

    def test_overlapping_health_request_is_structurally_not_dispatched(self) -> None:
        state = dispatch_health(initialized())

        overlap = state.begin_health(request_id="health-2", now_ms=99_000)

        self.assertEqual(overlap.decision.kind, "health_already_in_flight")
        self.assertEqual(overlap.state, state)


class HealthFailureTests(unittest.TestCase):
    def test_success_resets_consecutive_health_failures(self) -> None:
        state = initialized(consecutive_health_failures=2)
        in_flight = dispatch_health(state)

        ok = in_flight.complete_health(
            request_id="health-1",
            completed_at_ms=12_000,
            result=identity().health_result(),
        )

        self.assertEqual(ok.state.consecutive_health_failures, 0)
        self.assertEqual(ok.decision.kind, "health_ok")

    def test_malformed_or_mismatched_health_response_counts_once(self) -> None:
        state = dispatch_health(initialized())

        failed = state.complete_health(
            request_id="health-1",
            completed_at_ms=12_000,
            result={"status": "healthy", "adapter_id": "other"},
        )

        self.assertEqual(failed.decision.kind, "health_failed")
        self.assertEqual(failed.decision.fault, INVALID_HEALTH_RESPONSE)
        self.assertEqual(failed.state.consecutive_health_failures, 1)
        repeated = failed.state.complete_health(
            request_id="health-1",
            completed_at_ms=12_001,
            result={"status": "healthy", "adapter_id": "other"},
        )
        self.assertEqual(repeated.decision.kind, "unknown_health_request")
        self.assertEqual(repeated.state.consecutive_health_failures, 1)

    def test_expiry_is_counted_once_not_on_reobservation_or_elapsed_time(self) -> None:
        state = dispatch_health(initialized(), request_id="health-1")

        before = state.expire_health(request_id="health-1", now_ms=11_000 + HEALTH_DEADLINE_MS - 1)
        self.assertEqual(before.decision.kind, "health_not_expired")
        self.assertEqual(before.state.consecutive_health_failures, 0)

        expired = state.expire_health(request_id="health-1", now_ms=11_000 + HEALTH_DEADLINE_MS)
        self.assertEqual(expired.state.consecutive_health_failures, 1)

        repeated = expired.state.expire_health(
            request_id="health-1",
            now_ms=11_000 + HEALTH_DEADLINE_MS + 10_000,
        )
        self.assertEqual(repeated.decision.kind, "health_expiry_already_recorded")
        self.assertEqual(repeated.state.consecutive_health_failures, 1)

    def test_two_literal_failures_do_not_derive_unhealthy_decision(self) -> None:
        state = initialized(consecutive_health_failures=1)
        in_flight = dispatch_health(state)

        failed = in_flight.expire_health(
            request_id="health-1",
            now_ms=11_000 + HEALTH_DEADLINE_MS,
        )

        self.assertEqual(failed.decision.kind, "health_failed")
        self.assertIsNone(failed.decision.unhealthy)
        self.assertIsNone(failed.state.unhealthy)
        self.assertEqual(failed.state.consecutive_health_failures, 2)

    def test_third_literal_failure_derives_sticky_unhealthy_decision(self) -> None:
        state = initialized(
            consecutive_health_failures=2,
            possibly_accepted_attempts=("attempt-1", "attempt-2"),
        )
        in_flight = dispatch_health(state)

        unhealthy = in_flight.expire_health(
            request_id="health-1",
            now_ms=11_000 + HEALTH_DEADLINE_MS,
        )

        self.assertEqual(unhealthy.decision.kind, "adapter_unhealthy")
        self.assertEqual(unhealthy.decision.fault, ADAPTER_UNHEALTHY)
        self.assertIsNotNone(unhealthy.decision.unhealthy)
        self.assertEqual(unhealthy.state.unhealthy, unhealthy.decision.unhealthy)
        decision = unhealthy.decision.unhealthy
        assert decision is not None
        self.assertEqual(decision.adapter_id, "adapter_a")
        self.assertEqual(decision.manifest_id, "manifest_a")
        self.assertEqual(decision.profile_id, "profile_a")
        self.assertEqual(decision.endpoint_id, "endpoint_a")
        self.assertEqual(decision.workspace_id, "ws_alpha")
        self.assertEqual(decision.project_id, "amiga")
        self.assertEqual(decision.failure_count, 3)
        self.assertEqual(decision.unresolved_attempts, ("attempt-1", "attempt-2"))

    def test_unhealthy_state_does_not_auto_clear_or_admit_normal_replacement(self) -> None:
        state = initialized(consecutive_health_failures=2)
        unhealthy = dispatch_health(state).expire_health(
            request_id="health-1",
            now_ms=11_000 + HEALTH_DEADLINE_MS,
        ).state

        replacement = unhealthy.replacement_initialized(initialized_at_ms=99_000)
        self.assertEqual(replacement.decision.kind, "defer_replacement_to_recovery")
        self.assertEqual(replacement.decision.fault, ADAPTER_UNHEALTHY)
        self.assertEqual(replacement.state, unhealthy)

        refused = unhealthy.classify_later_work(method="runtime.deliver")
        self.assertEqual(refused.kind, "refuse_new_work")
        self.assertEqual(refused.fault, ADAPTER_UNHEALTHY)

        injected = replace_for_test(
            unhealthy,
            in_flight_health=HealthRequest("health-2", 12_000),
            next_health_due_ms=None,
        )
        ok = injected.complete_health(
            request_id="health-2",
            completed_at_ms=12_000,
            result=identity().health_result(),
        )
        self.assertEqual(ok.decision.kind, "adapter_unhealthy")
        self.assertEqual(ok.state.unhealthy, unhealthy.unhealthy)


class ShutdownLifecycleTests(unittest.TestCase):
    def test_shutdown_stops_later_admission_and_sets_distinct_deadlines(self) -> None:
        transition = initialized().begin_shutdown(now_ms=5_000)

        self.assertEqual(transition.decision.kind, "shutdown_started")
        self.assertEqual(transition.decision.actions, ("stop_admitting_new_work",))
        self.assertEqual(transition.decision.drain_deadline_ms, 5_000 + SHUTDOWN_DRAIN_MS)
        self.assertEqual(transition.decision.hard_kill_deadline_ms, 5_000 + SHUTDOWN_HARD_KILL_MS)

        refused = transition.state.classify_later_work(method="runtime.deliver")
        self.assertEqual(refused.kind, "refuse_new_work")
        self.assertEqual(refused.fault, SHUTDOWN_IN_PROGRESS)

        second_shutdown = transition.state.classify_later_work(method=METHOD_SHUTDOWN)
        self.assertEqual(second_shutdown.kind, "defer_shutdown_capacity_to_request_policy")

    def test_shutdown_drain_does_not_create_authoritative_outcomes(self) -> None:
        state = initialized(possibly_accepted_attempts=("attempt-1",)).begin_shutdown(now_ms=1_000).state

        draining = state.classify_shutdown_progress(now_ms=1_000 + SHUTDOWN_DRAIN_MS - 1, process_running=True)
        self.assertEqual(draining.kind, "draining")
        self.assertFalse(draining.authoritative_outcome)

        drained = state.classify_shutdown_progress(now_ms=1_000 + SHUTDOWN_DRAIN_MS, process_running=True)
        self.assertEqual(drained.kind, "drain_deadline_reached")
        self.assertEqual(drained.actions, ("continue_drain_without_outcome",))
        self.assertEqual(drained.unresolved_attempts, ("attempt-1",))
        self.assertFalse(drained.authoritative_outcome)

    def test_hard_kill_is_later_than_drain_and_still_not_authoritative(self) -> None:
        state = initialized(possibly_accepted_attempts=("attempt-1",)).begin_shutdown(now_ms=1_000).state

        before_hard_kill = state.classify_shutdown_progress(
            now_ms=1_000 + SHUTDOWN_HARD_KILL_MS - 1,
            process_running=True,
        )
        self.assertEqual(before_hard_kill.kind, "drain_deadline_reached")

        hard_kill = state.classify_shutdown_progress(
            now_ms=1_000 + SHUTDOWN_HARD_KILL_MS,
            process_running=True,
        )
        self.assertEqual(hard_kill.kind, "hard_kill_due")
        self.assertEqual(hard_kill.actions, ("hard_kill_process", "continue_stderr_drain"))
        self.assertEqual(hard_kill.unresolved_attempts, ("attempt-1",))
        self.assertFalse(hard_kill.authoritative_outcome)


class LifecycleScopeGuardTests(unittest.TestCase):
    def test_lifecycle_imports_request_constants_one_way_only(self) -> None:
        lifecycle_tree = ast.parse(MODULE_PATH.read_text())
        request_tree = ast.parse(REQUESTS_PATH.read_text())

        self.assertTrue(imports_module(lifecycle_tree, "llm_collab.runtime_adapter_requests"))
        self.assertFalse(imports_module(request_tree, "llm_collab.runtime_adapter_lifecycle"))

    def test_lifecycle_module_uses_no_process_timer_or_persistence_imports(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        forbidden_imports = {
            "time",
            "datetime",
            "threading",
            "subprocess",
            "os",
            "sqlite3",
        }
        forbidden_calls = {
            "sleep",
            "Timer",
            "Thread",
            "Popen",
            "run",
            "terminate",
            "kill",
        }
        forbidden_llm_collab = {
            "canonical",
            "ledger",
            "compatibility",
            "inbox",
            "daemon",
            "registry",
            "project_issue_queue",
            "runtime_adapter_supervisor",
            "manifest_provenance",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    self.assertNotIn(parts[0], forbidden_imports)
                    if parts[0] == "llm_collab":
                        self.assertFalse(set(parts) & forbidden_llm_collab)
            if isinstance(node, ast.ImportFrom):
                parts = (node.module or "").split(".")
                if parts:
                    self.assertNotIn(parts[0], forbidden_imports)
                if parts and parts[0] == "llm_collab":
                    self.assertFalse(set(parts) & forbidden_llm_collab)
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".", 1)[0], forbidden_llm_collab)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    self.assertNotIn(func.id, forbidden_calls)
                if isinstance(func, ast.Attribute):
                    self.assertNotIn(func.attr, forbidden_calls)

    def test_no_bin_consumer_imports_lifecycle_module(self) -> None:
        for path in (ROOT / "bin").glob("*.py"):
            tree = ast.parse(path.read_text())
            self.assertFalse(imports_module(tree, "llm_collab.runtime_adapter_lifecycle"), path)


def imports_module(tree: ast.AST, module: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom):
            if node.module == module:
                return True
            if node.module == "llm_collab":
                wanted = module.rsplit(".", 1)[-1]
                if any(alias.name == wanted for alias in node.names):
                    return True
    return False


def replace_for_test(state: LifecycleState, **changes: object) -> LifecycleState:
    return LifecycleState(
        identity=state.identity,
        next_health_due_ms=changes.get("next_health_due_ms", state.next_health_due_ms),
        consecutive_health_failures=state.consecutive_health_failures,
        in_flight_health=changes.get("in_flight_health", state.in_flight_health),
        expired_health_requests=state.expired_health_requests,
        possibly_accepted_attempts=state.possibly_accepted_attempts,
        shutdown_started_at_ms=state.shutdown_started_at_ms,
        unhealthy=state.unhealthy,
    )


if __name__ == "__main__":
    unittest.main()
