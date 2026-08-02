"""Self-check for bin/pr_watch.py's pure delta logic (no network)."""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_oversized_response_fails_closed(self):
        fake = MagicMock(returncode=0,
                         stdout=json.dumps([[] for _ in range(self.pw.MAX_PAGES + 1)]))
        with patch.object(self.pw.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError):
                self.pw._gh_pages("repos/x/y/issues/1/timeline")

    def test_repo_is_required(self):
        # A worker must never fall back to a default repo.
        with patch.object(sys, "argv", ["pr_watch.py", "--pr", "1"]):
            with self.assertRaises(SystemExit):
                self.pw.main()


if __name__ == "__main__":
    unittest.main()
