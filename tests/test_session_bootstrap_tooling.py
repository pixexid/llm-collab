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

import json
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import session_bootstrap


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

    def test_the_override_banner_says_results_are_bound_to_old_tooling(self) -> None:
        text = self.announce(StaleBootstrapRefusesTest.STALE, allowed=True)
        self.assertIn("--allow-stale-tooling was passed", text)
        self.assertIn("including anything you report", text)


if __name__ == "__main__":
    unittest.main()
