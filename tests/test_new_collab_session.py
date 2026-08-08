import sys as _grsys; from pathlib import Path as _grPath
_grsys.path.insert(0, str(_grPath(__file__).resolve().parent)); import _runtime_gate_testkit  # noqa: E402,F401  GH-503: deterministic gate-bypass install (any run form)
import subprocess
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import new_collab_session as ncs
import new_chat as new_chat_cli


class PlainNewChatTest(unittest.TestCase):
    def test_plain_new_chat_remains_ungated(self):
        from contextlib import redirect_stdout
        import io

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chats = root / "Chats"
            with patch.object(sys, "argv", [
                "new_chat.py", "--title", "Plain chat", "--project", "llm-collab",
            ]), patch.object(new_chat_cli, "ensure_project", return_value=None), \
                 patch.object(new_chat_cli, "ROOT", root), \
                 patch.object(new_chat_cli, "CHATS_DIR", chats), \
                 patch.object(new_chat_cli, "chat_id", return_value="CHAT-PLAIN"), \
                 patch.object(new_chat_cli, "date_prefix", return_value="2026-08-05"), \
                 patch.object(new_chat_cli, "utc_iso", return_value="2026-08-05T00:00:00+00:00"), \
                 redirect_stdout(io.StringIO()):
                new_chat_cli.main()

            chat_dirs = list(chats.iterdir())
            self.assertEqual(1, len(chat_dirs))
            self.assertEqual("CHAT-PLAIN", json.loads(
                (chat_dirs[0] / "meta.json").read_text()
            )["chat_id"])


