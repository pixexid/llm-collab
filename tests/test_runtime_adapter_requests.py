from __future__ import annotations

import ast
from pathlib import Path
import unittest

from llm_collab.runtime_adapter_requests import (
    HANDSHAKE_DEADLINE_MS,
    HANDSHAKE_TIMEOUT,
    HEALTH_DEADLINE_MS,
    INVALID_DELIVERY,
    INVALID_REQUEST,
    METHOD_CANCEL,
    METHOD_DELIVER,
    METHOD_HEALTH,
    METHOD_RECONCILE,
    METHOD_SHUTDOWN,
    MAX_IN_FLIGHT_CANCEL_REQUESTS,
    MAX_IN_FLIGHT_DELIVERIES,
    MAX_IN_FLIGHT_HEALTH_REQUESTS,
    MAX_IN_FLIGHT_RECONCILE_REQUESTS,
    MAX_IN_FLIGHT_REQUESTS,
    MAX_IN_FLIGHT_SHUTDOWN_REQUESTS,
    RECONCILIATION_REQUIRED,
    REQUEST_CANCELLED,
    REQUEST_DEADLINE_MS,
    REQUEST_TIMEOUT,
    TOO_MANY_IN_FLIGHT,
    DeliveryRef,
    RequestPolicy,
    deadline_for_method,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_requests.py"


def _delivery(index: int = 1, *, session: dict[str, object] | None = None) -> DeliveryRef:
    return DeliveryRef(
        session_ref=session
        or {
            "workspace_id": "ws",
            "project_id": "amiga",
            "native_session_id": "native",
        },
        original_request_id=f"orig-{index}",
        delivery_id=f"delivery-{index}",
        attempt_id=f"attempt-{index}",
    )


class RequestCapacityTests(unittest.TestCase):
    def test_admits_exactly_twenty_eight_deliveries_plus_four_named_controls(self) -> None:
        policy = RequestPolicy()
        for index in range(MAX_IN_FLIGHT_DELIVERIES):
            delivery = _delivery(index)
            result = policy.begin_request(
                METHOD_DELIVER,
                delivery.original_request_id,
                received_at_ms=0,
                delivery=delivery,
            )
            self.assertTrue(result.accepted)
            self.assertEqual(result.deadline_ms, REQUEST_DEADLINE_MS)

        for method in (METHOD_CANCEL, METHOD_RECONCILE, METHOD_HEALTH, METHOD_SHUTDOWN):
            result = policy.begin_request(method, f"{method}-1", received_at_ms=0)
            self.assertTrue(result.accepted)

        self.assertEqual(policy.in_flight_count, MAX_IN_FLIGHT_REQUESTS)
        snapshot = policy.snapshot()
        self.assertEqual(
            len(snapshot.in_flight_by_method[METHOD_DELIVER]),
            MAX_IN_FLIGHT_DELIVERIES,
        )
        self.assertEqual(len(snapshot.in_flight_by_method[METHOD_CANCEL]), 1)
        self.assertEqual(len(snapshot.in_flight_by_method[METHOD_RECONCILE]), 1)
        self.assertEqual(len(snapshot.in_flight_by_method[METHOD_HEALTH]), 1)
        self.assertEqual(len(snapshot.in_flight_by_method[METHOD_SHUTDOWN]), 1)

    def test_delivery_cannot_borrow_an_unused_control_pool(self) -> None:
        policy = RequestPolicy()
        for index in range(MAX_IN_FLIGHT_DELIVERIES):
            delivery = _delivery(index)
            self.assertTrue(
                policy.begin_request(
                    METHOD_DELIVER,
                    delivery.original_request_id,
                    received_at_ms=0,
                    delivery=delivery,
                ).accepted
            )

        before = policy.snapshot()
        over_cap = _delivery(999)
        refused = policy.begin_request(
            METHOD_DELIVER,
            over_cap.original_request_id,
            received_at_ms=0,
            delivery=over_cap,
        )
        self.assertFalse(refused.accepted)
        self.assertEqual(refused.fault, TOO_MANY_IN_FLIGHT)
        self.assertEqual(policy.snapshot(), before)
        self.assertEqual(policy.in_flight_count, MAX_IN_FLIGHT_DELIVERIES)

    def test_invalid_delivery_metadata_leaves_no_capacity_or_pending_state(self) -> None:
        policy = RequestPolicy()
        delivery = _delivery()
        policy.begin_request(METHOD_DELIVER, delivery.original_request_id, received_at_ms=0, delivery=delivery)

        before = policy.snapshot()
        with self.assertRaises(ValueError):
            policy.begin_request(
                METHOD_DELIVER,
                "new-request-id",
                received_at_ms=0,
                delivery=delivery,
            )
        self.assertEqual(policy.snapshot(), before)
        self.assertEqual(policy.in_flight_count, 1)

        other = RequestPolicy()
        empty_before = other.snapshot()
        with self.assertRaises(ValueError):
            other.begin_request(METHOD_DELIVER, "missing-delivery", received_at_ms=0)
        self.assertEqual(other.snapshot(), empty_before)
        self.assertEqual(other.in_flight_count, 0)

        mismatched = RequestPolicy()
        mismatched_before = mismatched.snapshot()
        with self.assertRaises(ValueError):
            mismatched.begin_request(
                METHOD_DELIVER,
                "rpc-id",
                received_at_ms=0,
                delivery=_delivery(7),
            )
        self.assertEqual(mismatched.snapshot(), mismatched_before)
        self.assertEqual(mismatched.in_flight_count, 0)

    def test_each_control_pool_is_non_borrowable_and_independent(self) -> None:
        policy = RequestPolicy()
        methods = (METHOD_CANCEL, METHOD_RECONCILE, METHOD_HEALTH, METHOD_SHUTDOWN)
        for method in methods:
            self.assertTrue(policy.begin_request(method, f"{method}-1", received_at_ms=0).accepted)

        for method in methods:
            with self.subTest(method=method):
                before = policy.snapshot()
                refused = policy.begin_request(method, f"{method}-2", received_at_ms=0)
                self.assertFalse(refused.accepted)
                self.assertEqual(refused.fault, TOO_MANY_IN_FLIGHT)
                self.assertEqual(policy.snapshot(), before)

    def test_capacity_constants_are_the_protocol_partition(self) -> None:
        self.assertEqual(MAX_IN_FLIGHT_DELIVERIES, 28)
        self.assertEqual(MAX_IN_FLIGHT_CANCEL_REQUESTS, 1)
        self.assertEqual(MAX_IN_FLIGHT_RECONCILE_REQUESTS, 1)
        self.assertEqual(MAX_IN_FLIGHT_HEALTH_REQUESTS, 1)
        self.assertEqual(MAX_IN_FLIGHT_SHUTDOWN_REQUESTS, 1)
        self.assertEqual(
            MAX_IN_FLIGHT_DELIVERIES
            + MAX_IN_FLIGHT_CANCEL_REQUESTS
            + MAX_IN_FLIGHT_RECONCILE_REQUESTS
            + MAX_IN_FLIGHT_HEALTH_REQUESTS
            + MAX_IN_FLIGHT_SHUTDOWN_REQUESTS,
            MAX_IN_FLIGHT_REQUESTS,
        )


class RequestDeadlineTests(unittest.TestCase):
    def test_deadline_classification_is_pure_over_injected_now(self) -> None:
        policy = RequestPolicy()
        before = policy.classify_request_deadline(
            METHOD_DELIVER,
            "req-1",
            received_at_ms=10,
            now_ms=10 + REQUEST_DEADLINE_MS - 1,
        )
        self.assertFalse(before.expired)

        first = policy.classify_request_deadline(
            METHOD_DELIVER,
            "req-1",
            received_at_ms=10,
            now_ms=10 + REQUEST_DEADLINE_MS,
        )
        second = policy.classify_request_deadline(
            METHOD_DELIVER,
            "req-1",
            received_at_ms=10,
            now_ms=10 + REQUEST_DEADLINE_MS,
        )
        self.assertEqual(first, second)
        self.assertTrue(first.expired)
        self.assertEqual(first.fault, REQUEST_TIMEOUT)
        self.assertEqual(first.unresolved_request_id, "req-1")
        self.assertFalse(first.automatic_retry)
        self.assertIn("req-1", policy.snapshot().unresolved)

    def test_health_and_handshake_use_five_second_deadlines(self) -> None:
        policy = RequestPolicy()
        self.assertEqual(deadline_for_method(METHOD_HEALTH), HEALTH_DEADLINE_MS)
        self.assertEqual(deadline_for_method(METHOD_DELIVER), REQUEST_DEADLINE_MS)
        self.assertEqual(deadline_for_method(METHOD_CANCEL), REQUEST_DEADLINE_MS)
        self.assertEqual(deadline_for_method(METHOD_RECONCILE), REQUEST_DEADLINE_MS)
        self.assertEqual(deadline_for_method(METHOD_SHUTDOWN), REQUEST_DEADLINE_MS)

        self.assertFalse(
            policy.classify_handshake_deadline(
                process_started_at_ms=100,
                now_ms=100 + HANDSHAKE_DEADLINE_MS - 1,
            ).expired
        )
        expired = policy.classify_handshake_deadline(
            process_started_at_ms=100,
            now_ms=100 + HANDSHAKE_DEADLINE_MS,
        )
        self.assertTrue(expired.expired)
        self.assertEqual(expired.fault, HANDSHAKE_TIMEOUT)
        self.assertTrue(expired.should_close)


class RequestCancelTests(unittest.TestCase):
    def test_cancel_requires_exact_session_bound_delivery_identity(self) -> None:
        policy = RequestPolicy()
        delivery = _delivery()
        policy.begin_request(METHOD_DELIVER, delivery.original_request_id, received_at_ms=0, delivery=delivery)
        policy.begin_request(METHOD_CANCEL, "cancel-1", received_at_ms=0)

        for kwargs in (
            {"session_ref": {"workspace_id": "ws-other", "project_id": "amiga"}},
            {"delivery_id": "delivery-other"},
            {"attempt_id": "attempt-other"},
            {"original_request_id": "orig-other"},
        ):
            before = policy.snapshot()
            params = {
                "cancel_request_id": "cancel-1",
                "session_ref": delivery.session_ref,
                "original_request_id": delivery.original_request_id,
                "delivery_id": delivery.delivery_id,
                "attempt_id": delivery.attempt_id,
            }
            params.update(kwargs)
            result = policy.cancel_delivery(**params)
            self.assertFalse(result.ok)
            self.assertEqual(result.fault, INVALID_DELIVERY)
            self.assertEqual(policy.snapshot(), before)

    def test_successful_cancel_terminates_original_and_repeats_idempotently(self) -> None:
        policy = RequestPolicy()
        delivery = _delivery()
        policy.begin_request(METHOD_DELIVER, delivery.original_request_id, received_at_ms=0, delivery=delivery)
        policy.begin_request(METHOD_CANCEL, "cancel-1", received_at_ms=0)

        result = policy.cancel_delivery(
            cancel_request_id="cancel-1",
            session_ref=delivery.session_ref,
            original_request_id=delivery.original_request_id,
            delivery_id=delivery.delivery_id,
            attempt_id=delivery.attempt_id,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.original_fault, REQUEST_CANCELLED)
        self.assertTrue(result.state_advanced)
        self.assertNotIn(delivery.original_request_id, policy.snapshot().pending_deliveries)

        repeated = policy.cancel_delivery(
            cancel_request_id="cancel-1",
            session_ref=delivery.session_ref,
            original_request_id=delivery.original_request_id,
            delivery_id=delivery.delivery_id,
            attempt_id=delivery.attempt_id,
        )
        self.assertEqual(repeated, result)

    def test_uncertain_cancel_requires_reconciliation_without_retry_or_success(self) -> None:
        policy = RequestPolicy()
        delivery = _delivery()
        policy.begin_request(METHOD_DELIVER, delivery.original_request_id, received_at_ms=0, delivery=delivery)
        policy.begin_request(METHOD_CANCEL, "cancel-1", received_at_ms=0)

        result = policy.cancel_delivery(
            cancel_request_id="cancel-1",
            session_ref=delivery.session_ref,
            original_request_id=delivery.original_request_id,
            delivery_id=delivery.delivery_id,
            attempt_id=delivery.attempt_id,
            acceptance_may_have_occurred=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.fault, RECONCILIATION_REQUIRED)
        self.assertTrue(result.unresolved)
        self.assertIn(delivery.original_request_id, policy.snapshot().pending_deliveries)
        self.assertIn(delivery.original_request_id, policy.snapshot().unresolved)

    def test_cancel_action_requires_admitted_cancel_slot(self) -> None:
        policy = RequestPolicy()
        delivery = _delivery()
        policy.begin_request(METHOD_DELIVER, delivery.original_request_id, received_at_ms=0, delivery=delivery)
        result = policy.cancel_delivery(
            cancel_request_id="cancel-not-admitted",
            session_ref=delivery.session_ref,
            original_request_id=delivery.original_request_id,
            delivery_id=delivery.delivery_id,
            attempt_id=delivery.attempt_id,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.fault, INVALID_REQUEST)


class RequestScopeGuardTests(unittest.TestCase):
    def test_module_uses_no_timer_sleep_process_or_environment_primitives(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        forbidden_imports = {"time", "datetime", "random", "os", "subprocess", "threading"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(forbidden_imports & {alias.name.split(".")[0] for alias in node.names})
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], forbidden_imports)

    def test_module_has_no_forbidden_runtime_or_persistence_imports(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        forbidden = {
            "canonical",
            "ledger",
            "compatibility",
            "inbox",
            "daemon",
            "registry",
            "project_issue_queue",
            "manifest_provenance",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module or ""}
            else:
                continue
            joined = " ".join(names)
            self.assertFalse(any(part in joined for part in forbidden), joined)

    def test_no_bin_command_consumes_request_policy(self) -> None:
        for path in (ROOT / "bin").iterdir():
            if not path.is_file():
                continue
            text = path.read_text(errors="ignore")
            self.assertNotIn("runtime_adapter_requests", text, path.name)

    def test_reconcile_has_no_policy_api_or_state(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        public_names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and not node.name.startswith("_")
        }
        self.assertNotIn("reconcile_delivery", public_names)
        self.assertNotIn("ReconcileResult", public_names)


if __name__ == "__main__":
    unittest.main()
