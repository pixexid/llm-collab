from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import deliver


class AxDoorbellRoutingTest(unittest.TestCase):
    """PR78 R6: an unsupported cli_session recipient (no activation.ax_app) must
    NOT be treated as an AX-doorbell target, so deliver.py never emits an AX ring
    for a wake transport that fails closed (e.g. Gemini after R5 mapped it to the
    .unknown composer profile)."""

    def test_cli_session_without_ax_app_is_not_ax_doorbell(self) -> None:
        gemini = {
            "id": "gemini",
            "activation": {"type": "cli_session", "watcher_enabled": True},
        }
        self.assertIsNone(deliver.ax_doorbell_app(gemini))
        self.assertFalse(deliver.is_ax_doorbell_target(gemini, "gemini"))

    def test_cli_session_with_ax_app_is_ax_doorbell(self) -> None:
        codex = {
            "id": "codex",
            "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"},
        }
        self.assertEqual(deliver.ax_doorbell_app(codex), "Codex")
        self.assertTrue(deliver.is_ax_doorbell_target(codex, "codex"))

    def test_human_relay_is_not_ax_doorbell(self) -> None:
        antigravity = {
            "id": "antigravity",
            "activation": {"type": "human_relay", "watcher_enabled": False},
        }
        self.assertFalse(deliver.is_ax_doorbell_target(antigravity, "antigravity"))

    def test_blank_ax_app_is_not_ax_doorbell(self) -> None:
        agent = {
            "id": "x",
            "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "  "},
        }
        self.assertIsNone(deliver.ax_doorbell_app(agent))
        self.assertFalse(deliver.is_ax_doorbell_target(agent, "x"))


if __name__ == "__main__":
    unittest.main()
