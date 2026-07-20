"""Mutation-sensitive guards for the inert runtime-adapter V1 contract."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
PROJECTS = ("amiga", "nuvyr")


def normalized(text: str) -> str:
    return " ".join(text.split())


def contract_invariants(text: str) -> dict[str, bool]:
    compact = normalized(text)
    cancel_shape = normalized(
        text.split("- `runtime.cancel` params", 1)[1].split(
            "- `runtime.reconcile` params",
            1,
        )[0]
    )
    deadline_values = tuple(
        int(value.replace(",", ""))
        for value in re.findall(
            r"`HEALTH_DEADLINE_MS`, fixed at ([\d,]+) milliseconds",
            compact,
        )
    )
    interval_values = tuple(
        int(value.replace(",", ""))
        for value in re.findall(
            r"`HEALTH_INTERVAL_MS`, fixed at ([\d,]+) milliseconds",
            compact,
        )
    )
    return {
        "cancel_params": (
            "`runtime.cancel` params contain exactly `session_ref`, "
            "`original_request_id`, `delivery_id`, and `attempt_id`"
        )
        in compact,
        "cancel_s2_scalars": (
            "the last two values use the exact S2 `DeliveryV1.delivery_id` and "
            "`DeliveryV1.attempt_id` scalar definitions"
        )
        in cancel_shape,
        "cancel_result": (
            "Its success result contains exactly `original_request_id`, "
            "`delivery_id`, `attempt_id`, and `status`"
        )
        in compact,
        "cancel_result_shape_equality": (
            "The first three members equal their params members in JSON type "
            "and value"
        )
        in cancel_shape,
        "cancel_normative_result_equality": (
            "all three identity members MUST equal their params members in "
            "JSON type and value"
        )
        in compact,
        "cancel_match": (
            "MUST refuse cancellation unless both its complete `SessionRefV1` "
            "exactly equals the original request's recorded session and the "
            "complete "
            "`(original_request_id, delivery_id, attempt_id)` triple exactly "
            "matches the recorded original delivery and attempt"
        )
        in compact,
        "cancel_mismatch": (
            "A session or triple mismatch is `INVALID_DELIVERY` at P7, performs "
            "no action, and advances no state"
        )
        in compact,
        "cancel_idempotence": (
            "Cancellation is idempotent only for the same exact `SessionRefV1` "
            "and complete matching "
            "`(original_request_id, delivery_id, attempt_id)` triple"
        )
        in compact,
        "health_deadline_values": deadline_values == (5_000, 5_000),
        "health_interval_value": interval_values == (10_000,),
        "health_deadline_below_interval": (
            bool(deadline_values)
            and bool(interval_values)
            and max(deadline_values) < interval_values[0]
        ),
        "health_scope": (
            "`runtime.health` alone uses `HEALTH_DEADLINE_MS`, fixed at 5,000 "
            "milliseconds"
        )
        in compact,
        "health_anchor": (
            "after the previous health request completes, never from dispatch. "
            "A request that reaches its deadline instead ends that connection's "
            "cadence epoch; no successor is scheduled from its expiry"
        )
        in compact,
        "health_no_overlap": (
            "This completion-anchored cadence on a live initialized connection, "
            "the timeout-ended cadence epoch, replacement-initialization re-anchor, "
            "and the one-request bound make an overlapping health request "
            "structurally impossible for a conforming host"
        )
        in compact,
        "health_no_stale_overlap_rationale": (
            "completion- or deadline-expiry-anchored cadence" not in compact
        ),
        "health_no_forced_miss": (
            "A forced miss from overlap therefore cannot occur and is not a "
            "health-failure category"
        )
        in compact,
        "health_expiry_teardown": (
            "the host MUST record exactly one health failure at expiry, close "
            "that connection, terminate the old adapter process, and confirm "
            "its exit before initializing any replacement permitted by the "
            "current adapter-state gate"
        )
        in compact,
        "health_replacement_admission": (
            "The timeout failure is recorded before replacement admission; "
            "when it reaches `HEALTH_FAILURE_THRESHOLD`, only Clause 12's "
            "explicitly authorized recovery route can admit a replacement"
        )
        in compact,
        "health_fresh_replacement": (
            "the new initialized connection begins a fresh cadence epoch: its "
            "first `runtime.health` is dispatched exactly `HEALTH_INTERVAL_MS` "
            "after that successful replacement initialization"
        )
        in compact,
        "health_no_expiry_catch_up": (
            "The expired connection has no successor or expiry-anchored slot "
            "to catch up, and no immediate catch-up dispatch occurs after a "
            "slow initialization"
        )
        in compact,
        "health_latency_is_neutral": (
            "Teardown duration, replacement-initialization duration, and the "
            "absence of a health call while no initialized connection exists "
            "are not additional health failures and do not create a missed or "
            "overlapping health request"
        )
        in compact,
        "health_failure_continuity": (
            "The endpoint's consecutive health-failure count survives that "
            "connection and process replacement"
        )
        in compact,
        "health_recovery_reanchor": (
            "The same re-anchored first-health rule applies when Clause 12 "
            "admits and successfully initializes a recovery connection after "
            "the threshold failure"
        )
        in compact,
        "health_threshold": (
            "A health request that exceeds `HEALTH_DEADLINE_MS` counts as one "
            "failure. A malformed response, including any response without "
            'the exact `"healthy"` status, counts as one failure; an '
            "identity-mismatched or revision-mismatched response counts as "
            "one failure. These are the exhaustive health-failure categories. "
            "A successful health response resets the consecutive-failure "
            "count to zero. Three consecutive failures "
            "(`HEALTH_FAILURE_THRESHOLD = 3`)"
        )
        in compact,
    }


def evaluate_case(text: str, case: dict[str, object]) -> object:
    """Evaluate one project-labelled case without project-specific behavior."""
    invariants = contract_invariants(text)
    if not all(invariants.values()):
        return "invalid_contract"
    scenario = case["scenario"]
    if scenario == "cancel":
        recorded = tuple(case["recorded_identity"])
        requested = tuple(case["requested_identity"])
        return "cancelled" if requested == recorded else "refused_invalid_delivery"
    if scenario == "health_boundaries":
        interval = int(case["interval_ms"])
        anchors = tuple(case["completion_anchors_ms"])
        return tuple(anchor + interval for anchor in anchors)
    if scenario == "health_timeout_replacement":
        initialization = int(case["replacement_initialization_ms"])
        return initialization + int(case["interval_ms"])
    raise AssertionError(f"unknown scenario: {scenario}")


def paired_cases() -> tuple[dict[str, object], ...]:
    templates = (
        {
            "scenario": "cancel",
            "recorded_identity": (
                "session-a",
                "request-reused",
                "delivery-1",
                "attempt-1",
            ),
            "requested_identity": (
                "session-a",
                "request-reused",
                "delivery-1",
                "attempt-1",
            ),
            "expected": "cancelled",
        },
        {
            "scenario": "cancel",
            "recorded_identity": (
                "session-a",
                "request-reused",
                "delivery-1",
                "attempt-1",
            ),
            "requested_identity": (
                "session-a",
                "request-reused",
                "delivery-2",
                "attempt-1",
            ),
            "expected": "refused_invalid_delivery",
        },
        {
            "scenario": "cancel",
            "recorded_identity": (
                "session-a",
                "request-reused",
                "delivery-1",
                "attempt-1",
            ),
            "requested_identity": (
                "session-b",
                "request-reused",
                "delivery-1",
                "attempt-1",
            ),
            "expected": "refused_invalid_delivery",
        },
        {
            "scenario": "health_boundaries",
            "interval_ms": 10_000,
            "completion_anchors_ms": (0, 10_000, 20_000),
            "expected": (10_000, 20_000, 30_000),
        },
        {
            "scenario": "health_timeout_replacement",
            "expired_at_ms": 5_000,
            "replacement_initialization_ms": 23_000,
            "interval_ms": 10_000,
            "expected": 33_000,
        },
    )
    return tuple(
        {"project_id": project_id, **template}
        for project_id in PROJECTS
        for template in templates
    )


class RuntimeAdapterJsonRpcV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_contract_invariants_are_complete(self) -> None:
        self.assertEqual(
            [
                name
                for name, present in contract_invariants(self.text).items()
                if not present
            ],
            [],
        )

    def test_project_cases_are_complete_universal_and_case_driven(self) -> None:
        cases = paired_cases()
        scenario_counts = {
            project_id: sum(case["project_id"] == project_id for case in cases)
            for project_id in PROJECTS
        }
        self.assertEqual(len(set(scenario_counts.values())), 1)
        self.assertGreater(next(iter(scenario_counts.values())), 0)

        outcomes: dict[tuple[object, ...], dict[str, object]] = {}
        for case in cases:
            project_id = str(case["project_id"])
            result = evaluate_case(self.text, case)
            self.assertEqual(result, case["expected"], case)
            identity = tuple(
                (key, repr(value))
                for key, value in sorted(case.items())
                if key not in {"project_id", "expected"}
            )
            outcomes.setdefault(identity, {})[project_id] = result

        for project_results in outcomes.values():
            self.assertEqual(set(project_results), set(PROJECTS))
            self.assertEqual(len(set(project_results.values())), 1)

    def test_guard_reads_only_the_protocol_not_live_registry(self) -> None:
        reads: list[Path] = []
        original_read_text = Path.read_text

        def guarded_read_text(path: Path, *args, **kwargs) -> str:
            reads.append(path)
            self.assertNotEqual(path.name, "projects.json")
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded_read_text):
            text = PROTOCOL_PATH.read_text(encoding="utf-8")
            for case in paired_cases():
                evaluate_case(text, case)
        self.assertEqual(reads, [PROTOCOL_PATH])

    def test_frozen_mutations_fail(self) -> None:
        mutations = (
            (
                "cancel params reverted",
                self.text.replace(
                    "`runtime.cancel` params contain exactly `session_ref`,\n"
                    "  `original_request_id`, `delivery_id`, and `attempt_id`",
                    "`runtime.cancel` params contain exactly `session_ref` and\n"
                    "  `original_request_id`",
                    1,
                ),
            ),
            (
                "triple match removed",
                self.text.replace(
                    "adapter MUST refuse cancellation unless both its complete "
                    "`SessionRefV1`",
                    "adapter MAY accept cancellation without checking its "
                    "`SessionRefV1`",
                    1,
                ),
            ),
            (
                "clause 3 deadline raised",
                self.text.replace(
                    "`HEALTH_DEADLINE_MS`, fixed at 5,000\n"
                    "   milliseconds from receipt",
                    "`HEALTH_DEADLINE_MS`, fixed at 10,000\n"
                    "   milliseconds from receipt",
                    1,
                ),
            ),
            (
                "ordinary cadence reanchored",
                self.text.replace(
                    "never\n    from dispatch",
                    "from dispatch",
                    1,
                ),
            ),
            (
                "forced miss counted",
                self.text.replace(
                    "cannot occur and is not a health-failure category",
                    "counts as one failed response",
                    1,
                ),
            ),
            (
                "clause 11 deadline raised",
                self.text.replace(
                    "inside `HEALTH_DEADLINE_MS`, fixed at\n"
                    "    5,000 milliseconds for `runtime.health` only",
                    "inside `HEALTH_DEADLINE_MS`, fixed at\n"
                    "    10,000 milliseconds for `runtime.health` only",
                    1,
                ),
            ),
            (
                "health interval changed",
                self.text.replace(
                    "`HEALTH_INTERVAL_MS`, fixed at 10,000 milliseconds",
                    "`HEALTH_INTERVAL_MS`, fixed at 20,000 milliseconds",
                    1,
                ),
            ),
            (
                "S2 scalar binding removed",
                self.text.replace(
                    "the last two values use the\n"
                    "  exact S2 `DeliveryV1.delivery_id` and "
                    "`DeliveryV1.attempt_id` scalar\n"
                    "  definitions",
                    "the last two values are arbitrary strings",
                    1,
                ),
            ),
            (
                "result equality removed",
                self.text.replace(
                    "The first three members equal\n"
                    "  their params members in JSON type and value",
                    "The first three members may differ from their params members",
                    1,
                ),
            ),
            (
                "original session match removed",
                self.text.replace(
                    "both its complete `SessionRefV1`\n"
                    "   exactly equals the original request's recorded session "
                    "and the complete",
                    "the complete",
                    1,
                ),
            ),
            (
                "expired health process left active",
                self.text.replace(
                    "the host MUST record exactly one health failure at expiry,\n"
                    "    close that connection, terminate the old adapter "
                    "process, and confirm its\n"
                    "    exit before initializing any replacement permitted by "
                    "the current\n"
                    "    adapter-state gate",
                    "the host MAY leave the old adapter process active",
                    1,
                ),
            ),
            (
                "replacement reanchored to old expiry",
                self.text.replace(
                    "after that\n"
                    "    successful replacement initialization",
                    "after the expired request's deadline",
                    1,
                ),
            ),
            (
                "immediate catch-up allowed",
                self.text.replace(
                    "no immediate catch-up\n"
                    "    dispatch occurs after a slow initialization",
                    "an immediate catch-up dispatch occurs after initialization",
                    1,
                ),
            ),
            (
                "teardown latency counted as another failure",
                self.text.replace(
                    "are not additional health failures and do\n"
                    "    not create a missed or overlapping health request",
                    "count as additional health failures",
                    1,
                ),
            ),
            (
                "failure count reset on replacement",
                self.text.replace(
                    "consecutive health-failure count survives that connection "
                    "and process\n"
                    "    replacement",
                    "health-failure count resets after connection replacement",
                    1,
                ),
            ),
            (
                "unhealthy replacement bypasses recovery gate",
                self.text.replace(
                    "only Clause 12's explicitly authorized recovery\n"
                    "    route can admit a replacement",
                    "an automatic replacement remains allowed",
                    1,
                ),
            ),
            (
                "stale no-overlap rationale restored",
                self.text.replace(
                    "This completion-anchored cadence on a live initialized "
                    "connection, the\n"
                    "    timeout-ended cadence epoch, replacement-initialization "
                    "re-anchor, and the\n"
                    "    one-request",
                    "This mandatory expiry teardown, completion- or "
                    "deadline-expiry-anchored cadence, and the one-request bound",
                    1,
                ),
            ),
        )
        mutated_texts = [mutated for _name, mutated in mutations]
        self.assertEqual(len(set(mutated_texts)), len(mutated_texts))
        for name, mutated in mutations:
            with self.subTest(mutation=name):
                self.assertNotEqual(mutated, self.text)
                self.assertIn(False, contract_invariants(mutated).values())


if __name__ == "__main__":
    unittest.main()
