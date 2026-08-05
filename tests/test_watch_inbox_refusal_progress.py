"""GH-539: the watcher must make progress on repo-scope refusals.

Before this lane a refusal was emitted but never recorded, so every poll
re-decided and re-logged the same stale message — refusal work was O(unread) per
poll forever. These tests pin the four properties that fix it without turning the
change into backlog cleanup.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import watch_inbox  # noqa: E402


class RefusalFingerprintTest(unittest.TestCase):
    def test_same_decision_is_stable(self) -> None:
        a = watch_inbox.refusal_fingerprint("project_mismatch", ["app"], ["other"], "llm-collab", "amiga")
        b = watch_inbox.refusal_fingerprint("project_mismatch", ["app"], ["other"], "llm-collab", "amiga")
        self.assertEqual(a, b)

    def test_repo_target_order_does_not_change_the_decision(self) -> None:
        a = watch_inbox.refusal_fingerprint("r", ["app", "docs"], None, "p", None)
        b = watch_inbox.refusal_fingerprint("r", ["docs", "app"], None, "p", None)
        self.assertEqual(a, b)

    def test_corrected_routing_produces_a_new_fingerprint(self) -> None:
        """AC4: the whole point of keying on the decision rather than the path —
        a config fix must re-open eligibility instead of being suppressed."""
        stale = watch_inbox.refusal_fingerprint("project_mismatch", ["app"], ["other"], "llm-collab", "amiga")
        fixed = watch_inbox.refusal_fingerprint("project_mismatch", ["app"], ["app"], "llm-collab", "llm-collab")
        self.assertNotEqual(stale, fixed)

    def test_reason_change_produces_a_new_fingerprint(self) -> None:
        a = watch_inbox.refusal_fingerprint("route_ambiguous", ["app"], None, "p", None)
        b = watch_inbox.refusal_fingerprint("project_mismatch", ["app"], None, "p", None)
        self.assertNotEqual(a, b)


class RefusalProgressStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _patch_dir(self):
        from unittest.mock import patch

        return patch.object(watch_inbox, "agent_dir", return_value=self.root)

    def test_round_trip(self) -> None:
        with self._patch_dir():
            entry = {
                "fp": "fp1",
                "mtime": None,
                "session_id": None,
                "reason": "repo_mismatch",
                "packet_repo_targets": ["other"],
                "packet_project": "amiga",
                "session_id": None,
            }
            watch_inbox.save_refusal_progress("claude", {"a.md": entry})
            self.assertEqual({"a.md": entry}, watch_inbox.load_refusal_progress("claude"))

    def test_missing_store_is_empty_not_an_error(self) -> None:
        with self._patch_dir():
            self.assertEqual({}, watch_inbox.load_refusal_progress("claude"))

    def test_corrupt_store_degrades_to_empty(self) -> None:
        """Progress is an optimisation, never a gate: a damaged store must make the
        watcher re-log, not crash the durable wake path."""
        with self._patch_dir():
            (self.root / "watcher-refusal-progress.json").write_text("{not json")
            self.assertEqual({}, watch_inbox.load_refusal_progress("claude"))

    def test_write_is_atomic_and_leaves_no_temp_file(self) -> None:
        with self._patch_dir():
            watch_inbox.save_refusal_progress("claude", {"a.md": "fp1"})
            self.assertEqual(
                [], sorted(p.name for p in self.root.glob("*.tmp"))
            )
            payload = json.loads((self.root / "watcher-refusal-progress.json").read_text())
            self.assertEqual(1, payload["version"])

    def test_store_is_separate_from_the_durable_inbox(self) -> None:
        """AC2/AC7: refusal progress is watcher-owned state. It must not be the
        inbox index, and recording a refusal must never mark anything read."""
        with self._patch_dir():
            watch_inbox.save_refusal_progress("claude", {"a.md": "fp1"})
            self.assertNotEqual(
                watch_inbox.refusal_progress_path("claude").name, "inbox.json"
            )



class RefusalProgressShapeTest(unittest.TestCase):
    """GH-539 review finding 3: valid JSON of the WRONG SHAPE must degrade like
    corrupt JSON. `{"refused": []}` previously returned a list, and the watcher
    loop then called .get() on it."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _patch_dir(self):
        from unittest.mock import patch

        return patch.object(watch_inbox, "agent_dir", return_value=self.root)

    def _write(self, payload: str) -> None:
        (self.root / "watcher-refusal-progress.json").write_text(payload)

    def test_refused_as_list_degrades_to_empty_mapping(self) -> None:
        with self._patch_dir():
            self._write('{"refused": []}')
            result = watch_inbox.load_refusal_progress("claude")
            self.assertEqual({}, result)
            self.assertIsInstance(result, dict)
            self.assertIsNone(result.get("anything"))

    def test_top_level_list_degrades_to_empty_mapping(self) -> None:
        with self._patch_dir():
            self._write('["a.md"]')
            self.assertEqual({}, watch_inbox.load_refusal_progress("claude"))

    def test_non_string_entries_are_dropped(self) -> None:
        with self._patch_dir():
            self._write('{"refused": {"a.md": "fp1", "b.md": 7, "9": null}}')
            # A pre-GH-539 bare-string entry stays usable, normalised to the
            # richer shape; malformed values are dropped.
            self.assertEqual(
                {
                    "a.md": {
                        "fp": "fp1",
                        "mtime": None,
                        "reason": "",
                        "packet_repo_targets": None,
                        "packet_project": None,
                        "session_id": None,
                    }
                },
                watch_inbox.load_refusal_progress("claude"),
            )


