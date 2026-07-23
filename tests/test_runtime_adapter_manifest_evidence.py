"""Tests for deterministic Runtime Adapter trusted-manifest evidence."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_admission_evidence import build_admission_evidence
from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_manifest import UNTRUSTED_MANIFEST_INPUT
from llm_collab.runtime_adapter_manifest_evidence import (
    ARTIFACT_LABEL,
    MANIFEST_SECURITY_EVIDENCED,
    ManifestEvidenceFailure,
    build_manifest_evidence,
)
from llm_collab.runtime_adapter_transport_evidence import build_transport_evidence


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_manifest_evidence.py"
CLAIM_PATH = ROOT / "llm_collab" / "runtime_adapter_claim.py"
SUPERVISOR_PATH = ROOT / "llm_collab" / "runtime_adapter_supervisor.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
MANIFEST_KEYS = {
    "Cc15ecf086a5b.1",
    "Cc02b8dfb1bfa.1",
    "Cc02b8dfb1bfa.2",
    "Ca183987f3efe.1",
    "Ca183987f3efe.2",
}
GROUP_B_KEYS = {"Cd87ad3561bfc.1", "C4c2db37e63d2.1", "C4c2db37e63d2.2", "C4c2db37e63d2.3"}


class RuntimeAdapterManifestEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_manifest_evidence_is_distinct_and_covers_only_group_a_rows(self) -> None:
        artifact = build_manifest_evidence(self.protocol)

        self.assertEqual(artifact["artifact_label"], ARTIFACT_LABEL)
        self.assertEqual(artifact["evidence_kind"], "manifest_launch_security")
        self.assertEqual(artifact["claim"], MANIFEST_SECURITY_EVIDENCED)
        self.assertNotEqual(artifact["claim"], "exercised_conforming")
        covered = {clause["clause_key"] for clause in artifact["clauses"]}
        self.assertEqual(covered, MANIFEST_KEYS)
        self.assertFalse(covered & GROUP_B_KEYS)
        self.assertTrue(
            all(
                clause["state"] == MANIFEST_SECURITY_EVIDENCED and clause["evidence"] == ARTIFACT_LABEL
                for clause in artifact["clauses"]
            )
        )

    def test_valid_manifest_resolution_and_unknown_adapter_are_non_vacuous(self) -> None:
        observation = build_manifest_evidence(self.protocol)["observation"]

        self.assertEqual(observation["resolved_adapter_id"], "adapter_a")
        self.assertEqual(observation["resolved_adapter_revision"], "rev_1")
        self.assertEqual(observation["resolved_manifest_id"], "manifest_a")
        self.assertEqual(observation["resolved_manifest_revision"], "manifest_rev_1")
        self.assertEqual(observation["resolved_executable"], "/trusted/bin/adapter-a")
        self.assertEqual(observation["resolved_argv"], ("adapter-a", "--stdio"))
        self.assertEqual(observation["resolved_working_directory"], "/trusted/work")
        self.assertEqual(observation["resolved_environment"], {"SAFE": "1"})
        self.assertEqual(observation["unknown_adapter_fault"], UNTRUSTED_MANIFEST_INPUT)

    def test_each_untrusted_manifest_input_class_fails_closed_before_resolution(self) -> None:
        observation = build_manifest_evidence(self.protocol)["observation"]

        self.assertEqual(
            observation["rejected_field_faults"],
            {
                "executable": UNTRUSTED_MANIFEST_INPUT,
                "argv": UNTRUSTED_MANIFEST_INPUT,
                "working_directory": UNTRUSTED_MANIFEST_INPUT,
                "environment_outside_allowlist": UNTRUSTED_MANIFEST_INPUT,
                "shell": UNTRUSTED_MANIFEST_INPUT,
                "manifest_path": UNTRUSTED_MANIFEST_INPUT,
                "adapter_alias": UNTRUSTED_MANIFEST_INPUT,
            },
        )
        self.assertFalse(observation["rejection_outputs_resolved_adapter"])

    def test_supervisor_ast_proves_direct_exec_without_shell_or_hidden_second_spawn(self) -> None:
        observation = build_manifest_evidence(self.protocol)["observation"]["supervisor_spawn"]

        self.assertEqual(observation["popen_call_count"], 1)
        self.assertTrue(observation["shell_keyword_false"])
        self.assertTrue(observation["args_are_resolved_argv"])
        self.assertEqual(observation["shell_true_count"], 0)
        self.assertEqual(observation["forbidden_spawn_helpers"], ())

        tree = ast.parse(SUPERVISOR_PATH.read_text(encoding="utf-8"))
        popen_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "Popen"
        ]
        self.assertEqual(len(popen_calls), 1)
        self.assertFalse(
            any(
                isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                for call in popen_calls
                for arg in call.args
            )
        )

    def test_build_claim_still_gaps_manifest_rows(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        self.assertLessEqual(MANIFEST_KEYS, {gap["clause_key"] for gap in result.gaps})

    def test_manifest_ledger_is_scoped_disjoint_from_transport_and_admission_ledgers(self) -> None:
        manifest = build_manifest_evidence(self.protocol)
        transport = build_transport_evidence(self.protocol)
        admission = build_admission_evidence(self.protocol)

        manifest_keys = {clause["clause_key"] for clause in manifest["clauses"]}
        transport_keys = {clause["clause_key"] for clause in transport["clauses"]}
        admission_keys = {clause["clause_key"] for clause in admission["clauses"]}
        self.assertFalse(manifest_keys & transport_keys)
        self.assertFalse(transport_keys & manifest_keys)
        self.assertFalse(manifest_keys & admission_keys)
        self.assertFalse(admission_keys & manifest_keys)

    def test_clause_text_drift_fails_closed_for_all_manifest_groups(self) -> None:
        replacements = (
            (
                "The host MUST resolve the adapter\n   executable",
                "The host MUST select the adapter\n   executable",
            ),
            (
                "The host MUST\n   execute the resolved program directly",
                "The host MUST\n   start the resolved program directly",
            ),
            (
                "A caller-\n   supplied executable path",
                "A caller-\n   provided executable path",
            ),
        )
        for old, new in replacements:
            with self.subTest(old=old):
                changed = self.protocol.replace(old, new)
                self.assertNotEqual(changed, self.protocol)
                with self.assertRaisesRegex(ManifestEvidenceFailure, "missing manifest clause|stale manifest clause"):
                    build_manifest_evidence(changed)

    def test_manifest_evidence_module_does_not_spawn(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "subprocess" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "subprocess")
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    self.assertFalse(func.value.id == "subprocess" and func.attr == "Popen")
                if isinstance(func, ast.Name):
                    self.assertNotEqual(func.id, "Popen")

    def test_manifest_module_and_claim_module_remain_disjoint(self) -> None:
        manifest_tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        claim_tree = ast.parse(CLAIM_PATH.read_text(encoding="utf-8"))

        for node in ast.walk(manifest_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_claim" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_claim")
        for node in ast.walk(claim_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_manifest_evidence" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_manifest_evidence")


if __name__ == "__main__":
    unittest.main()
