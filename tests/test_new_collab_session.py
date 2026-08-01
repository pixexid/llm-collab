import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_prompt_uses_launcher_not_raw_python(self):
        p = ncs.coworker_prompt("gemini", "watcher", "llm-collab",
                                "CHAT-ABCD1234", "app", "gemini_cli")
        self.assertIn("bin/llm-collab", p)
        self.assertNotIn("python bin/", p)

    def test_prompt_uses_discovered_home_not_a_default(self):
        p = ncs.coworker_prompt("claude", "watcher", "llm-collab",
                                "CHAT-ABCD1234", "app", "claude_app")
        self.assertIn("<YOUR_HOME_FROM_STEP_1>", p)
        self.assertNotIn("--runtime-home ~/.claude", p)

    def test_claude_prompt_scopes_discovery_with_project_path(self):
        p = ncs.coworker_prompt("claude", "watcher", "llm-collab",
                                "CHAT-ABCD1234", "app", "claude_app")
        self.assertIn("--project-path", p)

    def test_watch_cmd_omits_skip_existing(self):
        cmd = ncs.watch_cmd("claude", "p", "CHAT-X", "S", "app", "id", "claude_app")
        self.assertNotIn("--skip-existing", cmd)

    def test_watch_cmd_exports_reader_family(self):
        # GH-468: the reader must carry its actual family, so the watcher exports
        # LLM_COLLAB_READER_RUNTIME_FAMILY alongside the id.
        cmd = ncs.watch_cmd("claude", "p", "CHAT-X", "S", "app", "id", "claude_app")
        self.assertIn("export LLM_COLLAB_READER_RUNTIME_FAMILY=claude_app", cmd)


class PickupBlockTest(unittest.TestCase):
    def test_codex_pickup_never_arms_a_watcher(self):
        # Codex has no native watcher: the initiator/self output must not tell it
        # to arm one (codex ruling).
        block = "\n".join(ncs.pickup_block(
            "ax_doorbell", "codex", "llm-collab", "CHAT-ABCD1234",
            "SESSION-CODEX", "app", "019f-native", "codex_app"))
        self.assertNotIn("watch_inbox.py", block)
        self.assertIn("inbox.py", block)

    def test_watcher_pickup_arms_a_watcher(self):
        block = "\n".join(ncs.pickup_block(
            "watcher", "claude", "llm-collab", "CHAT-ABCD1234",
            "SESSION-CLAUDE", "app", "3db9-native", "claude_app"))
        self.assertIn("watch_inbox.py", block)
        self.assertIn("export LLM_COLLAB_READER_RUNTIME_FAMILY=claude_app", block)


class DirtyCheckoutGuardTest(unittest.TestCase):
    def _run_guard(self, *, head, origin, dirty):
        def fake_git(*args):
            a = list(args)
            if a[:2] == ["rev-parse", "origin/main"]:
                return origin
            if a[:2] == ["rev-parse", "HEAD"]:
                return head
            if a[0] == "status":
                return dirty
            return ""
        with patch.object(ncs, "_git", side_effect=fake_git):
            ncs.assert_current_checkout()

    def test_clean_origin_main_passes(self):
        self._run_guard(head="abc", origin="abc", dirty="")  # no raise

    def test_same_head_but_dirty_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._run_guard(head="abc", origin="abc", dirty=" M bin/x.py")

    def test_behind_origin_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._run_guard(head="old", origin="new", dirty="")


class MainPathTest(unittest.TestCase):
    AGENTS = [
        {"id": "claude", "activation": {"type": "cli_session", "watcher_enabled": True}},
        {"id": "codex", "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"}},
    ]

    def _main(self, argv):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS), \
             patch.object(ncs, "register_session", return_value=None), \
             patch.object(ncs, "subprocess") as sub:
            sub.run.return_value = type("R", (), {"returncode": 0, "stdout": '{"chat_id": "CHAT-TEST9999"}', "stderr": ""})()
            with redirect_stdout(out):
                ncs.main()
            return out.getvalue(), sub

    def test_codex_initiator_gets_poll_not_watcher(self):
        # P1: the initiator self-pickup must branch by wake channel — a Codex
        # initiator must never be told to arm a persistent native watcher.
        out, _ = self._main([
            "--project", "p", "--title", "t", "--me", "codex",
            "--my-runtime-session-id", "019f-x", "--my-runtime-family", "codex_app",
            "--with", "claude:claude_app", "--repo-target", "app", "--skip-currency-check",
        ])
        # initiator (codex) section is before the coworker section.
        initiator = out.split("SETUP PROMPT")[0]
        self.assertIn("NO native session watcher", initiator)
        self.assertNotIn("watch_inbox.py", initiator)

    def test_claude_initiator_arms_watcher(self):
        out, _ = self._main([
            "--project", "p", "--title", "t", "--me", "claude",
            "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
            "--with", "codex:codex_app", "--repo-target", "app", "--skip-currency-check",
        ])
        initiator = out.split("SETUP PROMPT")[0]
        self.assertIn("watch_inbox.py", initiator)

    def test_unsupported_coworker_family_is_refused_with_no_chat(self):
        argv = ["--project", "p", "--title", "t", "--me", "claude",
                "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
                "--with", "glmpi:pi", "--repo-target", "app", "--skip-currency-check"]
        with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS + [
                 {"id": "glmpi", "activation": {"type": "cli_session", "watcher_enabled": True}}]), \
             patch.object(ncs, "subprocess") as sub:
            with self.assertRaises(SystemExit):
                ncs.main()
            sub.run.assert_not_called()  # refused before chat creation

    def test_register_failure_rolls_back_the_chat(self):
        argv = ["--project", "p", "--title", "t", "--me", "claude",
                "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
                "--with", "codex:codex_app", "--repo-target", "app", "--skip-currency-check"]
        with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS), \
             patch.object(ncs, "register_session", side_effect=RuntimeError("boom")), \
             patch.object(ncs, "shutil") as sh, \
             patch.object(ncs, "subprocess") as sub:
            sub.run.return_value = type("R", (), {"returncode": 0, "stdout": '{"chat_id": "CHAT-X", "path": "/tmp/chat-x"}', "stderr": ""})()
            with self.assertRaises(SystemExit):
                ncs.main()
            sh.rmtree.assert_called_once()  # orphan chat rolled back

    def test_unknown_initiator_creates_no_chat(self):
        # P2: an invalid --me must fail closed BEFORE new_chat.py runs.
        argv = ["--project", "p", "--title", "t", "--me", "ghost",
                "--my-runtime-session-id", "x", "--my-runtime-family", "claude_app",
                "--with", "codex:codex_app", "--repo-target", "app", "--skip-currency-check"]
        with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS), \
             patch.object(ncs, "register_session", return_value=None), \
             patch.object(ncs, "subprocess") as sub:
            with self.assertRaises(SystemExit):
                ncs.main()
            sub.run.assert_not_called()  # no chat/register side effect


if __name__ == "__main__":
    unittest.main()
