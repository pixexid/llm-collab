"""GH-539: the watcher must make progress on repo-scope refusals.

Before this lane a refusal was emitted but never recorded, so every poll
re-decided and re-logged the same stale message — refusal work was O(unread) per
poll forever. These tests pin the four properties that fix it without turning the
change into backlog cleanup.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import watch_inbox  # noqa: E402


class RefusalRecheckWindowTest(unittest.TestCase):
    NOW = datetime(2026, 8, 8, 12, 0, 0)

    def _entry(self, path: str, *, repo_targets=None, mtime=1.0) -> dict:
        reason = "repo_mismatch"
        packet_repo = ["other"]
        project_id = "llm-collab"
        return {
            watch_inbox.progress_key(None, path): {
                "path": path,
                "fp": watch_inbox.refusal_fingerprint(
                    reason,
                    repo_targets,
                    packet_repo,
                    project_id,
                    project_id,
                ),
                "mtime": mtime,
                "reason": reason,
                "packet_repo_targets": packet_repo,
                "packet_project": project_id,
                "session_id": None,
                "session_repo_targets": repo_targets,
            }
        }

    def test_backlog_over_window_avoids_rechecking_old_packet_identity(self) -> None:
        from unittest.mock import patch

        old = "Chats/x/2026-07-01T00-00-00_to-claude_old.md"
        recent = "Chats/x/2026-08-08T00-00-00_to-claude_recent.md"
        progress = self._entry(old, repo_targets=["app"])
        progress.update(self._entry(recent, repo_targets=["app"]))
        with patch.object(watch_inbox, "_packet_mtime", return_value=1.0) as mtime:
            skipped = watch_inbox.terminal_refusal_paths(
                progress,
                ["app"],
                "llm-collab",
                refusal_recheck_window_days=7,
                now=self.NOW,
            )

        self.assertEqual({old, recent}, skipped)
        mtime.assert_called_once_with(recent)

    def test_new_arrival_is_considered_even_with_an_old_filename(self) -> None:
        recorded = "Chats/x/2026-07-01T00-00-00_to-claude_recorded.md"
        new = "Chats/x/2026-06-01T00-00-00_to-claude_new.md"
        skipped = watch_inbox.terminal_refusal_paths(
            self._entry(recorded, repo_targets=["app"], mtime=None),
            ["app"],
            "llm-collab",
            refusal_recheck_window_days=7,
            now=self.NOW,
        )

        self.assertEqual({recorded}, skipped)
        self.assertNotIn(new, skipped)

    def test_changed_fingerprint_escapes_even_with_an_old_filename(self) -> None:
        from unittest.mock import patch

        old = "Chats/x/2026-07-01T00-00-00_to-claude_old.md"
        progress = self._entry(old, repo_targets=["app"])
        progress[watch_inbox.progress_key(None, old)]["fp"] = "stale-fingerprint"
        with patch.object(
            watch_inbox,
            "_packet_mtime",
            side_effect=AssertionError("changed routing must escape before age"),
        ):
            skipped = watch_inbox.terminal_refusal_paths(
                progress,
                ["app"],
                "llm-collab",
                refusal_recheck_window_days=7,
                now=self.NOW,
            )

        self.assertEqual(set(), skipped)

    def test_absent_invalid_or_unparseable_window_inputs_recheck(self) -> None:
        from unittest.mock import patch

        old = "Chats/x/2026-07-01T00-00-00_to-claude_old.md"
        malformed = "Chats/x/not-a-packet-timestamp_to-claude_old.md"
        self.assertIsNone(watch_inbox.parse_refusal_recheck_window_days(None))
        self.assertIsNone(watch_inbox.parse_refusal_recheck_window_days("invalid"))
        with patch.object(watch_inbox, "_packet_mtime", return_value=2.0) as mtime:
            no_window = watch_inbox.terminal_refusal_paths(
                self._entry(old, repo_targets=["app"]),
                ["app"],
                "llm-collab",
                refusal_recheck_window_days=None,
                now=self.NOW,
            )
            malformed_packet = watch_inbox.terminal_refusal_paths(
                self._entry(malformed, repo_targets=["app"]),
                ["app"],
                "llm-collab",
                refusal_recheck_window_days=7,
                now=self.NOW,
            )

        self.assertEqual(set(), no_window)
        self.assertEqual(set(), malformed_packet)
        self.assertEqual([old, malformed], [call.args[0] for call in mtime.call_args_list])


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
                "path": "a.md",
                "session_repo_targets": ["app"],
                "session_scope": None,
            }
            key = watch_inbox.progress_key(None, "a.md")
            watch_inbox.save_refusal_progress("claude", {key: entry})
            self.assertEqual({key: entry}, watch_inbox.load_refusal_progress("claude"))

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

    def test_store_at_exact_byte_cap_round_trips_completely(self) -> None:
        entry = {
            "fp": "fp1",
            "mtime": None,
            "session_id": "SESSION-A",
            "reason": "repo_mismatch",
            "packet_repo_targets": ["other"],
            "packet_project": "llm-collab",
            "path": "",
            "session_repo_targets": ["app"],
            "session_scope": ["'app'"],
        }
        refused = {"SESSION-A\u0000Chats/x/a.md": entry}
        base_size = len(
            json.dumps({"version": 1, "refused": refused}, indent=2).encode("utf-8")
        )
        cap = base_size + 64
        entry["path"] = "x" * 64
        self.assertEqual(
            cap,
            len(
                json.dumps({"version": 1, "refused": refused}, indent=2).encode(
                    "utf-8"
                )
            ),
        )

        from unittest.mock import patch

        with self._patch_dir(), patch.object(
            watch_inbox, "MAX_REFUSAL_PROGRESS_BYTES", cap
        ):
            self.assertTrue(watch_inbox.save_refusal_progress("claude", refused))
            self.assertEqual(cap, watch_inbox.refusal_progress_path("claude").stat().st_size)
            self.assertEqual(refused, watch_inbox.load_refusal_progress("claude"))

    def test_oversized_store_is_visibly_refused_and_preserves_last_store(self) -> None:
        from unittest.mock import patch

        with self._patch_dir():
            self.assertTrue(
                watch_inbox.save_refusal_progress("claude", {"kept.md": "fp1"})
            )
            path = watch_inbox.refusal_progress_path("claude")
            before = path.read_bytes()
            cap = len(before) + 1
            oversized = {"new.md": "x" * cap}
            warning = io.StringIO()
            with patch.object(
                watch_inbox, "MAX_REFUSAL_PROGRESS_BYTES", cap
            ), redirect_stderr(warning):
                self.assertFalse(
                    watch_inbox.save_refusal_progress("claude", oversized)
                )

            self.assertIn("serialized bytes exceeds", warning.getvalue())
            self.assertEqual(before, path.read_bytes())
            self.assertLessEqual(path.stat().st_size, cap)
            self.assertIn("kept.md", watch_inbox.load_refusal_progress("claude"))
            self.assertEqual([], sorted(self.root.glob("*.tmp")))

    def test_atomic_replace_failure_preserves_last_readable_store(self) -> None:
        from unittest.mock import patch

        with self._patch_dir():
            self.assertTrue(
                watch_inbox.save_refusal_progress("claude", {"kept.md": "fp1"})
            )
            path = watch_inbox.refusal_progress_path("claude")
            before = path.read_bytes()
            with patch.object(watch_inbox.os, "replace", side_effect=OSError("boom")):
                self.assertFalse(
                    watch_inbox.save_refusal_progress("claude", {"new.md": "fp2"})
                )

            self.assertEqual(before, path.read_bytes())
            self.assertIn("kept.md", watch_inbox.load_refusal_progress("claude"))
            self.assertEqual([], sorted(self.root.glob("*.tmp")))

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
                        "path": None,
                        "session_repo_targets": None,
                        "session_scope": None,
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
            watch_inbox.progress_key(None, path): {
                "path": path,
                "fp": fp,
                "mtime": mtime,
                "reason": reason,
                "packet_repo_targets": packet_repo,
                "packet_project": packet_project,
                "session_id": None,
                "session_repo_targets": repo_targets,
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
            watch_inbox.progress_key(None, path): {
                "path": path,
                "fp": fp,
                "mtime": None,
                "reason": reason,
                "packet_repo_targets": packet_repo,
                "packet_project": packet_project,
                "session_id": None,
                "session_repo_targets": repo_targets,
            }
        }
        # skips before any restart
        self.assertEqual(
            {path}, watch_inbox.terminal_refusal_paths(live, repo_targets, project_id)
        )
        with self._patch_dir():
            inbox = self.root / "inbox.json"
            inbox.write_text(json.dumps({"unread": [path], "read": []}))
            inbox_before = inbox.read_bytes()
            watch_inbox.save_refusal_progress("claude", live)
            reloaded = watch_inbox.load_refusal_progress("claude")
            self.assertEqual(inbox_before, inbox.read_bytes())
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
            watch_inbox.progress_key(None, path): {
                "path": path,
                "fp": fp,
                "mtime": None,
                "reason": "repo_mismatch",
                "packet_repo_targets": ["other"],
                "packet_project": "amiga",
                "session_id": None,
                "session_repo_targets": ["app"],
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
            watch_inbox.progress_key("SESSION-APP", path): {
                "path": path,
                "fp": fp_app,
                "mtime": None,
                "reason": "repo_mismatch",
                "packet_repo_targets": ["docs"],
                "packet_project": "llm-collab",
                "session_id": "SESSION-APP",
                "session_repo_targets": ["app"],
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


class TwoSessionsSamePathTest(unittest.TestCase):
    """Codex residual finding 1: keying progress on path alone let two sessions
    refusing the SAME packet in one poll overwrite each other, so the loser
    repeated its refusal on every later poll."""

    def _entry(self, path, session_id, repo_targets):
        return {
            "path": path,
            "session_id": session_id,
            "session_repo_targets": repo_targets,
            "fp": watch_inbox.refusal_fingerprint(
                "repo_mismatch", repo_targets, ["zzz"], "llm-collab", "llm-collab", session_id
            ),
            "mtime": None,
            "reason": "repo_mismatch",
            "packet_repo_targets": ["zzz"],
            "packet_project": "llm-collab",
        }

    def test_both_sessions_skip_on_the_next_poll(self) -> None:
        path = "Chats/x/shared.md"
        progress = {
            watch_inbox.progress_key("SESSION-A", path): self._entry(path, "SESSION-A", ["app"]),
            watch_inbox.progress_key("SESSION-B", path): self._entry(path, "SESSION-B", ["docs"]),
        }
        self.assertEqual(
            {path},
            watch_inbox.terminal_refusal_paths(progress, ["app"], "llm-collab", "SESSION-A"),
        )
        self.assertEqual(
            {path},
            watch_inbox.terminal_refusal_paths(progress, ["docs"], "llm-collab", "SESSION-B"),
        )

    def test_a_third_session_still_evaluates(self) -> None:
        """The accepting session must not inherit either refusal."""
        path = "Chats/x/shared.md"
        progress = {
            watch_inbox.progress_key("SESSION-A", path): self._entry(path, "SESSION-A", ["app"]),
        }
        self.assertEqual(
            set(),
            watch_inbox.terminal_refusal_paths(progress, ["other"], "llm-collab", "SESSION-C"),
        )


class BoundedReadSeamTest(unittest.TestCase):
    """Codex residual finding 2: stat-then-read is two objects and a growth race,
    and a plain open() on a writer-less FIFO blocks forever BEFORE any cap."""

    def test_non_regular_file_degrades_to_empty(self) -> None:
        import os
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / "watcher-refusal-progress.json"
            os.mkfifo(fifo)
            with patch.object(watch_inbox, "agent_dir", return_value=root):
                self.assertEqual({}, watch_inbox.load_refusal_progress("claude"))


class ReRegisteredScopeTest(unittest.TestCase):
    """Codex P2: a refusal recorded under the OLD session scope must not survive
    the session being re-registered with a corrected scope."""

    def test_corrected_stored_session_scope_reopens(self) -> None:
        path = "Chats/x/p.md"
        old_scope = ["app"]
        entry = {
            "path": path,
            "session_id": "SESSION-A",
            "session_repo_targets": old_scope,
            "fp": watch_inbox.refusal_fingerprint(
                "repo_mismatch", old_scope, ["docs"], "llm-collab", "llm-collab", "SESSION-A"
            ),
            "mtime": None,
            "reason": "repo_mismatch",
            "packet_repo_targets": ["docs"],
            "packet_project": "llm-collab",
        }
        progress = {watch_inbox.progress_key("SESSION-A", path): entry}
        # unchanged scope: still terminal
        self.assertEqual(
            {path},
            watch_inbox.terminal_refusal_paths(progress, old_scope, "llm-collab", "SESSION-A"),
        )
        # same session re-registered with a corrected scope: must re-evaluate
        self.assertEqual(
            set(),
            watch_inbox.terminal_refusal_paths(progress, ["docs"], "llm-collab", "SESSION-A"),
        )


class SkipBeforeReadTest(unittest.TestCase):
    """Codex P2: skipped packets must not be opened or charged against
    MAX_DISPATCH_INBOX_BYTES, or a large refusal backlog starves eligible mail."""

    def test_skipped_packet_body_is_never_read(self) -> None:
        import _session_autobridge as sab
        from unittest.mock import patch

        reads: list = []

        def fake_read(path, limit, **kwargs):
            reads.append(str(path))
            name = str(path)
            if name.endswith("inbox.json"):
                return b'{"unread": ["Chats/x/skipme.md", "Chats/x/keep.md"]}'
            return b"---\nproject_id: llm-collab\n---\n"

        with patch.object(sab, "read_regular_file_bounded", side_effect=fake_read), patch.object(
            sab, "agent_inbox_path", return_value=Path("/tmp/inbox.json")
        ):
            sab.bounded_unread_messages("claude", skip_paths={"Chats/x/skipme.md"})

        self.assertFalse(
            any("skipme" in r for r in reads),
            "skipped packet body was read and charged against the budget",
        )
        self.assertTrue(any("keep" in r for r in reads), "eligible packet was not reached")


class LoadedSessionScopeTest(unittest.TestCase):
    """Codex: _session_repo_scope_matches consults BOTH the loaded session scope
    and the invocation scope. My previous regression varied the INVOCATION scope,
    so it never exercised the loaded-session correction path — the defect it was
    meant to catch would have passed."""

    def _entry(self, path, session_scope):
        return {
            "path": path,
            "session_id": "SESSION-A",
            "session_repo_targets": ["app"],          # invocation scope, held FIXED
            "session_scope": watch_inbox._stable_targets(session_scope),
            "fp": watch_inbox.refusal_fingerprint(
                "repo_mismatch", ["app"], ["zzz"], "llm-collab", "llm-collab",
                "SESSION-A", session_scope,
            ),
            "mtime": None,
            "reason": "repo_mismatch",
            "packet_repo_targets": ["zzz"],
            "packet_project": "llm-collab",
        }

    def test_unchanged_loaded_scope_stays_terminal(self) -> None:
        path = "Chats/x/p.md"
        progress = {watch_inbox.progress_key("SESSION-A", path): self._entry(path, ["app"])}
        self.assertEqual(
            {path},
            watch_inbox.terminal_refusal_paths(
                progress, ["app"], "llm-collab", "SESSION-A", ["app"]
            ),
        )

    def test_only_the_stored_session_scope_changes_and_it_reopens(self) -> None:
        """Invocation scope stays 'app' throughout; only session[repo_targets]
        moves app -> docs. That is the case the previous test could not reach."""
        path = "Chats/x/p.md"
        progress = {watch_inbox.progress_key("SESSION-A", path): self._entry(path, ["app"])}
        self.assertEqual(
            set(),
            watch_inbox.terminal_refusal_paths(
                progress, ["app"], "llm-collab", "SESSION-A", ["docs"]
            ),
        )


class RewriteBetweenReadAndRecordTest(unittest.TestCase):
    """Codex P2 (TOCTOU): read()-then-stat() is two operations on two possibly
    different objects. A rewrite landing between them yields a STABLE new mtime
    that the next poll also observes, so the corrected packet is skipped forever.

    The earlier version of this test incremented every fake stat call, so the
    "next poll" never saw a stable value — it could not model the actual failure.
    """

    def test_identity_comes_from_the_read_descriptor_not_a_later_stat(self) -> None:
        import _session_autobridge as sab
        from unittest.mock import patch

        READ_MTIME, REWRITTEN_MTIME = 100.0, 200.0

        class FakeInfo:
            st_mode = 0o100644
            st_size = 64
            st_mtime = READ_MTIME

        def fake_open(path, flags):
            return 99

        reads = [b"---\nproject_id: llm-collab\n---\n", b""]

        def fake_read(fd, n):
            return reads.pop(0) if reads else b""

        # Every path.stat() AFTER the read reports the rewritten value, and keeps
        # reporting it — a stable observation the next poll would also see.
        class StableRewrittenStat:
            st_mtime = REWRITTEN_MTIME

        with patch.object(sab.os, "open", side_effect=fake_open), patch.object(
            sab.os, "fstat", return_value=FakeInfo()
        ), patch.object(sab.os, "read", side_effect=fake_read), patch.object(
            sab.os, "close"
        ), patch.object(Path, "stat", lambda self: StableRewrittenStat()):
            payload, identity = sab.read_regular_file_bounded_with_identity(
                Path("/tmp/p.md"), 4096
            )

        self.assertEqual(
            READ_MTIME,
            identity,
            "identity must come from the fstat on the read descriptor, not a later stat",
        )
        self.assertNotEqual(
            REWRITTEN_MTIME,
            identity,
            "a stable post-read rewrite must not be certified as the parsed version",
        )

    def test_record_uses_the_supplied_mtime_over_a_fresh_stat(self) -> None:
        """The decisive assertion: given an explicit read-time mtime, the stored
        entry must carry THAT value, never a fresh stat of the rewritten file."""
        progress: dict = {}
        stats: dict = {}

        # Mirror record_refusal's contract via terminal_refusal_paths round trip:
        # an entry written with a read-time mtime of 1.0 must not match a file
        # whose current mtime differs.
        path = "Chats/x/p.md"
        entry = {
            "path": path,
            "session_id": None,
            "session_repo_targets": ["app"],
            "session_scope": watch_inbox._stable_targets(None),
            "fp": watch_inbox.refusal_fingerprint(
                "repo_mismatch", ["app"], ["zzz"], "llm-collab", "llm-collab", None, None
            ),
            "mtime": 1.0,
            "reason": "repo_mismatch",
            "packet_repo_targets": ["zzz"],
            "packet_project": "llm-collab",
        }
        progress[watch_inbox.progress_key(None, path)] = entry
        # The real file does not exist, so _packet_mtime returns None != 1.0:
        # the rewritten packet re-opens instead of being skipped.
        self.assertEqual(
            set(),
            watch_inbox.terminal_refusal_paths(progress, ["app"], "llm-collab", None, None),
        )


class PerActionRecheckIdentityTest(unittest.TestCase):
    PACKET = b"---\nproject_id: llm-collab\nrepo_targets: [other]\n---\nbody\n"

    def _recorded_refusal(
        self,
        read_mtime: float | None,
        later_mtime: float,
        *,
        packet: bytes | None = None,
        read_error: Exception | None = None,
    ):
        import tempfile
        from unittest.mock import patch

        packet = self.PACKET if packet is None else packet

        def read_packet(*_args):
            if read_error is not None:
                raise read_error
            return packet, read_mtime

        relative_path = "Chats/x/p.md"
        session = {"session_id": "SESSION-A", "repo_targets": ["app"]}
        action = {
            "effective_action": "runtime_trigger",
            "message_path": relative_path,
            "runtime_result": {"returncode": 0},
        }
        progress: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / relative_path
            packet_path.parent.mkdir(parents=True)
            packet_path.write_bytes(packet)
            with patch.object(
                watch_inbox, "ROOT", root
            ), patch.object(
                watch_inbox, "_bootstrap_bb_before_dispatch", return_value=[]
            ), patch.object(
                watch_inbox, "autobridge_session_ids", return_value=["SESSION-A"]
            ), patch.object(
                watch_inbox, "load_session", return_value=session
            ), patch.object(
                watch_inbox, "session_has_exact_canonical_binding", return_value=True
            ), patch.object(
                watch_inbox, "_observe_bb_session"
            ), patch.object(
                watch_inbox,
                "dispatch_session",
                return_value={"actions": [action], "repo_scope_refused": []},
            ), patch.object(
                watch_inbox, "runtime_delivery_accepted", return_value=True
            ), patch.object(
                watch_inbox,
                "read_regular_file_bounded_with_identity",
                side_effect=read_packet,
            ), patch.object(
                watch_inbox, "_packet_mtime", return_value=later_mtime
            ), patch.object(
                watch_inbox, "emit"
            ), patch.object(
                watch_inbox, "mark_messages_read"
            ) as mark_read:
                consumed = watch_inbox.dispatch_autobridge(
                    "claude",
                    False,
                    project_id="llm-collab",
                    repo_targets=["app"],
                    refusal_progress=progress,
                )
                terminal = watch_inbox.terminal_refusal_paths(
                    progress,
                    ["app"],
                    "llm-collab",
                    "SESSION-A",
                    ["app"],
                )

        self.assertEqual([], consumed)
        mark_read.assert_not_called()
        entry = progress[watch_inbox.progress_key("SESSION-A", relative_path)]
        self.assertEqual("route_ambiguous", entry["reason"])
        return entry, terminal, relative_path

    def test_stable_post_read_rewrite_does_not_record_the_later_identity(self) -> None:
        entry, terminal, _ = self._recorded_refusal(100.0, 200.0)

        self.assertEqual(100.0, entry["mtime"])
        self.assertEqual(set(), terminal)

    def test_ordinary_read_records_the_identity_matching_the_parsed_bytes(self) -> None:
        entry, terminal, path = self._recorded_refusal(100.0, 100.0)

        self.assertEqual(100.0, entry["mtime"])
        self.assertEqual({path}, terminal)

    def test_read_failure_does_not_fallback_to_a_fresh_path_identity(self) -> None:
        entry, terminal, _ = self._recorded_refusal(
            None,
            200.0,
            read_error=watch_inbox.UnreadableFile("read failed"),
        )

        self.assertIsNone(entry["mtime"])
        self.assertEqual(set(), terminal)

    def test_parse_failure_keeps_the_read_descriptor_identity(self) -> None:
        entry, terminal, _ = self._recorded_refusal(100.0, 200.0, packet=b"\xff")

        self.assertEqual(100.0, entry["mtime"])
        self.assertEqual(set(), terminal)