class WakeChannelTest(unittest.TestCase):
    def test_codex_is_watcher_backed_despite_carrying_an_ax_app(self):
        # Contract v12 reverses the earlier ordering. Codex carries an ax_app
        # AND watcher_enabled=True; the watcher wins, because routine
        # exact-session dispatch is its wake and AX is only the fallback
        # deliver.py selects. Classifying it as ax_doorbell taught every new
        # Codex session to poll and await a ring instead of arming pickup.
        codex = {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"}
        self.assertEqual(ncs.wake_channel(codex), "watcher")

    def test_ax_app_without_a_watcher_still_resolves_to_the_doorbell(self):
        # The ax_doorbell branch is not dead: an activation with an ax_app and
        # no watcher still has no pickup of its own.
        self.assertEqual(
            ncs.wake_channel({"type": "cli_session", "ax_app": "Codex"}),
            "ax_doorbell",
        )

    def test_watcher_backed_worker(self):
        claude = {"type": "cli_session", "watcher_enabled": True}
        self.assertEqual(ncs.wake_channel(claude), "watcher")

    def test_attended_only(self):
        self.assertEqual(ncs.wake_channel({"ax_attended_only": True}), "ax_attended")

    def test_human_relay(self):
        self.assertEqual(ncs.wake_channel({"type": "human_relay"}), "relay")


class CoworkerPromptTest(unittest.TestCase):
    def test_codex_prompt_arms_the_watcher(self):
        # A Codex co-worker is onboarded onto routine dispatch, not polling.
        activation = {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"}
        p = ncs.coworker_prompt("codex", ncs.wake_channel(activation),
                                "llm-collab", "CHAT-ABCD1234", "app", "codex_app",
                                ncs.needs_dispatch_wake(activation))
        self.assertIn("pm2_watchers.py ensure --agent codex", p)
        self.assertNotIn("NO native session watcher", p)
        # the register step still names the exact session; only the WATCHER is
        # agent-wide, because that is the one that dispatches.
        self.assertIn("--session SESSION-CODEX-ABCD1234 --agent codex", p)
        self.assertNotIn("--session SESSION-CODEX-ABCD1234 --repo-target", p)
        self.assertLess(
            p.index("pm2_watchers.py ensure --agent codex-appserver"),
            p.index("session_autobridge.py register"),
        )
        self.assertLess(
            p.index("session_autobridge.py register"),
            p.rindex("pm2_watchers.py ensure --agent codex"),
        )

    def test_watcher_prompt_arms_watcher(self):
        p = ncs.coworker_prompt("gemini", "watcher", "llm-collab",
                                "CHAT-ABCD1234", "app", "gemini_cli")
        self.assertIn("watch_inbox.py", p)

    def test_prompt_states_full_project_chat_refusal_scope(self):
        # GH-468: the refusal key is the (project_id, chat_id) scope, not "another
        # chat" — the prompt must not imply cross-project chat_id reuse works.
        p = ncs.coworker_prompt("gemini", "watcher", "llm-collab",
                                "CHAT-ABCD1234", "app", "gemini_cli")
        self.assertIn("(project_id, chat_id)", p)

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
    def test_codex_pickup_arms_a_watcher(self):
        # Contract v12: Codex is watcher-backed like every other worker, so its
        # activation resolves to the watcher channel and the printed pickup arms
        # a real watcher. This reverses the earlier ruling that Codex had no
        # native session watcher; it has one, and that watcher delivered the
        # app-server proof on 2026-08-06.
        activation = {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"}
        channel = ncs.wake_channel(activation)
        self.assertEqual("watcher", channel)
        # And it must be the DISPATCHING watcher. watch_inbox.py runs
        # dispatch_autobridge only when --session is absent, so an exact-session
        # command would announce and never start Codex's turn — while the bound
        # binding suppresses AX. Observed, not woken (PR #559 r3725767284).
        self.assertTrue(ncs.needs_dispatch_wake(activation))
        block = "\n".join(ncs.pickup_block(
            channel, "codex", "llm-collab", "CHAT-ABCD1234",
            "SESSION-CODEX", "app", "019f-native", "codex_app",
            ncs.needs_dispatch_wake(activation)))
        # Assert on the COMMAND lines, not the block: the explanatory comment
        # legitimately contains the string "--session".
        command = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))
        # The MANAGED singleton, not a raw poller: pm2 already runs one watcher
        # per watcher_enabled agent, and a second agent-wide poller would
        # double-dispatch, since dispatch_session reads processed_messages
        # before invoking the runtime and records the path after
        # (PR #559 r3725819269).
        self.assertIn("pm2_watchers.py ensure --agent codex", command)
        self.assertNotIn("watch_inbox.py", command)
        # Transport setup now precedes registration, so pickup only ensures the
        # dispatching watcher and cannot repeat transport setup too late.
        cmds = [l.strip() for l in command.splitlines() if l.strip()]
        self.assertEqual(
            [f"{ncs.LAUNCH} pm2_watchers.py ensure --agent codex"],
            cmds,
            "pickup must only ensure the watcher after transport and registration",
        )
        self.assertNotIn("--session", command)
        self.assertNotIn("NO native session watcher", block)

    def test_self_reading_worker_still_gets_the_exact_session_watcher(self):
        # Claude reads its own inbox on the announcement, so the exact-session
        # observer is correct for it and must not be widened to agent-wide.
        activation = {"type": "cli_session", "watcher_enabled": True}
        self.assertFalse(ncs.needs_dispatch_wake(activation))
        block = "\n".join(ncs.pickup_block(
            ncs.wake_channel(activation), "claude", "llm-collab", "CHAT-ABCD1234",
            "SESSION-CLAUDE", "app", "019f-native", "claude_app",
            ncs.needs_dispatch_wake(activation)))
        self.assertIn("--session SESSION-CLAUDE", block)

    def test_watcher_pickup_arms_a_watcher(self):
        block = "\n".join(ncs.pickup_block(
            "watcher", "claude", "llm-collab", "CHAT-ABCD1234",
            "SESSION-CLAUDE", "app", "3db9-native", "claude_app"))
        self.assertIn("watch_inbox.py", block)
        self.assertIn("export LLM_COLLAB_READER_RUNTIME_FAMILY=claude_app", block)


class DirtyCheckoutGuardTest(unittest.TestCase):
    def _run_guard(self, *, head, origin, dirty):
        def fake_git(*args, timeout=None):
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
             patch.object(ncs, "get_project", return_value={"repos": {"app": "."}}), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS), \
             patch.object(ncs, "preflight_starter_binding", return_value=None), \
             patch.object(ncs, "register_session", return_value=None), \
             patch.object(ncs, "subprocess") as sub:
            sub.run.return_value = type("R", (), {"returncode": 0, "stdout": '{"chat_id": "CHAT-TEST9999"}', "stderr": ""})()
            with redirect_stdout(out):
                ncs.main()
            return out.getvalue(), sub

    def test_codex_initiator_gets_a_watcher(self):
        # Contract v12: a Codex initiator arms the routine watcher like anyone
        # else. The old expectation (poll, await AX) taught every new Codex
        # collaboration session the routing model v12 retired.
        out, _ = self._main([
            "--project", "p", "--title", "t", "--me", "codex",
            "--my-runtime-session-id", "019f-x", "--my-runtime-family", "codex_app",
            "--with", "claude:claude_app", "--repo-target", "app", "--skip-currency-check",
        ])
        # initiator (codex) section is before the coworker section.
        initiator = out.split("SETUP PROMPT")[0]
        # The managed singleton, not a raw per-chat poller (r3725819269).
        self.assertIn("pm2_watchers.py ensure --agent codex", initiator)
        self.assertNotIn("NO native session watcher", initiator)

    def test_codex_initiator_ensures_transport_before_registration(self):
        import io
        from contextlib import redirect_stdout

        calls = []

        def run(cmd, **kwargs):
            if cmd[1].endswith("new_chat.py"):
                return type("R", (), {"returncode": 0, "stdout": '{"chat_id": "CHAT-X", "path": "/tmp/chat-x"}', "stderr": ""})()
            calls.append("transport")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        argv = ["--project", "p", "--title", "t", "--me", "codex",
                "--my-runtime-session-id", "019f-x", "--my-runtime-family", "codex_app",
                "--with", "claude:claude_app", "--repo-target", "app", "--skip-currency-check"]
        with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "get_project", return_value={"repos": {"app": "."}}), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS), \
             patch.object(ncs, "preflight_starter_binding", return_value=None), \
             patch.object(ncs, "register_session", side_effect=lambda *args: calls.append("register")), \
             patch.object(ncs.subprocess, "run", side_effect=run), \
             redirect_stdout(io.StringIO()):
            ncs.main()

        self.assertEqual(["transport", "register"], calls)

    def test_claude_initiator_arms_watcher(self):
        out, _ = self._main([
            "--project", "p", "--title", "t", "--me", "claude",
            "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
            "--with", "codex:codex_app", "--repo-target", "app", "--skip-currency-check",
        ])
        initiator = out.split("SETUP PROMPT")[0]
        self.assertIn("watch_inbox.py", initiator)

    def test_starter_refusal_happens_before_new_chat(self):
        argv = ["--project", "p", "--title", "t", "--me", "claude",
                "--my-runtime-session-id", "starter-native", "--my-runtime-family", "claude_app",
                "--with", "codex:codex_app", "--repo-target", "app", "--skip-currency-check"]
        with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "get_project", return_value={"repos": {"app": "."}}), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS), \
             patch.object(ncs, "preflight_starter_binding",
                          side_effect=SystemExit("starter collision")), \
             patch.object(ncs, "subprocess") as sub:
            with self.assertRaisesRegex(SystemExit, "starter collision"):
                ncs.main()
            sub.run.assert_not_called()

    def test_starter_preflight_uses_the_shared_native_scope_guard(self):
        import session_autobridge

        with patch.object(session_autobridge, "preflight_native_session_registration") as check:
            ncs.preflight_starter_binding(
                agent="claude", project="llm-collab",
                runtime_session_id="starter-native", runtime_family="claude_app",
            )
        check.assert_called_once_with(
            session_id="__pending-new-collab__claude__starter-native",
            project_id="llm-collab", chat_id="__pending-new-collab__claude__starter-native",
            native_session_id="starter-native", native_family="claude_app",
        )

    def test_starter_collision_keeps_actionable_guard_message(self):
        import session_autobridge

        with patch.object(
            session_autobridge,
            "preflight_native_session_registration",
            side_effect=session_autobridge.NativeSessionOwnedElsewhere(
                "deactivate the other lease or use a fresh native session"
            ),
        ), self.assertRaisesRegex(
            SystemExit, "deactivate the other lease or use a fresh native session"
        ):
            ncs.preflight_starter_binding(
                agent="claude", project="llm-collab",
                runtime_session_id="starter-native", runtime_family="claude_app",
            )

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
             patch.object(ncs, "get_project", return_value={"repos": {"app": "."}}), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS), \
             patch.object(ncs, "preflight_starter_binding", return_value=None), \
             patch.object(ncs, "register_session", side_effect=RuntimeError("boom")), \
             patch.object(ncs, "shutil") as sh, \
             patch.object(ncs, "subprocess") as sub:
            sub.run.return_value = type("R", (), {"returncode": 0, "stdout": '{"chat_id": "CHAT-X", "path": "/tmp/chat-x"}', "stderr": ""})()
            with self.assertRaises(SystemExit):
                ncs.main()
            sh.rmtree.assert_called_once()  # orphan chat rolled back

    def test_pm2_start_must_succeed_and_be_online_before_registration(self):
        import pm2_watchers

        argv = ["--project", "p", "--title", "t", "--me", "codex",
                "--my-runtime-session-id", "019f-x", "--my-runtime-family", "codex_app",
                "--with", "claude:claude_app", "--repo-target", "app", "--skip-currency-check"]

        cases = {
            "start failed": [
                type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
                type("R", (), {"returncode": 7, "stdout": "", "stderr": ""})(),
                type("R", (), {"returncode": 0, "stdout": "online", "stderr": ""})(),
            ],
            "start returned zero but app stayed offline": [
                type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
                type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
                type("R", (), {"returncode": 0, "stdout": "stopped", "stderr": ""})(),
            ],
        }
        for label, pm2_results in cases.items():
            with self.subTest(label):
                results = iter(pm2_results)

                def run(cmd, **kwargs):
                    if cmd[1].endswith("new_chat.py"):
                        return type("R", (), {"returncode": 0, "stdout": '{"chat_id": "CHAT-X", "path": "/tmp/chat-x"}', "stderr": ""})()
                    try:
                        with patch.object(sys, "argv", ["pm2_watchers.py", "ensure", "--agent", "codex-appserver"]), \
                             patch.object(pm2_watchers, "agent_ids", return_value=["codex"]), \
                             patch.object(pm2_watchers, "enabled_sidecar_ids", return_value=["codex-appserver"]), \
                             patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                             patch.object(pm2_watchers, "pm2_run", side_effect=results):
                            pm2_watchers.main()
                    except SystemExit as exc:
                        return type("R", (), {"returncode": exc.code, "stdout": "", "stderr": "transport unavailable"})()
                    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

                with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
                     patch.object(ncs, "ensure_project", return_value=None), \
                     patch.object(ncs, "get_project", return_value={"repos": {"app": "."}}), \
                     patch.object(ncs, "load_agents", return_value=self.AGENTS), \
                     patch.object(ncs, "preflight_starter_binding", return_value=None), \
                     patch.object(ncs, "register_session") as register, \
                     patch.object(ncs, "shutil") as sh, \
                     patch.object(ncs.subprocess, "run", side_effect=run):
                    with self.assertRaisesRegex(SystemExit, "transport unavailable"):
                        ncs.main()

                register.assert_not_called()
                sh.rmtree.assert_called_once_with("/tmp/chat-x", ignore_errors=True)

    def test_transport_timeout_rolls_back_instead_of_hanging(self):
        import threading

        argv = ["--project", "p", "--title", "t", "--me", "codex",
                "--my-runtime-session-id", "019f-x", "--my-runtime-family", "codex_app",
                "--with", "claude:claude_app", "--repo-target", "app", "--skip-currency-check"]
        seen_timeouts = []

        def run(cmd, **kwargs):
            if cmd[1].endswith("new_chat.py"):
                return type("R", (), {"returncode": 0, "stdout": '{"chat_id": "CHAT-X", "path": "/tmp/chat-x"}', "stderr": ""})()
            timeout = kwargs.get("timeout")
            seen_timeouts.append(timeout)
            threading.Event().wait(0.01)  # fake a blocked token read without a real hung mount
            if timeout is None:
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "get_project", return_value={"repos": {"app": "."}}), \
             patch.object(ncs, "load_agents", return_value=self.AGENTS), \
             patch.object(ncs, "preflight_starter_binding", return_value=None), \
             patch.object(ncs, "register_session") as register, \
             patch.object(ncs, "shutil") as sh, \
             patch.object(ncs.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(SystemExit, "timed out after 50s"):
                ncs.main()

        self.assertEqual([ncs.CODEX_TRANSPORT_ENSURE_TIMEOUT_SECONDS], seen_timeouts)
        register.assert_not_called()
        sh.rmtree.assert_called_once_with("/tmp/chat-x", ignore_errors=True)

    def _run_expect_exit(self, argv, agents, *, get_project_ret={"repos": {"app": "."}}):
        with patch.object(sys, "argv", ["new_collab_session.py", *argv]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "get_project", return_value=get_project_ret), \
             patch.object(ncs, "load_agents", return_value=agents), \
             patch.object(ncs, "preflight_starter_binding", return_value=None), \
             patch.object(ncs, "register_session", return_value=None), \
             patch.object(ncs, "subprocess") as sub:
            with self.assertRaises(SystemExit):
                ncs.main()
            return sub

    def test_human_relay_coworker_is_refused_before_chat(self):
        # GH-469 P1: a human_relay identity has no native session, so --with
        # zcode:claude_app must exit before new_chat.py, never registering it.
        agents = self.AGENTS + [{"id": "zcode", "activation": {"type": "human_relay"}}]
        sub = self._run_expect_exit(
            ["--project", "p", "--title", "t", "--me", "claude",
             "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
             "--with", "zcode:claude_app", "--repo-target", "app", "--skip-currency-check"],
            agents)
        sub.run.assert_not_called()

    def test_attended_only_coworker_is_refused_before_chat(self):
        # GH-469 P1: an attended-only identity cannot be autonomously registered.
        agents = self.AGENTS + [{"id": "att", "activation": {"ax_attended_only": True, "ax_app": "X"}}]
        sub = self._run_expect_exit(
            ["--project", "p", "--title", "t", "--me", "claude",
             "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
             "--with", "att:claude_app", "--repo-target", "app", "--skip-currency-check"],
            agents)
        sub.run.assert_not_called()

    def test_non_native_initiator_is_refused_before_chat(self):
        # GH-469 P1 (initiator bypass): a non-native --me must be refused before
        # new_chat.py too, not only coworkers.
        agents = self.AGENTS + [{"id": "zcode", "activation": {"type": "human_relay"}}]
        sub = self._run_expect_exit(
            ["--project", "p", "--title", "t", "--me", "zcode",
             "--my-runtime-session-id", "z", "--my-runtime-family", "claude_app",
             "--with", "codex:codex_app", "--repo-target", "app", "--skip-currency-check"],
            agents)
        sub.run.assert_not_called()

    def test_bogus_ax_app_coworker_is_refused(self):
        # GH-469 P1 (wake_channel too loose): an agent with a NON-routine ax_app and
        # no watcher would be classed ax_doorbell by wake_channel, but is not a
        # registerable native session — the routine-doorbell allowlist must refuse it.
        agents = self.AGENTS + [{"id": "botx", "activation": {"type": "cli_session", "ax_app": "RandomApp"}}]
        sub = self._run_expect_exit(
            ["--project", "p", "--title", "t", "--me", "claude",
             "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
             "--with", "botx:claude_app", "--repo-target", "app", "--skip-currency-check"],
            agents)
        sub.run.assert_not_called()

    def test_watcher_backed_attended_coworker_is_accepted(self):
        # GH-469: ax_attended_only disables only the routine AX doorbell; a
        # watcher-backed agent still has a native session (watcher precedence), so
        # it must NOT be refused.
        import io
        from contextlib import redirect_stdout
        agents = self.AGENTS + [{"id": "watk", "activation": {
            "type": "cli_session", "watcher_enabled": True, "ax_attended_only": True}}]
        out = io.StringIO()
        with patch.object(sys, "argv", ["new_collab_session.py",
                "--project", "p", "--title", "t", "--me", "claude",
                "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
                "--with", "watk:claude_app", "--repo-target", "app", "--skip-currency-check"]), \
             patch.object(ncs, "ensure_project", return_value=None), \
             patch.object(ncs, "get_project", return_value={"repos": {"app": "."}}), \
             patch.object(ncs, "load_agents", return_value=agents), \
             patch.object(ncs, "preflight_starter_binding", return_value=None), \
             patch.object(ncs, "register_session", return_value=None), \
             patch.object(ncs, "subprocess") as sub:
            sub.run.return_value = type("R", (), {"returncode": 0, "stdout": '{"chat_id": "CHAT-TEST9999"}', "stderr": ""})()
            with redirect_stdout(out):
                ncs.main()
            sub.run.assert_called()  # accepted -> chat created, not refused

    def test_unknown_repo_target_is_refused_before_chat(self):
        # GH-469 P2: a --repo-target not in the project's configured repos exits
        # before chat creation.
        sub = self._run_expect_exit(
            ["--project", "p", "--title", "t", "--me", "claude",
             "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
             "--with", "codex:codex_app", "--repo-target", "typo", "--skip-currency-check"],
            self.AGENTS)
        sub.run.assert_not_called()

    def test_configured_repo_target_passes(self):
        # GH-469 P2: a configured repo-target proceeds to chat creation.
        out, sub = self._main([
            "--project", "p", "--title", "t", "--me", "claude",
            "--my-runtime-session-id", "3db9", "--my-runtime-family", "claude_app",
            "--with", "codex:codex_app", "--repo-target", "app", "--skip-currency-check",
        ])
        self.assertIn("chat_id: CHAT-TEST9999", out)

    def test_fetch_timeout_surfaces_currency_refusal_not_a_hang(self):
        # GH-469 P3: the fetch must be bounded by a timeout AND a timeout must
        # become the checkout-currency refusal (SystemExit), not a hang.
        seen = {}

        def fake_git(*args, timeout=None):
            if args[:1] == ("fetch",):
                seen["fetch_timeout"] = timeout
                raise subprocess.TimeoutExpired(cmd="git fetch", timeout=timeout or 0)
            return ""
        with patch.object(ncs, "_git", side_effect=fake_git):
            with self.assertRaises(SystemExit):
                ncs.assert_current_checkout()
        self.assertIsNotNone(seen.get("fetch_timeout"),
                             "the origin/main fetch must be bounded by a timeout")

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
