"""Focused checks for retired instruction copies."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import contract_drift  # noqa: E402


class ContractDriftTest(unittest.TestCase):
    def test_manual_only_review_copy_is_stale(self):
        self.assertTrue(
            contract_drift.claims_review_is_manual_only(
                "Codex review is manual only; nothing arrives unless requested."
            )
        )

    def test_waiting_for_the_automatic_pass_is_current(self):
        self.assertFalse(
            contract_drift.claims_review_is_manual_only(
                "Every PR waits for one automatic bot pass before merge."
            )
        )


if __name__ == "__main__":
    unittest.main()
