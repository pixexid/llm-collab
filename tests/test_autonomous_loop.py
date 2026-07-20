from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import autonomous_loop


class AutonomousLoopStateRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / autonomous_loop.STATE_FILE_NAME

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def read(
        self,
        payload: dict[str, object],
        project_id: str = "amiga",
    ) -> dict[str, object]:
        self.path.write_text(json.dumps(payload))
        return autonomous_loop.read_state(self.path, project_id)

    def test_valid_v1_state_recomputes_stale_next_action(self) -> None:
        state = self.read(
            {
                "schema_version": 1,
                "mode": "worker_wait",
                "current": {"worker": "claude"},
                "next_action": "Trust this stale persisted instruction.",
            }
        )

        self.assertEqual(
            state["next_action"],
            "Check inbox, bridge status, and visible claude state; do not interrupt a running worker.",
        )

    def test_missing_version_recovers_and_preserves_existing_notes(self) -> None:
        existing_note = {"at": "earlier", "text": "Keep this note."}
        state = self.read(
            {
                "mode": "acceptance",
                "current": {"task": "TASK-OLD"},
                "next_action": "Stale.",
                "notes": [existing_note],
            }
        )

        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["notes"][0], existing_note)
        self.assertIn("missing schema_version", state["notes"][1]["text"])
        self.assertEqual(
            state["next_action"],
            "Run dirty-worktree acceptance and task-contract review for TASK-OLD.",
        )

    def test_non_v1_version_recovers_with_stored_version_note(self) -> None:
        state = self.read({"schema_version": 2, "mode": "next_lane"})

        self.assertEqual(state["schema_version"], 1)
        self.assertIn("schema_version=2", state["notes"][-1]["text"])
        self.assertIn("normalized to schema_version=1", state["notes"][-1]["text"])

    def test_registered_non_amiga_project_recovers_canonical_state(self) -> None:
        state = self.read(
            {
                "schema_version": 0,
                "project_id": "amiga",
                "mode": "acceptance",
                "current": {"task": "TASK-NUVYR"},
                "next_action": "Stale cross-project instruction.",
            },
            project_id="nuvyr",
        )

        self.assertEqual(state["project_id"], "nuvyr")
        self.assertEqual(state["schema_version"], 1)
        self.assertIn("schema_version=0", state["notes"][-1]["text"])
        self.assertEqual(
            state["next_action"],
            "Run dirty-worktree acceptance and task-contract review for TASK-NUVYR.",
        )

    def test_boolean_version_recovers_with_stored_condition_note(self) -> None:
        state = self.read({"schema_version": True})

        self.assertIs(type(state["schema_version"]), int)
        self.assertEqual(state["schema_version"], 1)
        self.assertIn("schema_version=True", state["notes"][-1]["text"])

    def test_float_version_recovers_with_stored_condition_note(self) -> None:
        state = self.read({"schema_version": 1.0})

        self.assertIs(type(state["schema_version"]), int)
        self.assertEqual(state["schema_version"], 1)
        self.assertIn("schema_version=1.0", state["notes"][-1]["text"])

    def test_valid_v1_state_has_no_false_version_recovery_note(self) -> None:
        existing_note = {"at": "earlier", "text": "Existing operational note."}
        state = self.read({"schema_version": 1, "notes": [existing_note]})

        self.assertIs(type(state["schema_version"]), int)
        self.assertEqual(state["notes"], [existing_note])

    def test_invalid_json_recovery_remains_intact(self) -> None:
        self.path.write_text("{invalid")

        state = autonomous_loop.read_state(self.path, "amiga")

        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["mode"], "idle")
        self.assertEqual(state["next_action"], autonomous_loop.next_action(state))
        self.assertEqual(len(state["notes"]), 1)
        self.assertIn("Recovered from unreadable autonomous-loop.json", state["notes"][0]["text"])


if __name__ == "__main__":
    unittest.main()
