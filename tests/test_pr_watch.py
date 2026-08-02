"""Self-check for bin/pr_watch.py's pure delta logic (no network)."""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))


def load_pr_watch():
    spec = importlib.util.spec_from_file_location(
        "pr_watch", REPO_ROOT / "bin" / "pr_watch.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sig(**over):
    base = {
        "state": "open", "merged": False, "head": "a" * 40,
        "timeline": [["committed", "a" * 40]], "reactions": [],
        "checks": {"_combined_status": "pending"},
    }
    base.update(over)
    return base


class PrWatchDiffTest(unittest.TestCase):
    def setUp(self):
        self.pw = load_pr_watch()

    def test_no_change_reports_nothing(self):
        old = _sig()
        self.assertEqual([], self.pw.diff(old, _sig(), {"timeline": old["timeline"], "reactions": []}))

    def test_new_reaction_detected(self):
        old = _sig()
        new = _sig(reactions=["bot:+1"])
        changes = self.pw.diff(old, new, {"timeline": new["timeline"], "reactions": []})
        self.assertTrue(any("reaction" in c for c in changes))

    def test_check_conclusion_change_detected(self):
        old = _sig()
        new = _sig(checks={"_combined_status": "pending", "verify": "success"})
        changes = self.pw.diff(old, new, {"timeline": new["timeline"], "reactions": []})
        self.assertTrue(any("checks" in c for c in changes))

    def test_new_timeline_event_detected(self):
        old = _sig()
        new = _sig(timeline=[["committed", "a" * 40], ["reviewed", 123]])
        raw = {"timeline": [{"event": "reviewed", "id": 123, "body": "looks good"}],
               "reactions": []}
        changes = self.pw.diff(old, new, raw)
        self.assertTrue(any("timeline" in c for c in changes))

    def test_merge_detected(self):
        old = _sig()
        new = _sig(state="closed", merged=True)
        changes = self.pw.diff(old, new, {"timeline": new["timeline"], "reactions": []})
        self.assertTrue(any("state" in c for c in changes))

    def test_multi_page_pagination_is_flattened(self):
        # --slurp yields a list of per-page values; array pages must concatenate.
        # (Regression: a single json.loads on non-slurped --paginate output
        # raised 'Extra data' on any multi-page response and killed the watch.)
        pages = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
        self.assertEqual(
            [{"id": 1}, {"id": 2}, {"id": 3}], self.pw._flatten_pages(pages)
        )
        # A single-object endpoint (one page, not a list) is kept as one element.
        self.assertEqual([{"head": {"sha": "x"}}],
                         self.pw._flatten_pages([{"head": {"sha": "x"}}]))

    def test_edited_comment_changes_the_timeline_signature(self):
        # An in-place edit keeps event+id but bumps updated_at; the signature
        # must change so the "ANY update" contract holds.
        before = [{"event": "commented", "id": 7, "updated_at": "t1"}]
        after = [{"event": "commented", "id": 7, "updated_at": "t2"}]
        self.assertNotEqual(
            self.pw._timeline_sig(before), self.pw._timeline_sig(after)
        )

    def test_stalled_gh_call_fails_closed_not_hangs(self):
        # A stalled gh api must raise (poll retries), never block the watch.
        with patch.object(self.pw.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("gh", 30)):
            with self.assertRaises(RuntimeError):
                self.pw._gh_pages("repos/x/y/issues/1/timeline")

    def test_page_budget_enforced_during_pagination(self):
        # Every page comes back full, so more always remains: the budget must be
        # spent BEFORE the check — never more than MAX_PAGES requests — then fail
        # closed rather than paginate on.
        calls = []

        def fake_call(path):
            calls.append(path)
            return json.dumps([{"id": i} for i in range(self.pw.PER_PAGE)])

        with patch.object(self.pw, "_gh_call", side_effect=fake_call):
            with self.assertRaises(RuntimeError):
                self.pw._gh_pages("repos/x/y/issues/1/timeline")
        self.assertEqual(self.pw.MAX_PAGES, len(calls))

    def test_pagination_stops_on_short_page(self):
        pages = [json.dumps([{"id": 1}] * self.pw.PER_PAGE), json.dumps([{"id": 2}])]
        with patch.object(self.pw, "_gh_call", side_effect=pages):
            got = self.pw.gh_array("repos/x/y/issues/1/timeline")
        self.assertEqual(self.pw.PER_PAGE + 1, len(got))

    def test_page_len_across_shapes(self):
        self.assertEqual(3, self.pw._page_len([1, 2, 3]))
        self.assertEqual(2, self.pw._page_len({"check_runs": [1, 2]}))
        self.assertEqual(0, self.pw._page_len({"head": {"sha": "x"}}))

    def test_rerun_check_runs_are_distinct(self):
        # Two runs sharing a name (a re-run) must not collapse: key by name#id so
        # the new run's conclusion is visible in the signature.
        def fake_one(path):
            if path.endswith("/status"):
                return {"state": "success"}
            return {"head": {"sha": "s"}, "state": "open", "merged": False}

        def fake_pages(path):
            if "check-runs" in path:
                return [{"check_runs": [
                    {"name": "verify", "id": 1, "conclusion": "failure"},
                    {"name": "verify", "id": 2, "conclusion": "success"},
                ]}]
            return []

        with patch.object(self.pw, "gh_one", side_effect=fake_one), \
                patch.object(self.pw, "gh_array", side_effect=lambda p: []), \
                patch.object(self.pw, "_gh_pages", side_effect=fake_pages):
            sig, _ = self.pw.snapshot("x/y", "1")
        self.assertIn("verify#1", sig["checks"])
        self.assertIn("verify#2", sig["checks"])
        self.assertEqual("failure", sig["checks"]["verify#1"])
        self.assertEqual("success", sig["checks"]["verify#2"])

    def _snapshot_with_comments(self, n_comments, fetched):
        comments = [{"id": i} for i in range(n_comments)]

        def fake_array(path):
            if path.endswith("issues/1/comments"):
                return comments
            if "comments/" in path and path.endswith("/reactions"):
                fetched.append(path)
            return []

        def fake_one(path):
            if path.endswith("/status"):
                return {"state": "success"}
            return {"head": {"sha": "s"}, "state": "open", "merged": False}

        with patch.object(self.pw, "gh_one", side_effect=fake_one), \
                patch.object(self.pw, "gh_array", side_effect=fake_array), \
                patch.object(self.pw, "_gh_pages", side_effect=lambda p: []):
            return self.pw.snapshot("x/y", "1")

    def test_over_cap_comments_fail_closed_with_no_reaction_fetches(self):
        # Past the cap the poll must fail closed (retryable) rather than silently
        # skip comments — a dropped comment could carry the verdict.
        fetched = []
        with self.assertRaises(RuntimeError):
            self._snapshot_with_comments(self.pw.MAX_REACTION_COMMENTS + 1, fetched)
        self.assertEqual([], fetched)

    def test_at_cap_comments_fetch_all_reactions(self):
        fetched = []
        self._snapshot_with_comments(self.pw.MAX_REACTION_COMMENTS, fetched)
        self.assertEqual(self.pw.MAX_REACTION_COMMENTS, len(fetched))

    def test_repo_is_required(self):
        # A worker must never fall back to a default repo.
        with patch.object(sys, "argv", ["pr_watch.py", "--pr", "1"]):
            with self.assertRaises(SystemExit):
                self.pw.main()


if __name__ == "__main__":
    unittest.main()
