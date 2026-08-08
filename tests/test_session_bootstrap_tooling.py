"""Tooling currency is a gate, not a status line (GH-369).

A checkout pinned behind main does not fail loudly — it runs the *absence* of
merged work as if it were the contract. On 2026-07-28 one accepted `inbox.py
--session` and ignored it, so a watcher believed it was session-bound, was not,
and lost five packets before anyone noticed. The version line bootstrap already
printed said `version 4` against a v5 main and was read as trivia.

These cases pin the four states and, most importantly, that a stale checkout
*refuses* rather than reporting.
"""

from __future__ import annotations
import sys as _grsys; from pathlib import Path as _grPath
_grsys.path.insert(0, str(_grPath(__file__).resolve().parent)); import _runtime_gate_testkit  # noqa: E402,F401  GH-503: deterministic gate-bypass install (any run form)

import json
import os
import subprocess
import tempfile
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import session_bootstrap
import _session_autobridge as session_autobridge_lib


class SessionBootstrapArgumentTest(unittest.TestCase):
    def parse_limit(self, value: str):
        with patch.object(
            sys,
            "argv",
            ["session_bootstrap.py", "--agent", "claude", "--limit", value],
        ):
            return session_bootstrap.parse_args()

    def test_negative_limit_is_rejected_during_argument_parsing(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            self.parse_limit("-1")

        self.assertEqual(2, raised.exception.code)
        self.assertIn("argument --limit: must be a positive integer", stderr.getvalue())
        self.assertNotIn("No unread messages", stderr.getvalue())

    def test_positive_limit_is_accepted(self) -> None:
        self.assertEqual(5, self.parse_limit("5").limit)

    def test_zero_limit_is_rejected_during_argument_parsing(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            self.parse_limit("0")

        self.assertEqual(2, raised.exception.code)
        self.assertIn("argument --limit: must be a positive integer", stderr.getvalue())


def completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


def git_responses(*, is_ancestor: int, behind: str = "0", have_origin: bool = True):
    """Stand in for the git calls tooling_currency() makes, in argument order."""

    def fake(*args, timeout=15):
        if args[0] == "fetch":
            return completed(0)
        if args[0] == "rev-parse" and args[-1] == "origin/main":
            return completed(0, "b1c55c9e69\n") if have_origin else completed(128)
        if args[0] == "merge-base":
            return completed(is_ancestor)
        if args[0] == "rev-list":
            return completed(0, f"{behind}\n")
        if args[0] == "rev-parse" and "--short" in args:
            return completed(0, "76f3670\n")
        if args[0] == "rev-parse" and "--abbrev-ref" in args:
            return completed(0, "claude/gh326-resume-prompt-pointer\n")
        return completed(0, "")

    return fake


class ToolingCurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        # A real temp checkout marker, not a global Path.exists patch: patching
        # Path.exists leaks into every other module under `unittest discover`
        # and broke five unrelated bootstrap cases when this file first ran.
        self.temp = tempfile.TemporaryDirectory(prefix="llm-collab-tooling-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / ".git").mkdir()
        patcher = patch.object(session_bootstrap, "ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_checkout_containing_origin_main_is_current(self) -> None:
        with patch.object(session_bootstrap, "_git", side_effect=git_responses(is_ancestor=0)):
            result = session_bootstrap.tooling_currency()
        self.assertEqual(session_bootstrap.TOOLING_CURRENT, result["state"])

    def test_a_checkout_that_cannot_reach_origin_main_is_stale(self) -> None:
        """The real 2026-07-28 shape: a lane branch 24 commits behind main."""
        with patch.object(
            session_bootstrap, "_git", side_effect=git_responses(is_ancestor=1, behind="8")
        ):
            result = session_bootstrap.tooling_currency()
        self.assertEqual(session_bootstrap.TOOLING_STALE, result["state"])
        self.assertEqual(8, result["commits_behind"])
        self.assertEqual("claude/gh326-resume-prompt-pointer", result["branch"])

    def test_a_branch_ahead_of_main_is_current_not_stale(self) -> None:
        """Ancestry, not equality. Every lane works ahead of main by construction,
        and a gate that called that stale would be disabled within a day."""
        with patch.object(session_bootstrap, "_git", side_effect=git_responses(is_ancestor=0)):
            result = session_bootstrap.tooling_currency()
        self.assertEqual(session_bootstrap.TOOLING_CURRENT, result["state"])

    def test_a_missing_origin_ref_is_unknown_never_a_silent_pass(self) -> None:
        with patch.object(
            session_bootstrap, "_git", side_effect=git_responses(is_ancestor=0, have_origin=False)
        ):
            result = session_bootstrap.tooling_currency()
        self.assertEqual(session_bootstrap.TOOLING_UNKNOWN, result["state"])
        self.assertNotEqual(session_bootstrap.TOOLING_CURRENT, result["state"])

    def test_an_unreachable_origin_still_compares_and_says_so(self) -> None:
        """Offline must not mean unguarded: compare against the last fetched ref
        and label the answer, rather than skipping the check."""

        def fake(*args, timeout=15):
            if args[0] == "fetch":
                return completed(1)
            return git_responses(is_ancestor=1, behind="3")(*args, timeout=timeout)

        with patch.object(session_bootstrap, "_git", side_effect=fake):
            result = session_bootstrap.tooling_currency()
        self.assertEqual(session_bootstrap.TOOLING_STALE, result["state"])
        self.assertFalse(result["fetched"])

    def test_an_ancestry_command_error_is_unknown_not_stale(self) -> None:
        """git reserves exit 1 for "not an ancestor". Anything else — 128 on a
        broken or partial repository — means the question was never answered, and
        folding it into `stale` blocks bootstrap on a verdict git did not give.
        """
        with patch.object(
            session_bootstrap, "_git", side_effect=git_responses(is_ancestor=128)
        ):
            result = session_bootstrap.tooling_currency()
        self.assertEqual(session_bootstrap.TOOLING_UNKNOWN, result["state"])
        self.assertNotIn("commits_behind", result)
        self.assertIn("128", result["reason"])

    def test_exit_one_is_still_stale(self) -> None:
        """The sibling of the case above: separating error from answer must not
        cost the one exit code that is a real negative answer."""
        with patch.object(
            session_bootstrap, "_git", side_effect=git_responses(is_ancestor=1, behind="24")
        ):
            result = session_bootstrap.tooling_currency()
        self.assertEqual(session_bootstrap.TOOLING_STALE, result["state"])

    def test_a_git_failure_is_unknown_rather_than_an_exception(self) -> None:
        with patch.object(session_bootstrap, "_git", return_value=None):
            result = session_bootstrap.tooling_currency()
        self.assertEqual(session_bootstrap.TOOLING_UNKNOWN, result["state"])


class StaleBootstrapRefusesTest(unittest.TestCase):
    """The gate itself: reporting staleness is what already failed."""

    def run_bootstrap(self, currency: dict, *, extra_argv: list[str]) -> tuple[int, str]:
        argv = ["session_bootstrap.py", "--agent", "claude", "--json", *extra_argv]
        out: list[str] = []
        with patch.object(sys, "argv", argv):
            with patch.object(session_bootstrap, "tooling_currency", return_value=currency):
                with patch.object(session_bootstrap, "agent_ids", return_value=["claude"]):
                    with patch.object(session_bootstrap, "get_agent", return_value={"id": "claude"}):
                        with patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(str(x) for x in a))):
                            try:
                                session_bootstrap.main()
                            except SystemExit as exit_error:
                                return int(exit_error.code or 0), "\n".join(out)
        return 0, "\n".join(out)

    STALE = {
        "state": "stale",
        "head": "76f3670",
        "branch": "claude/gh326-resume-prompt-pointer",
        "origin_main": "e421f90",
        "fetched": True,
        "commits_behind": 8,
    }

    def test_stale_tooling_refuses_before_any_inbox_or_watcher_work(self) -> None:
        code, out = self.run_bootstrap(self.STALE, extra_argv=[])
        self.assertEqual(1, code)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual("refused", payload["bootstrap"])
        self.assertEqual("stale", payload["tooling"]["state"])

    def test_the_override_proceeds_and_is_recorded(self) -> None:
        """A pinned checkout is legitimate. Proceeding *unknowingly* is not, so the
        override must leave a trace rather than silence the gate."""
        code, _ = self.run_bootstrap(self.STALE, extra_argv=["--allow-stale-tooling"])
        self.assertEqual(0, code)

    def test_unknown_currency_does_not_refuse(self) -> None:
        """A host without git or origin must still be able to work; it is told the
        answer is unverified rather than blocked on an unanswerable question."""
        code, _ = self.run_bootstrap(
            {"state": "unknown", "reason": "not a git checkout"}, extra_argv=[]
        )
        self.assertEqual(0, code)


class StaleAnnouncementTest(unittest.TestCase):
    def announce(self, currency: dict, *, allowed: bool) -> str:
        out: list[str] = []
        with patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(str(x) for x in a))):
            session_bootstrap.announce_tooling(currency, allowed=allowed)
        return "\n".join(out)

    def test_the_stale_banner_names_the_failure_mode_not_just_the_state(self) -> None:
        """'You are behind' invites a shrug. The reason this is dangerous is that
        an unimplemented flag is accepted and ignored, so the tool looks healthy."""
        text = self.announce(StaleBootstrapRefusesTest.STALE, allowed=False)
        self.assertIn("STALE TOOLING", text)
        self.assertIn("accepted and ignored", text)
        self.assertIn("claude/gh326-resume-prompt-pointer", text)
        self.assertIn("8 commit(s) behind", text)

    def test_an_unreachable_origin_is_disclosed_in_the_banner(self) -> None:
        stale_offline = {**StaleBootstrapRefusesTest.STALE, "fetched": False}
        self.assertIn("origin unreachable", self.announce(stale_offline, allowed=False))

    def test_an_offline_pass_is_not_announced_like_a_fetched_pass(self) -> None:
        """A `current` computed against a cached ref carries less assurance than one
        computed against the remote. Printing them identically is how a checkout
        proceeds unknowingly behind main — the failure this gate exists to stop.
        """
        online = {"state": "current", "head": "e421f90", "origin_main": "e421f90", "fetched": True}
        offline = {**online, "fetched": False}

        online_text = self.announce(online, allowed=False)
        offline_text = self.announce(offline, allowed=False)

        self.assertNotEqual(online_text, offline_text)
        self.assertIn("last fetched", offline_text)
        self.assertIn("may have moved", offline_text)
        self.assertNotIn("may have moved", online_text)

    def test_the_override_banner_says_results_are_bound_to_old_tooling(self) -> None:
        text = self.announce(StaleBootstrapRefusesTest.STALE, allowed=True)
        self.assertIn("--allow-stale-tooling was passed", text)
        self.assertIn("including anything you report", text)


