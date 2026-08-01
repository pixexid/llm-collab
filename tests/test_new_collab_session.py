import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import new_collab_session as ncs


class WakeChannelTest(unittest.TestCase):
    def test_codex_is_ax_not_watcher_despite_watcher_enabled(self):
        # The whole bug-class: codex carries watcher_enabled=True but has no
        # native session watcher. The ax_app must win, or it gets classified
        # as watcher-backed and the AX wake is dropped.
        codex = {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"}
        self.assertEqual(ncs.wake_channel(codex), "ax_doorbell")

    def test_watcher_backed_worker(self):
        claude = {"type": "cli_session", "watcher_enabled": True}
        self.assertEqual(ncs.wake_channel(claude), "watcher")

    def test_attended_only(self):
        self.assertEqual(ncs.wake_channel({"ax_attended_only": True}), "ax_attended")

    def test_human_relay(self):
        self.assertEqual(ncs.wake_channel({"type": "human_relay"}), "relay")


class CoworkerPromptTest(unittest.TestCase):
    def test_codex_prompt_says_poll_not_watch(self):
        p = ncs.coworker_prompt("codex", "ax_doorbell", "llm-collab",
                                "CHAT-ABCD1234", "app", "codex_app")
        self.assertIn("NO native session watcher", p)
        self.assertIn("inbox.py", p)
        self.assertNotIn("watch_inbox.py", p)
        self.assertIn("SESSION-CODEX-ABCD1234", p)

    def test_watcher_prompt_arms_watcher(self):
        p = ncs.coworker_prompt("gemini", "watcher", "llm-collab",
                                "CHAT-ABCD1234", "app", "gemini_cli")
        self.assertIn("watch_inbox.py", p)

    def test_prompt_never_hardcodes_a_native_id(self):
        p = ncs.coworker_prompt("codex", "ax_doorbell", "llm-collab",
                                "CHAT-ABCD1234", "app", "codex_app")
        self.assertIn("<YOUR_ID>", p)


if __name__ == "__main__":
    unittest.main()
