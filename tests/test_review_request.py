"""review_request.py must make a hand-typed SHA impossible and enforce the request budget.

The exact-head SHA is what every terminal review signal binds to, so the tool
takes it only from GitHub and the local checkout — there is deliberately no
--sha option (PR #347 contained a fabricated, retracted SHA). The budget is one
initial request per head plus the single exempted re-trigger.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import review_request  # noqa: E402

SHA = "a" * 40
OTHER_SHA = "b" * 40


def fake_run(mapping):
    prefixes = sorted(mapping, key=len, reverse=True)

    def _run(argv, capture_output=False, text=False):
        for prefix in prefixes:
            if argv[: len(prefix)] == list(prefix):
                return subprocess.CompletedProcess(argv, 0, stdout=mapping[prefix], stderr="")
        raise AssertionError(f"unexpected argv: {argv}")

    return _run


class RequestBodyTest(unittest.TestCase):
    def test_body_names_focus_and_exact_head(self):
        body = review_request.build_request_body(
            "authority selection, bounded reads", SHA
        )
        self.assertTrue(body.startswith("@codex review for authority selection, bounded reads"))
        self.assertIn(f"at exact head `{SHA}`.", body)

    def test_body_references_lane_contract_when_given(self):
        body = review_request.build_request_body("lenses", SHA, contract=349)
        self.assertIn("lane contract in #349", body)

    def test_empty_focus_is_rejected(self):
        with self.assertRaises(SystemExit):
            review_request.build_request_body("   ", SHA)


class PriorRequestCountTest(unittest.TestCase):
    def test_counts_only_requests_naming_this_head(self):
        bodies = [
            f"@codex review for lenses at exact head `{SHA}`.",
            f"@codex review for lenses at exact head `{OTHER_SHA}`.",
            "unrelated comment mentioning @codex review nowhere",
            f"quoted text\n@codex review not at line start {SHA}",
        ]
        self.assertEqual(review_request.count_prior_requests(bodies, SHA), 1)

    def test_budget_allows_initial_and_one_retrigger_only(self):
        self.assertIsNone(review_request.refusal_reason(0, retrigger=False))
        self.assertIsNotNone(review_request.refusal_reason(1, retrigger=False))
        self.assertIsNone(review_request.refusal_reason(1, retrigger=True))
        self.assertIsNotNone(review_request.refusal_reason(2, retrigger=True))


class MainFlowTest(unittest.TestCase):
    def gh_mapping(self, comments):
        return {
            ("gh", "pr", "view", "1", "--json", "comments"): json.dumps(comments),
            ("gh", "pr", "view"): json.dumps({"headRefOid": SHA}),
            ("git", "rev-parse"): SHA + "\n",
        }

    def test_posts_request_when_heads_match_and_budget_free(self):
        posted = []

        def _run(argv, capture_output=False, text=False):
            if argv[:3] == ["gh", "pr", "comment"]:
                posted.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return fake_run(self.gh_mapping([]))(argv, capture_output, text)

        with mock.patch.object(review_request.subprocess, "run", side_effect=_run):
            self.assertEqual(review_request.main(["--pr", "1", "--focus", "lenses"]), 0)
        self.assertEqual(len(posted), 1)
        self.assertIn(SHA, posted[0])

    def test_refuses_when_local_head_differs(self):
        mapping = self.gh_mapping([])
        mapping[("git", "rev-parse")] = OTHER_SHA + "\n"
        with mock.patch.object(
            review_request.subprocess, "run", side_effect=fake_run(mapping)
        ):
            with self.assertRaises(SystemExit) as ctx:
                review_request.main(["--pr", "1", "--focus", "lenses"])
        self.assertIn("push first", str(ctx.exception))

    def test_refuses_second_initial_request_for_same_head(self):
        comments = [f"@codex review for lenses at exact head `{SHA}`."]
        posted = []

        def _run(argv, capture_output=False, text=False):
            if argv[:3] == ["gh", "pr", "comment"]:
                posted.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return fake_run(self.gh_mapping(comments))(argv, capture_output, text)

        with mock.patch.object(review_request.subprocess, "run", side_effect=_run):
            with self.assertRaises(SystemExit) as ctx:
                review_request.main(["--pr", "1", "--focus", "lenses"])
        self.assertIn("--retrigger", str(ctx.exception))
        self.assertEqual(posted, [])

    def test_retrigger_posts_once_but_never_twice(self):
        one = [f"@codex review for lenses at exact head `{SHA}`."]
        two = one * 2
        posted = []

        def _run(argv, capture_output=False, text=False):
            if argv[:3] == ["gh", "pr", "comment"]:
                posted.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return fake_run(self.gh_mapping(one))(argv, capture_output, text)

        with mock.patch.object(review_request.subprocess, "run", side_effect=_run):
            self.assertEqual(
                review_request.main(["--pr", "1", "--focus", "lenses", "--retrigger"]), 0
            )
        self.assertEqual(len(posted), 1)

        def _run2(argv, capture_output=False, text=False):
            if argv[:3] == ["gh", "pr", "comment"]:
                posted.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return fake_run(self.gh_mapping(two))(argv, capture_output, text)

        with mock.patch.object(review_request.subprocess, "run", side_effect=_run2):
            with self.assertRaises(SystemExit) as ctx:
                review_request.main(["--pr", "1", "--focus", "lenses", "--retrigger"])
        self.assertIn("budget", str(ctx.exception))
        self.assertEqual(len(posted), 1)

    def test_dry_run_posts_nothing(self):
        posted = []

        def _run(argv, capture_output=False, text=False):
            if argv[:3] == ["gh", "pr", "comment"]:
                posted.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return fake_run(self.gh_mapping([]))(argv, capture_output, text)

        with mock.patch.object(review_request.subprocess, "run", side_effect=_run):
            self.assertEqual(
                review_request.main(["--pr", "1", "--focus", "lenses", "--dry-run"]), 0
            )
        self.assertEqual(posted, [])


if __name__ == "__main__":
    unittest.main()