class BatchRefusalRoutingInputsTest(unittest.TestCase):
    """GH-539 review finding 2: batch refusals must carry the packet's routing
    inputs, or AC4 silently fails for that path — a rerouted packet keeps the same
    fingerprint and stays suppressed."""

    def test_packet_reroute_changes_the_fingerprint(self) -> None:
        stale = watch_inbox.refusal_fingerprint(
            "project_mismatch", ["app"], ["other"], "llm-collab", "amiga"
        )
        rerouted = watch_inbox.refusal_fingerprint(
            "project_mismatch", ["app"], ["app"], "llm-collab", "llm-collab"
        )
        self.assertNotEqual(stale, rerouted)

    def test_missing_packet_inputs_collapse_to_one_fingerprint(self) -> None:
        """Documents WHY finding 2 mattered: without the carried fields every
        packet fingerprints identically, so reroutes cannot re-open."""
        a = watch_inbox.refusal_fingerprint("project_mismatch", ["app"], None, "llm-collab", None)
        b = watch_inbox.refusal_fingerprint("project_mismatch", ["app"], None, "llm-collab", None)
        self.assertEqual(a, b)

    def test_matching_unread_messages_records_packet_routing_inputs(self) -> None:
        import _session_autobridge as sab

        session = {"agent_id": "claude", "project_id": "llm-collab", "chat_id": None}
        message = {
            "path": "Chats/x/2026-08-05T00-00-00_to-claude_x.md",
            "frontmatter": {
                "project_id": "llm-collab",
                "repo_targets": ["other"],
            },
        }
        refusals: list[dict] = []
        from unittest.mock import patch

        with patch.object(sab, "bounded_unread_messages", return_value=[message]), patch.object(
            sab, "_session_repo_scope_matches", return_value=(False, "repo_mismatch")
        ):
            sab.matching_unread_messages(session, repo_scope_refusals=refusals)

        self.assertEqual(1, len(refusals))
        self.assertEqual(["other"], refusals[0]["packet_repo_targets"])
        self.assertEqual("llm-collab", refusals[0]["packet_project"])