class WatcherReloadTest(unittest.TestCase):
    def test_start_watcher_reloads_the_deployed_ecosystem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            watcher = root / "bin" / "pm2_watchers.py"
            watcher.parent.mkdir()
            watcher.touch()
            with (
                patch.object(session_bootstrap, "ROOT", root),
                patch.object(
                    session_bootstrap.subprocess,
                    "run",
                    return_value=completed(),
                ) as run,
            ):
                self.assertEqual({"status": "ok"}, session_bootstrap.start_watcher("claude"))

        self.assertEqual("restart", run.call_args.args[0][2])


class BindingDriftBannerTest(unittest.TestCase):
    SESSION = {
        "session_id": "SESSION-CLAUDE-OLD",
        "agent_id": "claude",
        "project_id": "llm-collab",
        "chat_id": "CHAT-DRIFT",
        "mode": "notify",
        "status": "active",
        "wake_strategy": "runtime_trigger",
        "repo_targets": ["app"],
        "runtime": {
            "family": "claude_app",
            "session_id": "runtime-old",
            "home": "/tmp/claude-home",
        },
    }

    def test_bootstrap_diagnoses_mismatch_without_unsafe_repair_command(self) -> None:
        mismatch = session_bootstrap.CanonicalBindingNativeMismatch(
            canonical_native_session_id="runtime-old",
            requested_runtime_session_id="runtime-new",
        )
        output = StringIO()
        ax = SimpleNamespace(as_dict=lambda: {})
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "runtime-new"}, clear=True),
            patch.object(sys, "argv", ["session_bootstrap.py", "--agent", "claude", "--no-watcher"]),
            patch.object(session_bootstrap, "agent_ids", return_value=["claude"]),
            patch.object(
                session_bootstrap,
                "get_agent",
                return_value={"id": "claude", "activation": {"watcher_enabled": False}},
            ),
            patch.object(
                session_bootstrap,
                "tooling_currency",
                return_value={"state": "current", "head": "abc", "fetched": True},
            ),
            patch.object(session_bootstrap, "announce_tooling"),
            patch.object(session_bootstrap, "dependency_report", return_value={}),
            patch.object(session_bootstrap, "announce_dependencies"),
            patch.object(session_bootstrap, "announce_contract"),
            patch.object(
                session_bootstrap,
                "agent_identity_path",
                return_value=Path("/definitely/missing/identity.md"),
            ),
            patch.object(session_bootstrap, "probe_ax_trust", return_value=ax),
            patch.object(session_bootstrap, "format_ax_status", return_value="[ax] skipped"),
            patch.object(
                session_bootstrap, "get_unread_messages", return_value=[]
            ) as get_unread,
            patch.object(session_bootstrap, "queue_summaries", return_value=[]),
            patch.object(session_bootstrap, "iter_sessions", return_value=[self.SESSION]),
            patch.object(
                session_bootstrap,
                "resolve_active_canonical_binding",
                side_effect=mismatch,
            ),
            redirect_stdout(output),
        ):
            session_bootstrap.main()

        text = output.getvalue()
        self.assertIn("BINDING DRIFT", text)
        self.assertIn("No self-service repair exists yet", text)
        self.assertNotIn("session_autobridge.py register", text)
        self.assertNotIn("--supersedes-session", text)
        get_unread.assert_called_once_with("claude", limit=5)

    def test_matching_runtime_is_silent(self) -> None:
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "runtime-current"}, clear=True),
            patch.object(session_bootstrap, "iter_sessions", return_value=[self.SESSION]),
            patch.object(
                session_bootstrap,
                "resolve_active_canonical_binding",
                return_value={"binding_id": "binding-current"},
            ),
        ):
            drifts = session_bootstrap.binding_drifts("claude")
        self.assertEqual("clear", drifts["status"])
        output = StringIO()
        with redirect_stdout(output):
            session_bootstrap.announce_binding_drifts(drifts)
        self.assertEqual("", output.getvalue())

    def test_multiple_peer_bindings_ask_for_scope_without_targeting_peers(self) -> None:
        peer = {
            **self.SESSION,
            "session_id": "SESSION-CLAUDE-PEER",
            "project_id": "amiga",
            "chat_id": "CHAT-PEER",
            "runtime": {**self.SESSION["runtime"], "session_id": "runtime-peer"},
        }
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "runtime-new"}, clear=True),
            patch.object(session_bootstrap, "iter_sessions", return_value=[self.SESSION, peer]),
            patch.object(session_bootstrap, "resolve_active_canonical_binding") as resolve,
        ):
            report = session_bootstrap.binding_drifts("claude")
        self.assertEqual("ambiguous", report["status"])
        resolve.assert_not_called()
        output = StringIO()
        with redirect_stdout(output):
            session_bootstrap.announce_binding_drifts(report)
        text = output.getvalue()
        self.assertIn("Which project/chat owns this restarting session?", text)
        self.assertNotIn("--supersedes-session", text)
        self.assertNotIn("SESSION-CLAUDE-OLD", text)
        self.assertNotIn("SESSION-CLAUDE-PEER", text)

    def test_logical_session_correlates_one_scope_without_targeting_its_peer(self) -> None:
        peer = {
            **self.SESSION,
            "session_id": "SESSION-CLAUDE-PEER",
            "project_id": "amiga",
            "chat_id": "CHAT-PEER",
            "runtime": {**self.SESSION["runtime"], "session_id": "runtime-peer"},
        }
        mismatch = session_bootstrap.CanonicalBindingNativeMismatch(
            canonical_native_session_id="runtime-old",
            requested_runtime_session_id="runtime-new",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "CLAUDE_CODE_SESSION_ID": "runtime-new",
                    "LLM_COLLAB_SESSION_ID": "SESSION-CLAUDE-OLD",
                },
                clear=True,
            ),
            patch.object(session_bootstrap, "iter_sessions", return_value=[self.SESSION, peer]),
            patch.object(
                session_bootstrap,
                "resolve_active_canonical_binding",
                side_effect=mismatch,
            ) as resolve,
        ):
            report = session_bootstrap.binding_drifts("claude")
        self.assertEqual("detected", report["status"])
        resolve.assert_called_once_with(
            "llm-collab", "CHAT-DRIFT", "claude", "runtime-new", strict=True
        )
        self.assertNotIn("SESSION-CLAUDE-PEER", json.dumps(report))
        self.assertNotIn("repair_command", report)

    def test_truncated_session_scan_is_explicitly_unavailable(self) -> None:
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "runtime-new"}, clear=True),
            patch.object(
                session_bootstrap,
                "iter_sessions",
                side_effect=session_bootstrap.UnreadableFile(
                    "session records exceed the 5000 entry limit"
                ),
            ),
        ):
            report = session_bootstrap.binding_drifts("claude")
        self.assertEqual("unavailable", report["status"])
        self.assertIn("entry limit", report["reason"])
        output = StringIO()
        with redirect_stdout(output):
            session_bootstrap.announce_binding_drifts(report)
        self.assertIn("CHECK UNAVAILABLE", output.getvalue())
        self.assertNotEqual([], report)

    def test_unreadable_canonical_ledger_is_explicitly_unavailable(self) -> None:
        class FakePaths:
            @staticmethod
            def derive(*_args):
                return object()

        class BrokenStore:
            @staticmethod
            def open_reader(_paths):
                raise OSError("canonical ledger is corrupt")

        fake_ledger = SimpleNamespace(LedgerPaths=FakePaths, LedgerStore=BrokenStore)
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "runtime-new"}, clear=True),
            patch.object(session_bootstrap, "iter_sessions", return_value=[self.SESSION]),
            patch.object(session_autobridge_lib, "config_get", return_value="ws_alpha"),
            patch.object(session_autobridge_lib, "project_state_root", return_value=Path("/tmp")),
            patch.object(
                session_autobridge_lib.importlib,
                "import_module",
                return_value=fake_ledger,
            ),
        ):
            report = session_bootstrap.binding_drifts("claude")
        self.assertEqual("unavailable", report["status"])
        self.assertIn("canonical ledger is corrupt", report["reason"])
        output = StringIO()
        with redirect_stdout(output):
            session_bootstrap.announce_binding_drifts(report)
        self.assertIn("CHECK UNAVAILABLE", output.getvalue())

    def test_missing_canonical_binding_is_a_legitimate_clear_result(self) -> None:
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "runtime-new"}, clear=True),
            patch.object(session_bootstrap, "iter_sessions", return_value=[self.SESSION]),
            patch.object(
                session_bootstrap,
                "resolve_active_canonical_binding",
                return_value=None,
            ) as resolve,
        ):
            report = session_bootstrap.binding_drifts("claude")
        self.assertEqual("clear", report["status"])
        self.assertFalse(report["canonical_binding_resolved"])
        resolve.assert_called_once_with(
            "llm-collab", "CHAT-DRIFT", "claude", "runtime-new", strict=True
        )


if __name__ == "__main__":
    unittest.main()
