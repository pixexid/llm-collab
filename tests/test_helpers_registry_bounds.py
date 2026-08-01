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
        # A VALID JSON document followed by whitespace past the cap: the byte cap is
        # the sole reason to refuse (the prefix parses fine), so a dropped cap guard
        # is detected rather than masked by a JSON-parse error on a truncated blob.
        valid = json.dumps({key: [member]})
        return valid + " " * (_helpers.MAX_REGISTRY_FILE_BYTES + 1)

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

    def test_grow_past_cap_after_fstat_is_refused_not_truncated(self):
        # The read is bounded by the CAP (not a recorded fstat size), so a registry
        # that is/grows over the limit is refused, never parsed as a valid prefix.
        # Simulate an fstat that under-reports while the file is actually over-cap.
        from unittest.mock import patch
        valid = json.dumps({"agents": [{"id": "codex"}]})
        # Valid JSON prefix, then whitespace past the cap: reading to the cap must
        # refuse; reading only to the (under-reported) fstat size would parse the
        # valid prefix as complete.
        _helpers.AGENTS_FILE.write_text(valid + " " * (_helpers.MAX_REGISTRY_FILE_BYTES + 1))
        real_fstat = os.fstat
        under = len(valid.encode())

        class _UnderReport:
            def __init__(self, real):
                self.st_mode = real.st_mode
                self.st_size = under  # lies: claims just the valid prefix

        with patch.object(_helpers.os, "fstat", lambda fd: _UnderReport(real_fstat(fd))):
            with self.assertRaises(SystemExit):
                _helpers.load_agents()
        self.assertIsNone(_helpers._agents_cache)

    def test_utf16_registry_is_refused_matching_the_daemon(self):
        # json.loads(bytes) would auto-detect UTF-16; the daemon decodes the same
        # authority registry strictly as UTF-8, so bin must too or authority splits.
        payload = json.dumps({"agents": [{"id": "codex"}]}).encode("utf-16")
        _helpers.AGENTS_FILE.write_bytes(payload)
        with self.assertRaises(SystemExit):
            _helpers.load_agents()
        self.assertIsNone(_helpers._agents_cache)

    def test_missing_agents_is_fatal(self):
        # No agents.json (and no pre-read .exists() stat): the bounded read
        # surfaces absence as FileNotFoundError and load_agents exits.
        with self.assertRaises(SystemExit):
            _helpers.load_agents()

    def test_missing_projects_yields_empty_list(self):
        # An absent projects.json is not an error — it yields [].
        self.assertEqual([], _helpers.load_projects())
        _helpers.ensure_project(None)  # allow_none: no exit

    def test_read_deadline_covers_a_stalled_open(self):
        # The earliest filesystem op (open) — the one that hangs on a stalled mount
        # and previously sat behind an unbounded .exists() — must be under the
        # deadline too, not just the read.
        import time
        from unittest.mock import patch
        _helpers.AGENTS_FILE.write_text(json.dumps({"agents": [{"id": "codex"}]}))
        real_open = os.open

        def _stalled_open(path, flags, *a, **k):
            time.sleep(1.0)  # exceeds the deadline; SIGALRM must interrupt it
            return real_open(path, flags, *a, **k)

        started = time.monotonic()
        with patch.object(_helpers, "REGISTRY_READ_DEADLINE_SECONDS", 0.3), \
             patch.object(_helpers.os, "open", _stalled_open):
            with self.assertRaises(SystemExit):
                _helpers.load_agents()
        self.assertLess(time.monotonic() - started, 0.9,
                        "a stalled open must fail at the deadline, not after the full stall")

    def test_read_deadline_fails_closed_on_a_stalled_read(self):
        # O_NONBLOCK does not bound a regular-file read on a hung mount; the SIGALRM
        # deadline must interrupt a stalled read and fail closed, not hang the caller.
        import time
        from unittest.mock import patch
        _helpers.AGENTS_FILE.write_text(json.dumps({"agents": [{"id": "codex"}]}))
        real_read = os.read
        calls = {"n": 0}

        def _stalled_read(fd, n):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(1.0)  # exceeds the deadline; SIGALRM must interrupt it
            return real_read(fd, n)

        started = time.monotonic()
        with patch.object(_helpers, "REGISTRY_READ_DEADLINE_SECONDS", 0.3), \
             patch.object(_helpers.os, "read", _stalled_read):
            with self.assertRaises(SystemExit):
                _helpers.load_agents()
        self.assertLess(time.monotonic() - started, 0.9,
                        "must fail at the deadline, not after the full stall")
        self.assertIsNone(_helpers._agents_cache)

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
