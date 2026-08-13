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


def _review(head, body="reviewed"):
    return {
        "event": "reviewed",
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "commit_id": head,
        "body": body,
    }


def _raw(*, timeline=None, reactions=None, comments=None,
         comment_reactions=None, captured_at="2026-08-12T00:00:00Z"):
    return {
        "timeline": [] if timeline is None else timeline,
        "reactions": [] if reactions is None else reactions,
        "comments": [] if comments is None else comments,
        "comment_reactions": (
            [] if comment_reactions is None else comment_reactions
        ),
        "captured_at": captured_at,
    }


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

    def test_connector_review_oids_bind_reviews_to_their_commits(self):
        timeline = [
            {"event": "reviewed", "user": {"login": "chatgpt-codex-connector[bot]"},
             "commit_id": "head-1"},
            {"event": "reviewed", "user": {"login": "pixexid"}, "commit_id": "human-1"},
            {"event": "reviewed", "actor": {"login": "chatgpt-codex-connector"},
             "commit_id": "head-2"},
        ]
        self.assertEqual(["head-1", "head-2"], self.pw.connector_review_oids(timeline))

    def test_connector_review_status_distinguishes_body_from_empty_container(self):
        head = "a" * 40
        self.assertEqual("review_seen", self.pw.connector_review_status(
            [_review(head, "findings or boilerplate")], head
        ))
        self.assertEqual("review_pending", self.pw.connector_review_status(
            [_review(head, " \n")], head
        ))
        self.assertEqual("no_connector_review", self.pw.connector_review_status(
            [_review("b" * 40, "reviewed another head")], head
        ))

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

    def test_gh_call_clamps_timeout_to_shared_deadline(self):
        completed = subprocess.CompletedProcess([], 0, "[]", "")
        with patch.object(self.pw.subprocess, "run", return_value=completed) as run, \
                patch.object(self.pw.time, "monotonic", return_value=10.0):
            self.pw._gh_call("repos/x/y/issues/1/timeline", deadline=15.0)
        self.assertEqual(5.0, run.call_args.kwargs["timeout"])

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
        def fake_one(path, budget=None):
            if path.endswith("/status"):
                return {"state": "success"}
            return {"head": {"sha": "s"}, "state": "open", "merged": False}

        def fake_pages(path, budget=None):
            if "check-runs" in path:
                return [{"check_runs": [
                    {"name": "verify", "id": 1, "conclusion": "failure"},
                    {"name": "verify", "id": 2, "conclusion": "success"},
                ]}]
            return []

        with patch.object(self.pw, "gh_one", side_effect=fake_one), \
                patch.object(self.pw, "gh_array", side_effect=lambda p, budget=None: []), \
                patch.object(self.pw, "_gh_pages", side_effect=fake_pages):
            sig, _ = self.pw.snapshot("x/y", "1")
        self.assertIn("verify#1", sig["checks"])
        self.assertIn("verify#2", sig["checks"])
        self.assertEqual("failure", sig["checks"]["verify#1"])
        self.assertEqual("success", sig["checks"]["verify#2"])

    def _snapshot_with_comments(self, n_comments, fetched):
        comments = [{"id": i} for i in range(n_comments)]

        def fake_array(path, budget=None):
            if path.endswith("issues/1/comments"):
                return comments
            if "comments/" in path and path.endswith("/reactions"):
                fetched.append(path)
            return []

        def fake_one(path, budget=None):
            if path.endswith("/status"):
                return {"state": "success"}
            return {"head": {"sha": "s"}, "state": "open", "merged": False}

        with patch.object(self.pw, "gh_one", side_effect=fake_one), \
                patch.object(self.pw, "gh_array", side_effect=fake_array), \
                patch.object(self.pw, "_gh_pages", side_effect=lambda p, budget=None: []):
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

    def test_budget_exhausts_after_max_pages(self):
        b = self.pw._Budget(2)
        b.charge("p")
        b.charge("p")
        with self.assertRaises(RuntimeError):
            b.charge("p")

    def test_budget_observes_deadline(self):
        b = self.pw._Budget(100, deadline=self.pw.time.monotonic() - 1)
        with self.assertRaises(RuntimeError) as cm:
            b.charge("p")
        self.assertIn("deadline", str(cm.exception))

    def test_snapshot_fails_closed_on_cumulative_budget(self):
        # Per-call MAX_PAGES is not enough: many legal calls collectively overrun.
        # The shared snapshot budget must fail closed across calls.
        def fake_call(path):
            if "/pulls/" in path:
                return json.dumps({"head": {"sha": "s"}, "state": "open", "merged": False})
            if "/status" in path:
                return json.dumps({"state": "success"})
            if "check-runs" in path:
                return json.dumps({"check_runs": []})
            return json.dumps([])

        with patch.object(self.pw, "MAX_SNAPSHOT_PAGES", 3), \
                patch.object(self.pw, "_gh_call", side_effect=fake_call):
            with self.assertRaises(RuntimeError) as cm:
                self.pw.snapshot("x/y", "1")
        self.assertIn("MAX_SNAPSHOT_PAGES", str(cm.exception))

    def test_snapshot_fails_closed_on_deadline(self):
        def fake_call(path):
            return json.dumps({"head": {"sha": "s"}} if "/pulls/" in path else [])

        with patch.object(self.pw, "_gh_call", side_effect=fake_call):
            with self.assertRaises(RuntimeError) as cm:
                self.pw.snapshot("x/y", "1", deadline=self.pw.time.monotonic() - 1)
        self.assertIn("deadline", str(cm.exception))

    def test_repo_is_required(self):
        # A worker must never fall back to a default repo.
        with patch.object(sys, "argv", ["pr_watch.py", "--pr", "1"]):
            with self.assertRaises(SystemExit):
                self.pw.main()

    def test_settle_after_push_reports_no_review_after_bounded_window(self):
        base = _sig(connector_review_oids=[])
        snapshots = iter([(base, {}), (base, {})])
        clock = iter([0.0, 0.0, 1.0, 1.0])
        with patch.object(self.pw, "snapshot", side_effect=lambda *args: next(snapshots)), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep") as sleep, \
                patch.object(sys, "stdout") as stdout:
            self.assertEqual(0, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        sleep.assert_called_once()
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn('"settle": "no_connector_review"', output)
        self.assertIn('"head":', output)
        self.assertIn('"final_snapshot_seconds":', output)

    def test_settle_after_push_rejects_stale_snapshot_when_tail_poll_fails(self):
        base = _sig(connector_review_oids=[])
        snapshots = iter([(base, {}), RuntimeError("transport unavailable")])
        clock = iter([0.0, 0.0, 1.0])
        with patch.object(self.pw, "snapshot", side_effect=snapshots), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep"), \
                patch.object(self.pw.sys, "stderr") as stderr:
            self.assertEqual(2, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("tail snapshot", output)

    def test_settle_after_push_binds_baseline_and_tail_to_separate_deadlines(self):
        base = _sig(connector_review_oids=[])
        calls = []
        clock = iter([10.0, 10.0, 11.0, 11.0])

        def fake_snapshot(*args):
            calls.append(args)
            return base, {}

        with patch.object(self.pw, "snapshot", side_effect=fake_snapshot), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep"):
            self.assertEqual(0, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        self.assertEqual(11.0, calls[0][2])
        self.assertEqual(11.0 + self.pw.FINAL_SNAPSHOT_GRACE, calls[1][2])

    def test_settle_after_push_accepts_review_for_the_captured_head(self):
        head = "a" * 40
        base = _sig(head=head, connector_review_oids=[])
        current = _sig(
            head=head,
            connector_review_oids=[head],
            timeline=[["reviewed", 1]],
        )
        snapshots = iter([
            (base, _raw()),
            (current, _raw(timeline=[_review(head)])),
        ])
        clock = iter([0.0, 0.0, 0.0, 0.5])
        with patch.object(self.pw, "snapshot", side_effect=lambda *args: next(snapshots)), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep") as sleep, \
                patch.object(sys, "stdout") as stdout:
            self.assertEqual(0, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        sleep.assert_called_once()
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn('"settle": "review_seen"', output)
        self.assertIn(head, output)

    def test_empty_review_at_baseline_ends_review_pending(self):
        head = "a" * 40
        base = _sig(head=head, connector_review_oids=[head])
        snapshots = iter([
            (base, _raw(timeline=[_review(head, "\n")])),
            (base, _raw(timeline=[_review(head, "\n")])),
        ])
        clock = iter([0.0, 0.0, 1.0, 1.0])
        with patch.object(self.pw, "snapshot", side_effect=lambda *args: next(snapshots)), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep"), \
                patch.object(sys, "stdout") as stdout:
            self.assertEqual(0, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn('"settle": "review_pending"', output)
        self.assertNotIn('"settle": "review_seen"', output)

    def test_empty_review_during_window_remains_review_pending(self):
        head = "a" * 40
        base = _sig(head=head, connector_review_oids=[])
        pending = _sig(head=head, connector_review_oids=[head])
        snapshots = iter([
            (base, _raw()),
            (pending, _raw(timeline=[_review(head, "")])),
            (pending, _raw(timeline=[_review(head, "")])),
        ])
        clock = iter([0.0, 0.0, 0.0, 0.5, 1.0, 1.0])
        with patch.object(self.pw, "snapshot", side_effect=lambda *args: next(snapshots)), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep"), \
                patch.object(sys, "stdout") as stdout:
            self.assertEqual(0, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn('"settle": "review_pending"', output)

    def test_empty_review_only_in_tail_ends_review_pending(self):
        head = "a" * 40
        base = _sig(head=head, connector_review_oids=[])
        tail = _sig(head=head, connector_review_oids=[head])
        snapshots = iter([
            (base, _raw()),
            (tail, _raw(timeline=[_review(head, "\t")])),
        ])
        clock = iter([0.0, 0.0, 1.0, 1.0])
        with patch.object(self.pw, "snapshot", side_effect=lambda *args: next(snapshots)), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep"), \
                patch.object(sys, "stdout") as stdout:
            self.assertEqual(0, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn('"settle": "review_pending"', output)

    def test_new_bound_manual_request_reaction_is_review_seen(self):
        head = "a" * 40
        request = {
            "id": 7,
            "body": f"@codex review for the exact head `{head}`.",
            "user": {"login": "pixexid"},
            "created_at": "2026-08-12T00:00:00Z",
            "updated_at": "2026-08-12T00:00:00Z",
        }
        reaction = {
            "id": 8,
            "content": "+1",
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "created_at": "2026-08-12T00:00:02Z",
        }
        base = _sig(head=head, connector_review_oids=[])
        current = _sig(head=head, connector_review_oids=[], reactions=["c7:chatgpt-codex-connector[bot]:+1"])
        snapshots = iter([
            (base, _raw(comments=[request], captured_at="2026-08-12T00:00:01Z")),
            (current, _raw(
                comments=[request],
                comment_reactions=[{"comment": request, "reaction": reaction}],
                captured_at="2026-08-12T00:00:03Z",
            )),
        ])
        clock = iter([0.0, 0.0, 0.0, 0.5])
        with patch.object(self.pw, "snapshot", side_effect=lambda *args: next(snapshots)), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep"), \
                patch.object(sys, "stdout") as stdout:
            self.assertEqual(0, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn('"settle": "review_seen"', output)

    def test_request_comment_accepts_exact_single_head_sha(self):
        head = "a" * 40
        request = {
            "id": 7,
            "body": f"@codex review for the exact head `{head}`.",
            "created_at": "2026-08-12T00:00:00Z",
        }
        self.assertEqual(
            [request], self.pw._request_comments(_raw(comments=[request]), head)
        )

    def test_request_comment_rejects_target_and_different_full_sha(self):
        head = "a" * 40
        other = "b" * 40
        request = {
            "id": 7,
            "body": f"@codex review for `{head}` and `{other}`.",
            "created_at": "2026-08-12T00:00:00Z",
        }
        self.assertEqual([], self.pw._request_comments(_raw(comments=[request]), head))

    def test_fresh_pr_level_reaction_without_pickup_binding_is_nonterminal(self):
        head = "a" * 40
        reaction = {
            "id": 8,
            "content": "+1",
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "created_at": "2026-08-12T00:00:02Z",
        }
        self.assertEqual(
            "no_connector_review",
            self.pw.connector_artifact_status(
                _raw(
                    reactions=[reaction],
                    captured_at="2026-08-12T00:00:03Z",
                ),
                head,
                _raw(captured_at="2026-08-12T00:00:01Z"),
            ),
        )

    def test_stale_reaction_is_not_exact_head_evidence(self):
        head = "a" * 40
        reaction = {
            "id": 8,
            "content": "+1",
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "created_at": "2026-08-12T00:00:00Z",
        }
        self.assertEqual(
            "no_connector_review",
            self.pw.connector_artifact_status(
                _raw(
                    reactions=[reaction],
                    captured_at="2026-08-12T00:00:03Z",
                ),
                head,
                _raw(captured_at="2026-08-12T00:00:01Z"),
            ),
        )

    def test_unbound_reaction_with_prior_connector_artifact_is_not_terminal(self):
        head = "a" * 40
        prior = _review("b" * 40, "reviewed another head")
        reaction = {
            "id": 8,
            "content": "+1",
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "created_at": "2026-08-12T00:00:02Z",
        }
        self.assertEqual(
            "no_connector_review",
            self.pw.connector_artifact_status(
                _raw(
                    timeline=[prior],
                    reactions=[reaction],
                    captured_at="2026-08-12T00:00:03Z",
                ),
                head,
                _raw(
                    timeline=[prior],
                    captured_at="2026-08-12T00:00:01Z",
                ),
            ),
        )

    def test_settle_after_push_refuses_a_changed_head(self):
        base = _sig(head="a" * 40, connector_review_oids=[])
        current = _sig(head="b" * 40, connector_review_oids=[])
        snapshots = iter([(base, {}), (current, {})])
        clock = iter([0.0, 0.0, 0.0])
        with patch.object(self.pw, "snapshot", side_effect=lambda *args: next(snapshots)), \
                patch.object(self.pw.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.pw.time, "sleep"), \
                patch.object(self.pw.sys, "stderr") as stderr:
            self.assertEqual(2, self.pw.settle_after_push("x/y", "1", 1.0, 1.0))
        self.assertTrue(stderr.write.called)


if __name__ == "__main__":
    unittest.main()
