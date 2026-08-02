"""GH-457 regression proofs for Claude native receiving.

Proof-only (no watcher/daemon changes). Each proof exercises the real authority
function and fails on a direct mutation of the invariant it protects:

- same-project isolation  -> binding_scoped_message_matches_session (binding_id gate)
- duplicate-event idempotency -> processed_message_blocks_dispatch (seen-path gate)

Compaction continuity is proved in
test_session_autobridge.py::test_compaction_continuity_preserves_canonical_binding
via the ledger-backed resolve_active_canonical_binding path.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _session_autobridge as sab

APP = ["app"]


def _bound_session(binding_id: str, generation: str = "1") -> dict:
    return {
        "project_id": "amiga",
        "repo_targets": APP,
        "binding_id": binding_id,
        "binding_generation": generation,
    }


def _packet(target_binding_id, generation: str = "1") -> dict:
    return {
        "frontmatter": {
            "project_id": "amiga",
            "repo_targets": APP,
            "target_binding_id": target_binding_id,
            "target_binding_generation": generation,
        }
    }


class SameProjectIsolationProof(unittest.TestCase):
    def test_worker_cannot_consume_another_workers_binding_packet(self):
        packet_for_a = _packet("binding-A")
        # Worker A owns binding-A: its own binding-targeted packet matches.
        ok_a, reason_a = sab.binding_scoped_message_matches_session(
            _bound_session("binding-A"), packet_for_a
        )
        self.assertTrue(ok_a, reason_a)
        self.assertEqual("explicit_target_match", reason_a)
        # Worker B (binding-B), SAME project, must NOT match A's packet — this is
        # the isolation invariant (drop the binding_id gate and B consumes A).
        ok_b, reason_b = sab.binding_scoped_message_matches_session(
            _bound_session("binding-B"), packet_for_a
        )
        self.assertFalse(ok_b)
        self.assertEqual(sab.ROUTE_AMBIGUOUS_REASON, reason_b)

    def test_generic_null_target_packet_does_not_reach_a_bound_worker(self):
        ok, reason = sab.binding_scoped_message_matches_session(
            _bound_session("binding-A"), _packet(None)
        )
        self.assertFalse(ok)
        self.assertEqual(sab.ROUTE_AMBIGUOUS_REASON, reason)


class DuplicateEventIdempotencyProof(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        import _helpers

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "collab.config.json").write_text(json.dumps({
            "workspace_name": "test", "schema_version": 2,
            "projects_root": str(root), "notifications_enabled": False,
        }))
        (root / "agents.json").write_text(json.dumps({"agents": [
            {"id": "claude", "display_name": "claude",
             "activation": {"type": "cli_session", "watcher_enabled": True}},
        ]}))
        self._orig_config = _helpers.CONFIG_FILE
        _helpers.CONFIG_FILE = root / "collab.config.json"
        _helpers._config_cache = None
        self.addCleanup(self._restore_config)

    def _restore_config(self):
        import _helpers
        _helpers.CONFIG_FILE = self._orig_config
        _helpers._config_cache = None

    def test_reseen_packet_is_blocked_but_a_fresh_one_dispatches(self):
        # A plain (no-binding, manual-mode) session needs no canonical
        # materialization, so the seen-path set alone gates re-dispatch.
        session = {"agent_id": "claude", "mode": "manual",
                   "processed_messages": ["Chats/p1.md"]}
        seen = {"Chats/p1.md"}
        reseen = {"path": "Chats/p1.md", "frontmatter": {}}
        fresh = {"path": "Chats/p2.md", "frontmatter": {}}
        # Repeated watcher notification for an already-processed packet is blocked.
        self.assertTrue(sab.processed_message_blocks_dispatch(session, reseen, seen))
        # A genuinely new packet is NOT blocked (drop the seen gate and this fails).
        self.assertFalse(sab.processed_message_blocks_dispatch(session, fresh, seen))


if __name__ == "__main__":
    unittest.main()
