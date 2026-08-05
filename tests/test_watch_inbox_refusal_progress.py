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
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import watch_inbox  # noqa: E402


class RefusalWindowTest(unittest.TestCase):
    def test_recent_packet_is_in_window(self) -> None:
        now = datetime(2026, 8, 5, 4, 0, 0)
        self.assertTrue(
            watch_inbox.within_refusal_window("2026-08-04T09-00-00_to-claude_x.md", now)
        )

    def test_old_packet_is_out_of_window(self) -> None:
        now = datetime(2026, 8, 5, 4, 0, 0)
        self.assertFalse(
            watch_inbox.within_refusal_window("2026-06-27T01-18-18_to-claude_x.md", now)
        )

    def test_boundary_is_inclusive_at_the_window_edge(self) -> None:
        now = datetime(2026, 8, 5, 4, 0, 0)
        edge = now - timedelta(days=watch_inbox.REFUSAL_WINDOW_DAYS)
        name = edge.strftime("%Y-%m-%dT%H-%M-%S") + "_to-claude_x.md"
        self.assertTrue(watch_inbox.within_refusal_window(name, now))

    def test_unparseable_name_is_never_hidden(self) -> None:
        """A message must never be dropped because its filename did not match a
        convention; fail OPEN on naming, fail closed on routing."""
        now = datetime(2026, 8, 5, 4, 0, 0)
        self.assertTrue(watch_inbox.within_refusal_window("weird-name.md", now))
        self.assertTrue(watch_inbox.within_refusal_window("", now))


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
            watch_inbox.save_refusal_progress("claude", {"a.md": "fp1"})
            self.assertEqual({"a.md": "fp1"}, watch_inbox.load_refusal_progress("claude"))

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


if __name__ == "__main__":
    unittest.main()
