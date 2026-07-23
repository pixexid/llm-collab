"""Tests for fail-closed Runtime Adapter V1 claim publication."""

from __future__ import annotations

import ast
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_claim import (
    EXERCISED_CONFORMING,
    REFERENCED_NOT_EXERCISED,
    UNREFERENCED,
    ClaimFailure,
    ClaimSuccess,
    _claim_from_checked,
    _coverage_states,
    _fixture_replays,
    _replayed_fixture_ids,
    _matches_refusal,
    build_claim,
    publish_claim,
)
from llm_collab.runtime_adapter_conformance import ClauseOccurrence
from llm_collab.runtime_adapter_fixtures import FIXTURES, ExpectedRefusal, NO_STATE_CHANGE


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_claim.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
FIXTURES_PATH = ROOT / "llm_collab" / "runtime_adapter_fixtures.py"
REFERENCE_PATH = ROOT / "llm_collab" / "runtime_adapter_reference.py"


class RuntimeAdapterClaimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_default_claim_fails_closed_and_emits_no_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claim.json"
            result = publish_claim(self.protocol, path, repo_root=tmp)

        self.assertIsInstance(result, ClaimFailure)
        self.assertGreater(len(result.gaps), 0)
        self.assertFalse(path.exists())

    def test_real_fixtures_drive_claim_replay(self) -> None:
        exercised = _replayed_fixture_ids(FIXTURES)
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        self.assertEqual(
            exercised,
            {
                "runtime-adapter-health-request",
                "runtime-adapter-reconcile-request",
                "runtime-adapter-shutdown-rejects-session-selector",
                "runtime-adapter-host-response-is-direction-fault",
            },
        )
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        exercised_clause_keys = {
            ref.clause_key
            for fixture in FIXTURES
            if fixture.fixture_id in exercised
            for ref in fixture.clause_refs
        }
        self.assertFalse(gap_keys & exercised_clause_keys)

    def test_coverage_states_keep_three_distinct_states(self) -> None:
        positive = ClauseOccurrence("pos", "a" * 64, "MUST do it", "MUST", 1, "C01 Test")
        prohibition = ClauseOccurrence("neg", "b" * 64, "MUST NOT do it", "MUST NOT", 2, "C01 Test")
        conforming_positive = replace(FIXTURES[0], clause_refs=(replace(FIXTURES[0].clause_refs[0], clause_key="pos"),))
        conforming_negative = replace(FIXTURES[0], clause_refs=(replace(FIXTURES[0].clause_refs[0], clause_key="neg"),))
        violating_negative = replace(
            FIXTURES[-1],
            fixture_id="violating-negative",
            clause_refs=(replace(FIXTURES[-1].clause_refs[0], clause_key="neg"),),
        )
        violating_positive = replace(
            FIXTURES[-1],
            fixture_id="violating-positive",
            clause_refs=(replace(FIXTURES[-1].clause_refs[0], clause_key="pos"),),
        )

        states = _coverage_states(
            (positive, prohibition, ClauseOccurrence("none", "c" * 64, "MUST wait", "MUST", 3, "C01 Test")),
            (conforming_positive, conforming_negative),
            {"runtime-adapter-health-request"},
        )

        self.assertEqual(states["pos"], EXERCISED_CONFORMING)
        self.assertEqual(states["neg"], UNREFERENCED)
        self.assertEqual(states["none"], UNREFERENCED)
        self.assertEqual(
            _coverage_states((prohibition,), (violating_negative,), {"violating-negative"})["neg"],
            EXERCISED_CONFORMING,
        )
        self.assertEqual(
            _coverage_states((prohibition,), (violating_negative,), set())["neg"],
            REFERENCED_NOT_EXERCISED,
        )
        self.assertEqual(
            _coverage_states((positive,), (violating_positive,), {"violating-positive"})["pos"],
            EXERCISED_CONFORMING,
        )

    def test_success_claim_writes_only_when_every_clause_is_exercised(self) -> None:
        clause = ClauseOccurrence(
            FIXTURES[0].clause_refs[0].clause_key,
            FIXTURES[0].clause_refs[0].text_sha256,
            "MUST do it",
            "MUST",
            1,
            "C01 Test",
        )
        result = _claim_from_checked((clause,), (FIXTURES[0],), {FIXTURES[0].fixture_id})

        self.assertIsInstance(result, ClaimSuccess)
        self.assertEqual(result.artifact["claim"], EXERCISED_CONFORMING)
        self.assertEqual(result.artifact["gaps"], [])
        self.assertEqual(
            result.artifact["clauses"],
            [{"clause_key": clause.clause_key, "state": EXERCISED_CONFORMING}],
        )

    def test_violating_fixture_refusal_counts_as_exercised(self) -> None:
        self.assertTrue(_fixture_replays(FIXTURES[-1]))
        self.assertFalse(
            _matches_refusal(
                ExpectedRefusal("INVALID_REQUEST", -32600, NO_STATE_CHANGE, response_emitted=True),
                '{"jsonrpc":"2.0","id":"x","result":{"accepted":true}}',
            )
        )

    def test_output_path_must_be_inside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "inside the repository"):
                publish_claim(self.protocol, Path(tmp) / "claim.json", repo_root=ROOT)

    def test_no_external_publication_or_process_imports(self) -> None:
        forbidden = {"http", "urllib", "requests", "socket", "subprocess", "webbrowser"}
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name.split(".")[0] in forbidden for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden)

    def test_only_write_is_caller_supplied_claim_path(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        write_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"write", "write_text", "write_bytes"}
        ]

        self.assertEqual(len(write_calls), 1)
        self.assertEqual(write_calls[0].func.attr, "write_text")
        self.assertIsInstance(write_calls[0].func.value, ast.Name)
        self.assertEqual(write_calls[0].func.value.id, "path")

    def test_fixtures_and_adapter_do_not_import_claim_module(self) -> None:
        for path in (FIXTURES_PATH, REFERENCE_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_claim" for alias in node.names))
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "llm_collab.runtime_adapter_claim")


if __name__ == "__main__":
    unittest.main()
