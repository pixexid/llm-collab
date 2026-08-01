from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _helpers  # noqa: E402


class RegistryBoundedReadTest(unittest.TestCase):
    """GH-467: ensure_project()/load_agents() read the shared registries through
    one bounded, fail-closed seam so a huge/corrupt/non-regular file cannot blow
    memory or yield a partial result."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gh467-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._orig = (_helpers.AGENTS_FILE, _helpers.PROJECTS_FILE)
        _helpers.AGENTS_FILE = self.root / "agents.json"
        _helpers.PROJECTS_FILE = self.root / "projects.json"
        self._reset_caches()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        _helpers.AGENTS_FILE, _helpers.PROJECTS_FILE = self._orig
        self._reset_caches()

    def _reset_caches(self) -> None:
        _helpers._agents_cache = None
        _helpers._projects_cache = None

    def _oversized(self, key: str, member: dict) -> str:
        member = {**member, "pad": "A" * _helpers.MAX_REGISTRY_FILE_BYTES}
        return json.dumps({key: [member]})

    def test_normal_amiga_and_non_amiga_paths(self):
        _helpers.AGENTS_FILE.write_text(json.dumps({"agents": [{"id": "codex"}]}))
        _helpers.PROJECTS_FILE.write_text(
            json.dumps({"projects": [{"id": "amiga"}, {"id": "nuvyr"}]}))
        self.assertEqual("codex", _helpers.get_agent("codex")["id"])
        _helpers.ensure_project("amiga")       # amiga: no exit
        _helpers.ensure_project("nuvyr")       # non-amiga: no exit

    def test_oversized_agents_fails_closed_with_no_partial_result(self):
        _helpers.AGENTS_FILE.write_text(self._oversized("agents", {"id": "codex"}))
        with self.assertRaises(SystemExit):
            _helpers.load_agents()
        self.assertIsNone(_helpers._agents_cache,
                          "an over-limit read must not cache a partial/claimed result")

    def test_oversized_projects_fails_closed_via_ensure_project(self):
        _helpers.PROJECTS_FILE.write_text(self._oversized("projects", {"id": "amiga"}))
        with self.assertRaises(SystemExit):
            _helpers.ensure_project("amiga")
        self.assertIsNone(_helpers._projects_cache,
                          "an over-limit read must not cache a partial/claimed result")

    def test_corrupt_json_fails_closed(self):
        _helpers.AGENTS_FILE.write_text("{not valid json")
        with self.assertRaises(SystemExit):
            _helpers.load_agents()
        self.assertIsNone(_helpers._agents_cache)

    def test_non_regular_file_is_refused(self):
        # A non-regular path (here a directory; a FIFO/device is the hung-mount
        # case) must be refused before any read — the regular-file guard is the
        # sole authority, so removing it surfaces a raw read error, not a refusal.
        os.mkdir(_helpers.AGENTS_FILE)
        with self.assertRaises(SystemExit):
            _helpers.load_agents()

    def test_writerless_fifo_does_not_block(self):
        # O_NONBLOCK open + regular-file guard: a writer-less FIFO must be refused
        # promptly, never blocking inside open().
        os.mkfifo(_helpers.AGENTS_FILE)
        with self.assertRaises(SystemExit):
            _helpers.load_agents()


if __name__ == "__main__":
    unittest.main()
