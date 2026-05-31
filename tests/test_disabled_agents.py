from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _helpers


class DisabledAgentsTest(unittest.TestCase):
    def test_legacy_disabled_role_blocks_agent_use(self) -> None:
        agent = {
            "id": "cdx2",
            "role": "legacy_disabled_implementation",
            "activation": {"type": "human_relay", "watcher_enabled": False},
        }

        self.assertTrue(_helpers.is_agent_disabled(agent))

    def test_enabled_human_relay_remains_available(self) -> None:
        agent = {
            "id": "antigravity",
            "role": "implementation",
            "activation": {"type": "human_relay", "watcher_enabled": False},
        }

        self.assertFalse(_helpers.is_agent_disabled(agent))

    def test_ensure_agent_enabled_exits_for_disabled_agent(self) -> None:
        agent = {
            "id": "cdx2",
            "role": "legacy_disabled_implementation",
            "activation": {"type": "human_relay", "watcher_enabled": False},
        }

        with patch.object(_helpers, "get_agent", return_value=agent):
            with self.assertRaises(SystemExit) as context:
                _helpers.ensure_agent_enabled("cdx2", context="test routing")

        self.assertEqual(context.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