class TerminalRefusalSkipsWorkTest(unittest.TestCase):
    """GH-539 review finding 1 — the one that matters. Suppressing the refusal
    EVENT while still running matching_unread_messages left the cost O(backlog)
    per poll. These assert the WORK is skipped, and that AC4 still re-opens."""

    def _entry(self, path, reason="repo_mismatch", packet_repo=None, packet_project=None,
               repo_targets=None, project_id=None, mtime=None):
        fp = watch_inbox.refusal_fingerprint(
            reason, repo_targets, packet_repo, project_id, packet_project
        )
        return {
            path: {
                "fp": fp,
                "mtime": mtime,
                "reason": reason,
                "packet_repo_targets": packet_repo,
                "packet_project": packet_project,
                "session_id": None,
            }
        }

    def test_terminal_path_is_skipped_under_the_same_decision(self) -> None:
        path = "Chats/x/nonexistent-so-mtime-is-None.md"
        progress = self._entry(path, repo_targets=["app"], project_id="llm-collab")
        skip = watch_inbox.terminal_refusal_paths(progress, ["app"], "llm-collab")
        self.assertEqual({path}, skip)

    def test_changed_subscriber_decision_reopens(self) -> None:
        """AC4, subscriber side: correcting --repo-target must re-evaluate."""
        path = "Chats/x/nonexistent-so-mtime-is-None.md"
        progress = self._entry(path, repo_targets=["app"], project_id="llm-collab")
        skip = watch_inbox.terminal_refusal_paths(progress, ["docs"], "llm-collab")
        self.assertEqual(set(), skip)

    def test_rerouted_packet_reopens_via_mtime(self) -> None:
        """AC4, packet side: a rewritten packet changes mtime, so a stored
        terminal decision no longer suppresses it."""
        path = "Chats/x/nonexistent-so-mtime-is-None.md"
        progress = self._entry(path, repo_targets=["app"], project_id="llm-collab", mtime=123.0)
        skip = watch_inbox.terminal_refusal_paths(progress, ["app"], "llm-collab")
        self.assertEqual(set(), skip)

    def test_matching_unread_messages_skips_before_the_routing_check(self) -> None:
        """The integrated assertion: with the path in skip_paths the repo-scope
        check is never called, so no work and no refusal is produced."""
        import _session_autobridge as sab
        from unittest.mock import patch

        session = {"agent_id": "claude", "project_id": None, "chat_id": None}
        message = {"path": "Chats/x/a.md", "frontmatter": {}}
        refusals: list = []
        with patch.object(sab, "bounded_unread_messages", return_value=[message]), patch.object(
            sab, "_session_repo_scope_matches"
        ) as scope:
            result = sab.matching_unread_messages(
                session, repo_scope_refusals=refusals, skip_paths={"Chats/x/a.md"}
            )
        scope.assert_not_called()
        self.assertEqual([], result)
        self.assertEqual([], refusals)


