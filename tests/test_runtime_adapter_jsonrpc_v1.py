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
    deadline_match = re.search(
        r"`HEALTH_DEADLINE_MS`, fixed at ([\d,]+) milliseconds", compact
    )
    interval_match = re.search(
        r"`HEALTH_INTERVAL_MS`, fixed at ([\d,]+) milliseconds", compact
    )
    deadline = int(deadline_match.group(1).replace(",", "")) if deadline_match else 0
    interval = int(interval_match.group(1).replace(",", "")) if interval_match else 0
    return {
        "cancel_params": (
            "`runtime.cancel` params contain exactly `session_ref`, "
            "`original_request_id`, `delivery_id`, and `attempt_id`"
        )
        in compact,
        "cancel_result": (
            "Its success result contains exactly `original_request_id`, "
            "`delivery_id`, `attempt_id`, and `status`"
        )
        in compact,
        "cancel_match": (
            "MUST refuse cancellation unless the complete "
            "`(original_request_id, delivery_id, attempt_id)` triple exactly "
            "matches the recorded original delivery and attempt"
        )
        in compact,
        "cancel_mismatch": (
            "A mismatch is `INVALID_DELIVERY` at P7, performs no action, "
            "and advances no state"
        )
        in compact,
        "cancel_idempotence": (
            "Cancellation is idempotent only for the complete matching "
            "`(original_request_id, delivery_id, attempt_id)` triple"
        )
        in compact,
        "health_deadline": 0 < deadline < interval,
        "health_scope": (
            "`runtime.health` alone uses `HEALTH_DEADLINE_MS`, fixed at 5,000 "
            "milliseconds"
        )
        in compact,
        "health_anchor": (
            "after the previous health request completes or after that "
            "request's `HEALTH_DEADLINE_MS` expires, never from dispatch"
        )
        in compact,
        "health_no_overlap": (
            "makes an overlapping health request structurally impossible for "
            "a conforming host"
        )
        in compact,
        "health_no_forced_miss": (
            "A forced miss from overlap therefore cannot occur and is not a "
            "health-failure category"
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
        anchors = tuple(case["completion_or_expiry_anchors_ms"])
        return tuple(anchor + interval for anchor in anchors)
    raise AssertionError(f"unknown scenario: {scenario}")


def paired_cases() -> tuple[dict[str, object], ...]:
    templates = (
        {
            "scenario": "cancel",
            "recorded_identity": ("request-reused", "delivery-1", "attempt-1"),
            "requested_identity": ("request-reused", "delivery-1", "attempt-1"),
            "expected": "cancelled",
        },
        {
            "scenario": "cancel",
            "recorded_identity": ("request-reused", "delivery-1", "attempt-1"),
            "requested_identity": ("request-reused", "delivery-2", "attempt-1"),
            "expected": "refused_invalid_delivery",
        },
        {
            "scenario": "health_boundaries",
            "interval_ms": 10_000,
            "completion_or_expiry_anchors_ms": (0, 10_000, 20_000),
            "expected": (10_000, 20_000, 30_000),
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
            self.text.replace(
                "`runtime.cancel` params contain exactly `session_ref`,\n"
                "  `original_request_id`, `delivery_id`, and `attempt_id`",
                "`runtime.cancel` params contain exactly `session_ref` and\n"
                "  `original_request_id`",
                1,
            ),
            self.text.replace(
                "adapter MUST refuse cancellation unless the complete",
                "adapter MAY accept cancellation without checking the complete",
                1,
            ),
            self.text.replace(
                "`HEALTH_DEADLINE_MS`, fixed at 5,000\n"
                "   milliseconds from receipt",
                "`HEALTH_DEADLINE_MS`, fixed at 10,000\n"
                "   milliseconds from receipt",
                1,
            ),
            self.text.replace("never from dispatch", "from dispatch", 1),
            self.text.replace(
                "cannot occur and is not a health-failure category",
                "counts as one failed response",
                1,
            ),
        )
        self.assertEqual(len(set(mutations)), len(mutations))
        for mutated in mutations:
            with self.subTest():
                self.assertNotEqual(mutated, self.text)
                self.assertIn(False, contract_invariants(mutated).values())


if __name__ == "__main__":
    unittest.main()
