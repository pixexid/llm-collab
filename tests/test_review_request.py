"""Focused contract tests for bin/review_request.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import review_request  # noqa: E402

SHA = "a" * 40
OTHER_SHA = "b" * 40
PROJECTS = [
    {
        "id": "llm-collab",
        "github": {"enabled": True, "repo": "pixexid/llm-collab"},
    }
]
AMIGA_PROJECT = {
    "id": "amiga",
    "github": {"enabled": True, "repo": "pixexid/amiga"},
}
INITIAL_ARGS = [
    "--pr",
    "1",
    "--project",
    "llm-collab",
    "--tier",
    "A",
    "--focus",
    "authority selection, bounded reads",
    "--contract",
    "352",
]


def page(
    bodies: list[str],
    *,
    next_page: bool = False,
    cursor: str | None = None,
    author: str = "pixexid",
    review_authors: list[str] | None = None,
    comment_reactors: list[str] | None = None,
    pr_reactors: list[str] | None = None,
    thread_authors: list[str] | None = None,
):
    def reactions(logins):
        return [{"users": {"totalCount": len(logins or []), "nodes": [
            {"login": login} for login in (logins or [])
        ]}}]

    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reactionGroups": reactions(pr_reactors),
                    "comments": {
                        "nodes": [
                            {"body": body, "author": {"login": author},
                             "reactionGroups": reactions(comment_reactors)}
                            for body in bodies
                        ],
                        "pageInfo": {
                            "hasNextPage": next_page,
                            "endCursor": cursor,
                        },
                    },
                    "reviews": {
                        "nodes": [
                            {"author": {"login": login}}
                            for login in (review_authors or [])
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "reviewThreads": {
                        "nodes": [{"comments": {"nodes": [
                            {"author": {"login": login}}
                        ]}} for login in (thread_authors or [])],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    }


class RequestBodyTest(unittest.TestCase):
    def test_body_names_focus_head_and_contract(self):
        body = review_request.build_request_body(
            "authority selection, bounded reads", SHA, contract=352
        )
        self.assertTrue(
            body.startswith(
                "@codex review for authority selection, bounded reads"
            )
        )
        self.assertIn(f"at exact head `{SHA}`.", body)
        self.assertIn("lane contract in #352", body)

    def test_body_accepts_task_hosted_contract(self):
        body = review_request.build_request_body(
            "authority selection", SHA, contract="TASK-ABC123"
        )
        self.assertIn("lane contract in TASK-ABC123", body)

    def test_empty_focus_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "at least one review lens"):
            review_request.build_request_body("   ", SHA)

    def test_caller_text_cannot_name_a_head(self):
        for value in (f"auth at {SHA}", "auth at exact head"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SystemExit, "caller text"):
                    review_request.reject_caller_supplied_shas({"focus": value})

    def test_caller_text_cannot_close_an_autolinked_issue(self):
        for label, value in (
            ("focus", "fix GH-123"),
            ("note", "resolved GH-456"),
            ("settled", "closes GH-789"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(SystemExit, "closing keyword"):
                    review_request.reject_caller_supplied_shas({label: value})


class ProjectResolutionTest(unittest.TestCase):
    def test_registered_project_supplies_exact_repo(self):
        self.assertEqual(
            review_request.repo_coordinates("llm-collab", PROJECTS),
            ("pixexid", "llm-collab"),
        )

    def test_amiga_registration_supplies_amiga_repo(self):
        self.assertEqual(
            review_request.repo_coordinates("amiga", [*PROJECTS, AMIGA_PROJECT]),
            ("pixexid", "amiga"),
        )

    def test_disabled_project_is_refused(self):
        projects = [
            {
                "id": "llm-collab",
                "github": {"enabled": False, "repo": "pixexid/llm-collab"},
            }
        ]
        with self.assertRaisesRegex(SystemExit, "no enabled GitHub"):
            review_request.repo_coordinates("llm-collab", projects)

    def test_product_worktree_reads_the_coordination_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "projects.json").write_text(json.dumps({"projects": PROJECTS}))
            with mock.patch.object(
                review_request, "coordination_root", return_value=root
            ):
                self.assertEqual(
                    review_request.repo_coordinates("llm-collab"),
                    ("pixexid", "llm-collab"),
                )

    def test_malformed_project_registry_uses_the_documented_refusal(self):
        for payload in (None, [], {"projects": [None]}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "projects.json").write_text(json.dumps(payload))
                with mock.patch.object(
                    review_request, "coordination_root", return_value=root
                ):
                    with self.assertRaisesRegex(SystemExit, "valid projects list"):
                        review_request.common_checkout_projects()


class RequestHistoryTest(unittest.TestCase):
    def test_counts_every_request_on_the_pr(self):
        bodies = [
            f"@codex review for lenses at exact head `{SHA}`.",
            f"@codex review for lenses at exact head `{OTHER_SHA}`.",
            (
                f"@codex review for lenses at exact head `{OTHER_SHA}`. "
                f"Settled on `{SHA}`."
            ),
            "unrelated comment",
            f"quoted text\n@codex review not at line start {SHA}",
        ]
        self.assertEqual(review_request.prior_requests(bodies), [*bodies[:3], bodies[4]])

    def test_request_with_a_sender_preamble_spends_the_budget(self):
        body = f"Sender: Claude.\n\n@codex review for lenses at exact head `{SHA}`."
        self.assertEqual(review_request.prior_requests([body]), [body])

    def test_paginates_until_the_initial_request_is_found(self):
        first = ["later"] * review_request.COMMENT_PAGE_SIZE
        initial = f"@codex review for lenses at exact head `{SHA}`."
        with mock.patch.object(
            review_request,
            "run_json",
            side_effect=[
                page(first, next_page=True, cursor="cursor-1"),
                page([initial]),
            ],
        ) as run_json:
            bodies, connector_seen = review_request.pr_review_history(
                1, "pixexid", "llm-collab"
            )
        self.assertEqual(bodies[-1], initial)
        self.assertFalse(connector_seen)
        self.assertIn("commentsAfter=cursor-1", run_json.call_args_list[1].args[0])

    def test_fails_closed_when_history_bound_is_exhausted(self):
        with (
            mock.patch.object(review_request, "COMMENT_HARD_CAP", 2),
            mock.patch.object(
                review_request,
                "run_json",
                return_value=page(["one", "two"], next_page=True, cursor="more"),
            ),
        ):
            with self.assertRaisesRegex(SystemExit, "declared bound"):
                review_request.pr_review_history(1, "pixexid", "llm-collab")

    def test_fails_closed_when_pagination_cursor_does_not_advance(self):
        with mock.patch.object(
            review_request,
            "run_json",
            side_effect=[
                page(["one"], next_page=True, cursor="same"),
                page([], next_page=True, cursor="same"),
            ],
        ):
            with self.assertRaisesRegex(SystemExit, "did not advance"):
                review_request.pr_review_history(1, "pixexid", "llm-collab")

    def test_fails_closed_when_page_bound_is_exhausted(self):
        with (
            mock.patch.object(review_request, "COMMENT_PAGE_HARD_CAP", 2),
            mock.patch.object(
                review_request,
                "run_json",
                side_effect=[
                    page([], next_page=True, cursor="one"),
                    page([], next_page=True, cursor="two"),
                ],
            ),
        ):
            with self.assertRaisesRegex(SystemExit, "page bound"):
                review_request.pr_review_history(1, "pixexid", "llm-collab")

    def test_counts_requests_from_other_workers(self):
        request = f"@codex review for lenses at exact head `{SHA}`."
        with mock.patch.object(
            review_request,
            "run_json",
            return_value=page([request], author="untrusted-commenter"),
        ):
            bodies, connector_seen = review_request.pr_review_history(
                1, "pixexid", "llm-collab"
            )
        self.assertEqual(review_request.prior_requests(bodies), [request])
        self.assertFalse(connector_seen)

    def test_connector_review_spends_the_fallback_budget(self):
        with mock.patch.object(
            review_request,
            "run_json",
            return_value=page([], review_authors=[review_request.CONNECTOR_LOGIN]),
        ):
            bodies, connector_seen = review_request.pr_review_history(
                1, "pixexid", "llm-collab"
            )
        self.assertEqual(bodies, [])
        self.assertTrue(connector_seen)

    def test_connector_reaction_or_thread_spends_the_fallback_budget(self):
        for kwargs in (
            {"comment_reactors": [review_request.CONNECTOR_LOGIN]},
            {"pr_reactors": [review_request.CONNECTOR_LOGIN]},
            {"thread_authors": [review_request.CONNECTOR_LOGIN]},
        ):
            with self.subTest(kwargs=kwargs), mock.patch.object(
                review_request, "run_json", return_value=page(["note"], **kwargs),
            ):
                _bodies, connector_seen = review_request.pr_review_history(
                    1, "pixexid", "llm-collab"
                )
            self.assertTrue(connector_seen)


class MainFlowTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(review_request, "require_contract")
        patcher.start()
        self.addCleanup(patcher.stop)
        visibility = mock.patch.object(
            review_request, "repo_is_private", return_value=True
        )
        visibility.start()
        self.addCleanup(visibility.stop)

    def patches(
        self,
        *,
        comments: list[str] | None = None,
        heads: list[str] | None = None,
        local: str = SHA,
    ):
        return (
            mock.patch.object(
                review_request,
                "repo_coordinates",
                return_value=("pixexid", "llm-collab"),
            ),
            mock.patch.object(
                review_request, "pr_head", side_effect=heads or [SHA, SHA, SHA]
            ),
            mock.patch.object(review_request, "local_head", return_value=local),
            mock.patch.object(
                review_request,
                "pr_review_history",
                return_value=(comments or [], False),
            ),
            mock.patch.object(review_request, "post_comment"),
        )

    def test_posts_only_after_both_head_checks_and_rechecks_after(self):
        patches = self.patches()
        with patches[0], patches[1] as pr_head, patches[2], patches[3], patches[4] as post:
            self.assertEqual(review_request.main(INITIAL_ARGS), 0)
        self.assertEqual(pr_head.call_count, 3)
        body = post.call_args.args[-1]
        self.assertIn(SHA, body)
        self.assertIn("lane contract in #352", body)

    def test_refuses_when_local_head_differs(self):
        patches = self.patches(local=OTHER_SHA)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as post:
            with self.assertRaisesRegex(SystemExit, "push the verified head"):
                review_request.main(INITIAL_ARGS)
        post.assert_not_called()

    def test_refuses_second_request_even_after_the_head_changes(self):
        initial = f"@codex review for lenses at exact head `{OTHER_SHA}`."
        patches = self.patches(comments=[initial])
        with patches[0], patches[1], patches[2], patches[3], patches[4] as post:
            with self.assertRaisesRegex(SystemExit, "one-pass budget is spent"):
                review_request.main(INITIAL_ARGS)
        post.assert_not_called()

    def test_refuses_when_the_automatic_connector_already_reviewed(self):
        patches = self.patches()
        patches = (*patches[:3], mock.patch.object(
            review_request, "pr_review_history", return_value=([], True)
        ), patches[4])
        with patches[0], patches[1], patches[2], patches[3], patches[4] as post:
            with self.assertRaisesRegex(SystemExit, "automatic connector artifact"):
                review_request.main(INITIAL_ARGS)
        post.assert_not_called()

    def test_tier_a_requires_lane_contract(self):
        args = INITIAL_ARGS[:-2]
        with self.assertRaisesRegex(SystemExit, "Tier A requires --contract"):
            review_request.main(args)

    def test_manual_fallback_is_tier_a_only(self):
        for tier in ("B", "C"):
            with self.subTest(tier=tier), self.assertRaisesRegex(
                SystemExit, "manual fallback is Tier A only"
            ):
                review_request.main(
                    [
                        "--pr", "1", "--project", "llm-collab", "--tier", tier,
                        "--focus", "mechanical prose",
                    ]
                )

    def test_head_move_before_post_has_no_side_effect(self):
        patches = self.patches(heads=[SHA, OTHER_SHA])
        with patches[0], patches[1], patches[2], patches[3], patches[4] as post:
            with self.assertRaisesRegex(SystemExit, "nothing was posted"):
                review_request.main(INITIAL_ARGS)
        post.assert_not_called()

    def test_head_move_during_post_reports_stale_request(self):
        patches = self.patches(heads=[SHA, SHA, OTHER_SHA])
        with patches[0], patches[1], patches[2], patches[3], patches[4] as post:
            with self.assertRaisesRegex(SystemExit, "posted request is stale"):
                review_request.main(INITIAL_ARGS)
        post.assert_called_once()

    def test_dry_run_posts_nothing(self):
        args = [*INITIAL_ARGS, "--dry-run"]
        patches = self.patches(heads=[SHA])
        with patches[0], patches[1], patches[2], patches[3], patches[4] as post:
            self.assertEqual(review_request.main(args), 0)
        post.assert_not_called()

    def test_public_repo_request_claims_untrusted_comment_content(self):
        patches = self.patches()
        with (
            mock.patch.object(
                review_request, "repo_is_private", return_value=False
            ),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4] as post,
        ):
            self.assertEqual(review_request.main(INITIAL_ARGS), 0)
        body = post.call_args.args[-1]
        self.assertIn("public repository", body)
        self.assertIn("untrusted input", body)
        self.assertNotIn("private repository", body)


class RequestShapingTest(unittest.TestCase):
    """GH-357: requests are shaped to cost one audit, not five."""

    def test_every_initial_request_carries_the_threat_model(self):
        body = review_request.build_request_body("lenses", SHA)
        self.assertIn("not an adversary", body)
        self.assertIn("non-goals", body)

    def test_visibility_is_never_hardcoded(self):
        private = review_request.build_request_body("lenses", SHA, is_private=True)
        self.assertIn("private repository", private)
        public = review_request.build_request_body("lenses", SHA, is_private=False)
        self.assertIn("public repository", public)
        self.assertIn("untrusted input", public)
        self.assertNotIn("private repository", public)
        unknown = review_request.build_request_body("lenses", SHA)
        self.assertNotIn("private repository", unknown)
        self.assertNotIn("public repository", unknown)

    def test_manual_fallback_is_always_a_full_audit(self):
        body = review_request.build_request_body("lenses", SHA, contract=352)
        self.assertIn("full diff", body)


class CommandContractTest(unittest.TestCase):
    def test_string_refusal_is_exit_two(self):
        with (
            mock.patch.object(
                review_request, "main", side_effect=SystemExit("error: refused")
            ),
            mock.patch("sys.stderr"),
        ):
            self.assertEqual(review_request.cli(), 2)

    def test_script_is_executable(self):
        self.assertTrue(os.access(ROOT / "bin" / "review_request.py", os.X_OK))

    def test_missing_command_is_a_refusal(self):
        with mock.patch.object(
            subprocess, "run", side_effect=FileNotFoundError("missing")
        ):
            with self.assertRaisesRegex(SystemExit, "cannot run gh"):
                review_request.run(["gh", "pr", "view"], 1)

    def test_malformed_json_is_a_refusal(self):
        with mock.patch.object(
            review_request,
            "run",
            return_value=subprocess.CompletedProcess(
                ["gh"], 0, stdout="{", stderr=""
            ),
        ):
            with self.assertRaisesRegex(SystemExit, "malformed JSON"):
                review_request.run_json(["gh", "pr", "view"])

    def test_closed_pr_is_refused(self):
        with mock.patch.object(
            review_request,
            "run_json",
            return_value={"headRefOid": SHA, "state": "CLOSED"},
        ):
            with self.assertRaisesRegex(SystemExit, "is not open"):
                review_request.pr_head(1, "pixexid", "llm-collab")

    def test_lane_contract_must_be_an_existing_issue_or_task(self):
        with self.assertRaisesRegex(SystemExit, "positive issue number or TASK-id"):
            review_request.require_contract(
                "0", "llm-collab", "pixexid", "llm-collab"
            )
        with mock.patch.object(
            review_request, "run_json", return_value={"number": 352}
        ) as run_json:
            review_request.require_contract(
                "352", "llm-collab", "pixexid", "llm-collab"
            )
        self.assertIn("issue", run_json.call_args.args[0])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "Tasks" / "active"
            task_dir.mkdir(parents=True)
            (task_dir / "renamed-lane.md").write_text(
                "---\ntask_id: TASK-ABC123\nproject_id: llm-collab\n---\ncontract"
            )
            with mock.patch.object(
                review_request, "coordination_root", return_value=root
            ):
                review_request.require_contract(
                    "TASK-ABC123", "llm-collab", "pixexid", "llm-collab"
                )
                with self.assertRaisesRegex(SystemExit, "not bound to project"):
                    review_request.require_contract(
                        "TASK-ABC123", "amiga", "pixexid", "llm-collab"
                    )
                with self.assertRaisesRegex(SystemExit, "does not exist"):
                    review_request.require_contract(
                        "TASK-MISSING", "llm-collab", "pixexid", "llm-collab"
                    )

    def test_task_contract_scan_is_cumulative_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "Tasks" / "active"
            task_dir.mkdir(parents=True)
            (task_dir / "foreign-one.md").write_text("one")
            (task_dir / "foreign-two.md").write_text("two")
            with (
                mock.patch.object(
                    review_request, "coordination_root", return_value=root
                ),
                mock.patch.object(
                    review_request, "TASK_CONTRACT_ENTRY_HARD_CAP", 1
                ),
            ):
                with self.assertRaisesRegex(SystemExit, "entry bound"):
                    review_request.require_contract(
                        "TASK-MISSING",
                        "llm-collab",
                        "pixexid",
                        "llm-collab",
                    )

    def test_task_contract_frontmatter_reads_are_cumulatively_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "Tasks" / "active"
            task_dir.mkdir(parents=True)
            (task_dir / "one.md").write_text("one")
            (task_dir / "two.md").write_text("two")
            with (
                mock.patch.object(
                    review_request, "coordination_root", return_value=root
                ),
                mock.patch.object(review_request, "TASK_CONTRACT_MAX_BYTES", 5),
            ):
                with self.assertRaisesRegex(SystemExit, "cumulative"):
                    review_request.require_contract(
                        "TASK-MISSING",
                        "llm-collab",
                        "pixexid",
                        "llm-collab",
                    )


if __name__ == "__main__":
    unittest.main()