class RestartRoundTripTest(unittest.TestCase):
    """Codex finding: load_refusal_progress previously reduced entries to
    {fp, mtime}, so after a watcher RESTART terminal_refusal_paths recomputed a
    different fingerprint and re-evaluated the stale refusal — persistence was
    effectively dead across restarts."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _patch_dir(self):
        from unittest.mock import patch

        return patch.object(watch_inbox, "agent_dir", return_value=self.root)

    def test_persisted_entry_still_skips_after_restart(self) -> None:
        path = "Chats/x/nonexistent-so-mtime-is-None.md"
        repo_targets, project_id = ["app"], "llm-collab"
        reason, packet_repo, packet_project = "repo_mismatch", ["other"], "amiga"
        fp = watch_inbox.refusal_fingerprint(
            reason, repo_targets, packet_repo, project_id, packet_project
        )
        live = {
            path: {
                "fp": fp,
                "mtime": None,
                "reason": reason,
                "packet_repo_targets": packet_repo,
                "packet_project": packet_project,
                "session_id": None,
            }
        }
        # skips before any restart
        self.assertEqual(
            {path}, watch_inbox.terminal_refusal_paths(live, repo_targets, project_id)
        )
        with self._patch_dir():
            watch_inbox.save_refusal_progress("claude", live)
            reloaded = watch_inbox.load_refusal_progress("claude")
        # ...and still skips after a save/load cycle
        self.assertEqual(
            {path},
            watch_inbox.terminal_refusal_paths(reloaded, repo_targets, project_id),
        )

    def test_reloaded_entry_still_reopens_on_changed_routing(self) -> None:
        """The round trip must not become a blanket skip: AC4 still applies."""
        path = "Chats/x/nonexistent-so-mtime-is-None.md"
        fp = watch_inbox.refusal_fingerprint(
            "repo_mismatch", ["app"], ["other"], "llm-collab", "amiga"
        )
        live = {
            path: {
                "fp": fp,
                "mtime": None,
                "reason": "repo_mismatch",
                "packet_repo_targets": ["other"],
                "packet_project": "amiga",
                "session_id": None,
            }
        }
        with self._patch_dir():
            watch_inbox.save_refusal_progress("claude", live)
            reloaded = watch_inbox.load_refusal_progress("claude")
        self.assertEqual(
            set(), watch_inbox.terminal_refusal_paths(reloaded, ["docs"], "llm-collab")
        )


class BotReviewRegressionsTest(unittest.TestCase):
    """PR #542 bot findings, all three real and all three mine."""

    def test_refusal_from_one_session_does_not_skip_another(self) -> None:
        """P1: one agent, two sessions, different repo scopes. A packet refused by
        the `app` session must NOT be skipped before the `docs` session evaluates
        it, or the message is stranded unread until mtime changes."""
        path = "Chats/x/nonexistent.md"
        fp_app = watch_inbox.refusal_fingerprint(
            "repo_mismatch", ["app"], ["docs"], "llm-collab", "llm-collab", "SESSION-APP"
        )
        progress = {
            path: {
                "fp": fp_app,
                "mtime": None,
                "reason": "repo_mismatch",
                "packet_repo_targets": ["docs"],
                "packet_project": "llm-collab",
                "session_id": "SESSION-APP",
            }
        }
        self.assertEqual(
            {path},
            watch_inbox.terminal_refusal_paths(progress, ["app"], "llm-collab", "SESSION-APP"),
        )
        self.assertEqual(
            set(),
            watch_inbox.terminal_refusal_paths(progress, ["app"], "llm-collab", "SESSION-DOCS"),
        )

    def test_malformed_packet_repo_targets_do_not_raise(self) -> None:
        """P2: a packet carrying [1, \"app\"] made sorted() raise TypeError, which
        escaped the per-session handler and stalled the whole poll."""
        fp = watch_inbox.refusal_fingerprint(
            "repo_mismatch", ["app"], [1, "app"], "llm-collab", "amiga", "S"
        )
        self.assertIsInstance(fp, str)
        same = watch_inbox.refusal_fingerprint(
            "repo_mismatch", ["app"], ["app", 1], "llm-collab", "amiga", "S"
        )
        self.assertEqual(fp, same)  # still order-insensitive

    def test_oversized_progress_store_degrades_to_empty(self) -> None:
        """P2: an oversized store must not stall the watcher before it reaches the
        durable inbox."""
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            big = '{"refused": {' + ",".join(
                f'"p{i}.md": "x"' for i in range(120000)
            ) + "}}"
            (root / "watcher-refusal-progress.json").write_text(big)
            self.assertGreater(
                len(big.encode()), watch_inbox.MAX_REFUSAL_PROGRESS_BYTES
            )
            with patch.object(watch_inbox, "agent_dir", return_value=root):
                self.assertEqual({}, watch_inbox.load_refusal_progress("claude"))


if __name__ == "__main__":
    unittest.main()
