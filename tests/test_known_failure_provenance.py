"""Checked provenance for known-red test match sets."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
KNOWN_FAILURES_PATH = ROOT / "tests" / "known_failures.json"
TOP_LEVEL_KEYS = {"version", "known_failures"}
ENTRY_KEYS = {
    "test_id",
    "command",
    "first_observed",
    "match_count",
    "match_ids",
    "tracking_issue",
}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _guarded_tokens() -> tuple[str, ...]:
    return (
        "daemon" + "_observation",
        "canonical" + "_writes",
        "runtime" + "_dispatch",
        "ax" + "_v2",
        "remote" + "_transport",
        "https://llm-collab.dev/" + "declarations",
    )


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_known_failures(path: Path = KNOWN_FAILURES_PATH) -> dict[str, object]:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_json_constant,
        object_pairs_hook=reject_duplicate_pairs,
    )


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        strings: list[str] = []
        for item in value:
            strings.extend(_string_values(item))
        return strings
    if isinstance(value, dict):
        strings = []
        for item in value.values():
            strings.extend(_string_values(item))
        return strings
    return []


def validate_known_failure_provenance(
    payload: dict[str, object],
    observed_failures: dict[str, set[str]],
) -> list[str]:
    errors: list[str] = []
    if set(payload) != TOP_LEVEL_KEYS:
        errors.append("top-level keys must be exactly version and known_failures")
        return errors
    if payload.get("version") != 1:
        errors.append("version must be 1")
    entries = payload.get("known_failures")
    if not isinstance(entries, list):
        errors.append("known_failures must be a list")
        return errors

    seen_tests: set[str] = set()
    covered_tests: set[str] = set()
    for index, entry in enumerate(entries):
        prefix = f"known_failures[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(entry) != ENTRY_KEYS:
            errors.append(f"{prefix} keys must match the known-failure schema")
            continue

        test_id = entry["test_id"]
        if not isinstance(test_id, str) or not test_id:
            errors.append(f"{prefix}.test_id must be a non-empty string")
            continue
        if test_id in seen_tests:
            errors.append(f"{test_id}: duplicate test_id")
        seen_tests.add(test_id)
        covered_tests.add(test_id)

        command = entry["command"]
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            errors.append(f"{test_id}: command must be a non-empty string list")

        first_observed = entry["first_observed"]
        if not isinstance(first_observed, str) or not COMMIT_RE.fullmatch(
            first_observed
        ):
            errors.append(f"{test_id}: first_observed commit is missing or unbisected")

        tracking_issue = entry["tracking_issue"]
        if (
            not isinstance(tracking_issue, int)
            or isinstance(tracking_issue, bool)
            or tracking_issue < 1
        ):
            errors.append(f"{test_id}: tracking_issue must be a positive integer")

        match_ids = entry["match_ids"]
        if (
            not isinstance(match_ids, list)
            or not all(isinstance(item, str) and item for item in match_ids)
        ):
            errors.append(f"{test_id}: match_ids must be a string list")
            expected_matches: set[str] = set()
        else:
            expected_matches = set(match_ids)
            if len(expected_matches) != len(match_ids):
                errors.append(f"{test_id}: match_ids must be unique")

        match_count = entry["match_count"]
        if not isinstance(match_count, int) or isinstance(match_count, bool):
            errors.append(f"{test_id}: match_count must be an integer")
        elif match_count != len(expected_matches):
            errors.append(f"{test_id}: match_count must equal match_ids length")

        for token in _guarded_tokens():
            if any(token in value for value in _string_values(entry)):
                errors.append(f"{test_id}: schema content contains a guarded token")
                break

        observed_matches = observed_failures.get(test_id)
        if observed_matches is None:
            errors.append(f"{test_id}: listed known failure is not currently observed")
        elif observed_matches != expected_matches:
            errors.append(f"{test_id}: observed match set differs from recorded set")

    for test_id in sorted(set(observed_failures) - covered_tests):
        errors.append(f"{test_id}: observed known failure lacks provenance")
    return errors


def valid_entry(**overrides: Any) -> dict[str, object]:
    entry: dict[str, object] = {
        "test_id": "tests.test_alpha.AlphaTests.test_guard",
        "command": ["python3.11", "-m", "unittest", "tests.test_alpha"],
        "first_observed": "a" * 40,
        "match_count": 2,
        "match_ids": ["path:one.py", "path:two.py"],
        "tracking_issue": 205,
    }
    entry.update(overrides)
    return entry


def payload_with(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"version": 1, "known_failures": entries}


class KnownFailureProvenanceTests(unittest.TestCase):
    def test_live_known_failure_artifact_is_empty_and_valid(self) -> None:
        payload = load_known_failures()

        self.assertEqual(payload, {"version": 1, "known_failures": []})
        self.assertEqual(validate_known_failure_provenance(payload, {}), [])

    def test_matching_recorded_match_set_is_accepted(self) -> None:
        payload = payload_with([valid_entry()])

        self.assertEqual(
            validate_known_failure_provenance(
                payload,
                {
                    "tests.test_alpha.AlphaTests.test_guard": {
                        "path:one.py",
                        "path:two.py",
                    }
                },
            ),
            [],
        )

    def test_unbisected_entry_is_representable_but_fails(self) -> None:
        payload = payload_with([valid_entry(first_observed=None)])

        self.assertEqual(
            validate_known_failure_provenance(
                payload,
                {
                    "tests.test_alpha.AlphaTests.test_guard": {
                        "path:one.py",
                        "path:two.py",
                    }
                },
            ),
            [
                "tests.test_alpha.AlphaTests.test_guard: "
                "first_observed commit is missing or unbisected"
            ],
        )

    def test_missing_first_observed_entry_fails_schema(self) -> None:
        entry = valid_entry()
        del entry["first_observed"]

        self.assertEqual(
            validate_known_failure_provenance(payload_with([entry]), {}),
            ["known_failures[0] keys must match the known-failure schema"],
        )

    def test_changed_match_set_fails_even_when_test_id_still_fails(self) -> None:
        payload = payload_with([valid_entry()])

        self.assertEqual(
            validate_known_failure_provenance(
                payload,
                {
                    "tests.test_alpha.AlphaTests.test_guard": {
                        "path:one.py",
                        "path:three.py",
                    }
                },
            ),
            [
                "tests.test_alpha.AlphaTests.test_guard: "
                "observed match set differs from recorded set"
            ],
        )

    def test_match_count_must_equal_recorded_match_ids(self) -> None:
        payload = payload_with([valid_entry(match_count=1)])

        self.assertIn(
            "tests.test_alpha.AlphaTests.test_guard: match_count must equal match_ids length",
            validate_known_failure_provenance(
                payload,
                {
                    "tests.test_alpha.AlphaTests.test_guard": {
                        "path:one.py",
                        "path:two.py",
                    }
                },
            ),
        )

    def test_duplicate_test_id_fails(self) -> None:
        payload = payload_with(
            [valid_entry(), valid_entry(match_ids=["path:three.py"], match_count=1)]
        )

        self.assertIn(
            "tests.test_alpha.AlphaTests.test_guard: duplicate test_id",
            validate_known_failure_provenance(
                payload,
                {
                    "tests.test_alpha.AlphaTests.test_guard": {
                        "path:one.py",
                        "path:two.py",
                    }
                },
            ),
        )

    def test_stale_listed_failure_fails(self) -> None:
        payload = payload_with([valid_entry()])

        self.assertIn(
            "tests.test_alpha.AlphaTests.test_guard: listed known failure is not currently observed",
            validate_known_failure_provenance(payload, {}),
        )

    def test_unlisted_observed_failure_fails(self) -> None:
        self.assertEqual(
            validate_known_failure_provenance(
                payload_with([]),
                {"tests.test_beta.BetaTests.test_guard": {"assertion:one"}},
            ),
            ["tests.test_beta.BetaTests.test_guard: observed known failure lacks provenance"],
        )

    def test_guarded_tokens_are_rejected_from_artifact_content(self) -> None:
        guarded = "canonical" + "_writes"
        payload = payload_with([valid_entry(match_ids=[guarded], match_count=1)])

        self.assertIn(
            "tests.test_alpha.AlphaTests.test_guard: schema content contains a guarded token",
            validate_known_failure_provenance(
                payload,
                {"tests.test_alpha.AlphaTests.test_guard": {guarded}},
            ),
        )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate JSON key 'version'"):
            json.loads(
                '{"version":1,"version":2,"known_failures":[]}',
                parse_constant=reject_json_constant,
                object_pairs_hook=reject_duplicate_pairs,
            )


if __name__ == "__main__":
    unittest.main()
