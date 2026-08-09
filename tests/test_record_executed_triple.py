from __future__ import annotations
import sys as _grsys; from pathlib import Path as _grPath
_grsys.path.insert(0, str(_grPath(__file__).resolve().parent)); import _runtime_gate_testkit  # noqa: E402,F401  GH-503: deterministic gate-bypass install (any run form)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "record_executed_triple.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))

PY = sys.executable


def run_record(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(workspace), timeout=60,
    )


def rows_for(workspace: Path, project: str) -> list[dict]:
    path = workspace / "records" / "executed-triples" / f"{project}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def file_bytes(path: Path) -> bytes:
    return path.read_bytes()


class RecordExecutedTripleTest(unittest.TestCase):
    """Each test runs in an isolated temp workspace so find_workspace_root() anchors
    ROOT at the temp dir (collab.config.json marker) and records/ is created there."""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="lc-et-", dir="/tmp"))
        (self.workspace / "collab.config.json").write_text("{}", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.workspace, True)

    # -- resolved direction -------------------------------------------------

    def test_resolved_triple_recorded_exactly_once_per_thread(self) -> None:
        """A resolved triple is recorded exactly once per thread: a second invocation
        for the same thread upserts (one row), a different thread adds a second row."""
        r1 = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_A",
                        "--provider", "pi", "--model", "zai/glm-5.2",
                        "--reasoning-level", "high", "--source", "client/turn/requested")
        self.assertEqual(0, r1.returncode, r1.stderr[:500])

        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows), "first record produces exactly one row")
        row = rows[0]
        self.assertEqual("thr_A", row["thread_id"])
        self.assertEqual("resolved", row["status"])
        self.assertEqual("zai/glm-5.2", row["model"])
        self.assertEqual("high", row["reasoning_level"])
        self.assertEqual("client/turn/requested", row["source"])
        self.assertEqual("pi", row["provider"])

        # Same thread again (event re-fire / manual re-run) must NOT duplicate.
        r2 = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_A",
                        "--provider", "pi", "--model", "zai/glm-5.2",
                        "--reasoning-level", "high", "--source", "client/turn/requested")
        self.assertEqual(0, r2.returncode, r2.stderr[:500])
        self.assertEqual(1, len(rows_for(self.workspace, "amiga")), "re-record upserts, no duplicate")

        # A second thread adds a second row; the first is unchanged in count.
        run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_B",
                   "--provider", "codex", "--model", "kimi-coding/k3",
                   "--reasoning-level", "high", "--source", "client/thread/start")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(2, len(rows), "two distinct threads => two rows")
        self.assertEqual({"thr_A", "thr_B"}, {r["thread_id"] for r in rows})

    def test_resolved_values_not_a_mutable_reference(self) -> None:
        """GH-617: the row stores the resolved VALUES, never a preset name that a later
        edit could retroactively re-resolve. No preset/name field exists, and a
        re-resolution replaces the row with the new resolved value (not a stale name)."""
        run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_X",
                   "--provider", "pi", "--model", "pi/gpt-5.4-mini",
                   "--reasoning-level", "low", "--source", "client/turn/requested")
        row = rows_for(self.workspace, "amiga")[0]
        self.assertNotIn("preset", json.dumps(row), "no preset-name reference is persisted")
        self.assertEqual("pi/gpt-5.4-mini", row["model"])

        # The resolved value changes (preset edited / different resolution): the row
        # reflects the NEW resolved value, still one row, still values not names.
        run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_X",
                   "--provider", "pi", "--model", "zai/glm-5.2",
                   "--reasoning-level", "high", "--source", "client/turn/requested")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows))
        self.assertEqual("zai/glm-5.2", rows[0]["model"], "the resolved value replaced the prior one")
        self.assertNotIn("preset", json.dumps(rows[0]))

    def test_absent_provider_recorded_as_null(self) -> None:
        run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_P",
                   "--model", "m", "--reasoning-level", "low", "--source", "client/turn/requested")
        row = rows_for(self.workspace, "amiga")[0]
        self.assertIsNone(row["provider"])

    # -- unresolved direction ----------------------------------------------

    def test_unresolvable_profile_records_typed_failure(self) -> None:
        """An unresolvable profile records a typed failure row rather than silently
        omitting it. An absent row (zero rows) and a failed resolution (one unresolved
        row) are distinguishable by row count and status."""
        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_U",
                       "--provider", "pi", "--unresolved", "profile_not_resolved")
        self.assertEqual(0, r.returncode, r.stderr[:500])

        # Absent row baseline: a fresh project has zero rows.
        self.assertEqual(0, len(rows_for(self.workspace, "other")))

        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows), "a failed resolution still produces a row")
        row = rows[0]
        self.assertEqual("unresolved", row["status"])
        self.assertEqual("profile_not_resolved", row["failure_reason"])
        self.assertEqual("thr_U", row["thread_id"])
        self.assertEqual("pi", row["provider"])
        self.assertNotIn("model", row, "an unresolved row carries no model")

        # Distinguishable from a resolved row for a different thread in the same file.
        run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_R",
                   "--provider", "pi", "--model", "m", "--reasoning-level", "low",
                   "--source", "client/turn/requested")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(2, len(rows))
        by_status = {row["status"] for row in rows}
        self.assertEqual({"resolved", "unresolved"}, by_status)

    def test_resolution_error_carries_detail(self) -> None:
        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_E",
                       "--unresolved", "profile_resolution_error",
                       "--failure-detail", "loopback timeout")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        row = rows_for(self.workspace, "amiga")[0]
        self.assertEqual("profile_resolution_error", row["failure_reason"])
        self.assertEqual("loopback timeout", row["failure_detail"])

    # -- project scoping ----------------------------------------------------

    def test_separate_projects_do_not_collide(self) -> None:
        run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_1",
                   "--provider", "pi", "--model", "m1", "--reasoning-level", "low",
                   "--source", "client/turn/requested")
        run_record(self.workspace, "--project", "nuvyr_app", "--thread-id", "thr_1",
                   "--provider", "codex", "--model", "m2", "--reasoning-level", "high",
                   "--source", "client/turn/requested")
        # Same thread_id, different project => two files, no cross-contamination.
        amiga = rows_for(self.workspace, "amiga")
        nuvyr = rows_for(self.workspace, "nuvyr_app")
        self.assertEqual(1, len(amiga))
        self.assertEqual(1, len(nuvyr))
        self.assertEqual("m1", amiga[0]["model"])
        self.assertEqual("m2", nuvyr[0]["model"])
        self.assertEqual("amiga", amiga[0]["project_id"])
        self.assertEqual("nuvyr_app", nuvyr[0]["project_id"])

    def test_project_id_must_be_a_safe_path_segment(self) -> None:
        for bad in ("../escape", "a/b", "", "  ", ".hidden", "a b"):
            with self.subTest(bad=bad):
                r = run_record(self.workspace, "--project", bad, "--thread-id", "thr",
                               "--model", "m", "--reasoning-level", "low",
                               "--source", "client/turn/requested")
                self.assertNotEqual(0, r.returncode, f"{bad!r} should be rejected")
        # Nothing was written outside records/executed-triples/.
        self.assertFalse((self.workspace / "records").exists() or any(p.suffix == ".jsonl" for p in self.workspace.rglob("*.jsonl")),
                         "no record file created for a rejected project_id")

    # -- bound / fail-closed ------------------------------------------------

    def test_oversized_log_is_refused_without_partial_rewrite(self) -> None:
        """AGENTS.md 'Bounded work fails closed and never truncates': a log exceeding
        the budget raises with no partial state. The original bytes are untouched."""
        import record_executed_triple as mod  # type: ignore
        path = self.workspace / "records" / "executed-triples" / "amiga.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"thread_id": "old", "status": "resolved",
                               "pad": "x" * (mod.RECORD_FILE_BUDGET_BYTES + 4096)})
        path.write_text(original + "\n", encoding="utf-8")
        before = file_bytes(path)

        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "new",
                       "--provider", "pi", "--model", "m", "--reasoning-level", "low",
                       "--source", "client/turn/requested")
        self.assertNotEqual(0, r.returncode, "an oversized log must fail closed")
        self.assertEqual(before, file_bytes(path), "no partial rewrite may land")

    def test_malformed_record_is_refused_not_dropped(self) -> None:
        """A corrupt line in our own log is surfaced, never silently dropped."""
        path = self.workspace / "records" / "executed-triples" / "amiga.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = '{"thread_id":"good","status":"resolved"}\n{not valid json\n'
        path.write_text(original, encoding="utf-8")
        before = file_bytes(path)

        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "new",
                       "--provider", "pi", "--model", "m", "--reasoning-level", "low",
                       "--source", "client/turn/requested")
        self.assertNotEqual(0, r.returncode, "a malformed line must fail closed")
        self.assertEqual(before, file_bytes(path), "the corrupt log is not rewritten")


if __name__ == "__main__":
    unittest.main()
