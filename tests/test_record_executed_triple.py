from __future__ import annotations
import sys as _grsys; from pathlib import Path as _grPath
_grsys.path.insert(0, str(_grPath(__file__).resolve().parent)); import _runtime_gate_testkit  # noqa: E402,F401  GH-503: deterministic gate-bypass install (any run form)

import json
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


def state_file(workspace: Path, project: str) -> Path:
    return workspace / "project-state" / project / "executed-triples.jsonl"


def rows_for(workspace: Path, project: str) -> list[dict]:
    path = state_file(workspace, project)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def any_record_file(workspace: Path) -> list[Path]:
    return [p for p in (workspace).rglob("executed-triples.jsonl")]


class RecordExecutedTripleTest(unittest.TestCase):
    """Each test runs in an isolated temp workspace. collab.config.json anchors
    find_workspace_root() at the temp dir and sets project_state_root; projects.json
    registers two projects with distinct bb.project_id scopes."""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="lc-et-", dir="/tmp"))
        (self.workspace / "collab.config.json").write_text(json.dumps({
            "project_state_root": str(self.workspace / "project-state"),
        }), encoding="utf-8")
        (self.workspace / "projects.json").write_text(json.dumps({"projects": [
            {"id": "amiga", "bb": {"enabled": True, "project_id": "proj_amiga"}},
            {"id": "nuvyr_app", "bb": {"enabled": True, "project_id": "proj_nuvyr"}},
        ]}), encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def _resolved(self, project: str, thread_id: str, thread_project: str = "proj_amiga",
                  *, model: str = "zai/glm-5.2", reasoning: str = "high",
                  source: str = "client/turn/requested", provider: str = "pi") -> subprocess.CompletedProcess:
        return run_record(self.workspace, "--project", project, "--thread-id", thread_id,
                          "--thread-project", thread_project, "--provider", provider,
                          "--model", model, "--reasoning-level", reasoning, "--source", source)

    # -- resolved direction -------------------------------------------------

    def test_resolved_triple_recorded_exactly_once_per_thread(self) -> None:
        r1 = self._resolved("amiga", "thr_A")
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
        r2 = self._resolved("amiga", "thr_A")
        self.assertEqual(0, r2.returncode, r2.stderr[:500])
        self.assertEqual(1, len(rows_for(self.workspace, "amiga")), "re-record upserts, no duplicate")

        # A second thread adds a second row.
        self._resolved("amiga", "thr_B")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(2, len(rows), "two distinct threads => two rows")
        self.assertEqual({"thr_A", "thr_B"}, {r["thread_id"] for r in rows})

    # -- N1: provenance immutable once resolved ---------------------------

    def test_re_fire_same_resolved_triple_is_noop(self) -> None:
        """A re-fire with the SAME resolved triple is a no-op: no rewrite, no duplicate."""
        self._resolved("amiga", "thr_S", model="m", reasoning="low", source="client/turn/requested")
        before = state_file(self.workspace, "amiga").read_text(encoding="utf-8")
        r = self._resolved("amiga", "thr_S", model="m", reasoning="low", source="client/turn/requested")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("noop", r.stdout)
        self.assertEqual(before, state_file(self.workspace, "amiga").read_text(encoding="utf-8"),
                         "a same-triple re-fire does not rewrite the file")
        self.assertEqual(1, len(rows_for(self.workspace, "amiga")))

    def test_re_fire_different_resolved_triple_preserves_original_and_surfaces_conflict(self) -> None:
        """A re-fire with a DIFFERENT resolved triple keeps the first and surfaces a
        conflict — a later preset change cannot rewrite historical provenance."""
        self._resolved("amiga", "thr_D", model="m_first", reasoning="low")
        r = self._resolved("amiga", "thr_D", model="m_second", reasoning="high")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("conflict", r.stdout)
        self.assertIn("model", r.stdout, "the conflict marker names what differs")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows))
        self.assertEqual("m_first", rows[0]["model"], "the original resolved value is preserved")

    def test_unresolved_then_resolved_completes(self) -> None:
        """unresolved -> resolved is the one legal write against an existing row."""
        run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_C",
                   "--thread-project", "proj_amiga", "--unresolved", "profile_not_resolved")
        self.assertEqual("unresolved", rows_for(self.workspace, "amiga")[0]["status"])
        r = self._resolved("amiga", "thr_C", model="m", reasoning="low")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("recorded resolved", r.stdout)
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows), "completion replaces, not duplicates")
        self.assertEqual("resolved", rows[0]["status"])

    def test_resolved_then_unresolved_preserves_resolved(self) -> None:
        """A resolved->unresolved re-fire keeps the resolved row (the truth) and surfaces
        a conflict; the failure does not overwrite established provenance."""
        self._resolved("amiga", "thr_R", model="m", reasoning="low")
        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_R",
                       "--thread-project", "proj_amiga", "--unresolved", "profile_not_resolved")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("conflict", r.stdout)
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows))
        self.assertEqual("resolved", rows[0]["status"], "resolved provenance is preserved")

    def test_resolved_values_not_a_mutable_reference(self) -> None:
        """GH-617: the row stores resolved VALUES (no preset name) and is IMMUTABLE once
        resolved — a re-resolution with different values keeps the original and surfaces
        a conflict, so a later preset edit cannot retroactively rewrite history (N1)."""
        self._resolved("amiga", "thr_X", model="pi/gpt-5.4-mini", reasoning="low")
        row = rows_for(self.workspace, "amiga")[0]
        self.assertNotIn("preset", json.dumps(row), "no preset-name reference is persisted")
        self.assertEqual("pi/gpt-5.4-mini", row["model"])

        # A different re-resolution does NOT replace the row: the original is kept
        # and the conflict is surfaced on stdout (the plugin logs it at info).
        r = self._resolved("amiga", "thr_X", model="zai/glm-5.2", reasoning="high")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("conflict", r.stdout)
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows), "a conflicting re-fire does not add a row")
        self.assertEqual("pi/gpt-5.4-mini", rows[0]["model"], "the original resolved value is preserved")
        self.assertNotIn("preset", json.dumps(rows[0]))

    def test_absent_provider_recorded_as_null(self) -> None:
        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_P",
                       "--thread-project", "proj_amiga",
                       "--model", "m", "--reasoning-level", "low", "--source", "client/turn/requested")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIsNone(rows_for(self.workspace, "amiga")[0]["provider"])

    # -- unresolved direction ----------------------------------------------

    def test_unresolvable_profile_records_typed_failure(self) -> None:
        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_U",
                       "--thread-project", "proj_amiga", "--provider", "pi",
                       "--unresolved", "profile_not_resolved")
        self.assertEqual(0, r.returncode, r.stderr[:500])

        self.assertEqual(0, len(rows_for(self.workspace, "nuvyr_app")), "absent row baseline: zero rows")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows), "a failed resolution still produces a row")
        row = rows[0]
        self.assertEqual("unresolved", row["status"])
        self.assertEqual("profile_not_resolved", row["failure_reason"])
        self.assertNotIn("model", row, "an unresolved row carries no model")

        # Distinguishable from a resolved row for a different thread in the same file.
        self._resolved("amiga", "thr_R")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(2, len(rows))
        self.assertEqual({"resolved", "unresolved"}, {row["status"] for row in rows})

    def test_resolution_error_carries_detail(self) -> None:
        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_E",
                       "--thread-project", "proj_amiga",
                       "--unresolved", "profile_resolution_error", "--failure-detail", "loopback timeout")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        row = rows_for(self.workspace, "amiga")[0]
        self.assertEqual("profile_resolution_error", row["failure_reason"])
        self.assertEqual("loopback timeout", row["failure_detail"])

    # -- F1: project scope per thread --------------------------------------

    def test_thread_for_another_project_is_ignored(self) -> None:
        """A thread whose bb project does not match the configured scope is IGNORED
        observably (exit 0 + 'ignored scope_mismatch') and writes no row, rather than
        mis-attributing it to the configured project's file."""
        r = run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_other",
                       "--thread-project", "proj_nuvyr", "--provider", "pi",
                       "--model", "m", "--reasoning-level", "low", "--source", "client/turn/requested")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("ignored scope_mismatch", r.stdout)
        self.assertEqual([], rows_for(self.workspace, "amiga"), "no row written for an out-of-scope thread")

        # An in-scope thread in the same run still records normally.
        self._resolved("amiga", "thr_ok")
        self.assertEqual(1, len(rows_for(self.workspace, "amiga")))

    def test_separate_projects_do_not_collide(self) -> None:
        self._resolved("amiga", "thr_1", thread_project="proj_amiga", model="m1")
        self._resolved("nuvyr_app", "thr_1", thread_project="proj_nuvyr", model="m2")
        amiga = rows_for(self.workspace, "amiga")
        nuvyr = rows_for(self.workspace, "nuvyr_app")
        self.assertEqual(1, len(amiga))
        self.assertEqual(1, len(nuvyr))
        self.assertEqual("m1", amiga[0]["model"])
        self.assertEqual("m2", nuvyr[0]["model"])
        self.assertEqual("amiga", amiga[0]["project_id"])
        self.assertEqual("nuvyr_app", nuvyr[0]["project_id"])

    # -- F2: state root -----------------------------------------------------

    def test_state_lives_under_configured_project_state_root(self) -> None:
        """Records live under {project_state_root}/{project_id}/, the root the Project
        Boundary rule owns — not a second invented root, and not under the checkout."""
        self._resolved("amiga", "thr_S")
        path = state_file(self.workspace, "amiga")
        self.assertTrue(path.exists(), path)
        self.assertTrue("project-state" in path.parts, f"under configured state root: {path}")
        self.assertNotIn("records", path.parts, "no second runtime-state root")

    # -- F3: registry binding ----------------------------------------------

    def test_unregistered_project_refused_and_creates_no_file(self) -> None:
        """An unregistered project_id is refused (nonzero) before the lock or record,
        reproducing neither the builtin tasks self-declared-project defect nor a
        phantom authoritative file."""
        for bad in ("amigaa", "../escape", "nonexistent", "AMIGA"):
            with self.subTest(bad=bad):
                r = run_record(self.workspace, "--project", bad, "--thread-id", "thr",
                               "--thread-project", "proj_amiga",
                               "--model", "m", "--reasoning-level", "low", "--source", "client/turn/requested")
                self.assertNotEqual(0, r.returncode, f"{bad!r} should be refused")
        self.assertEqual([], any_record_file(self.workspace), "no record file created for a refused project")

    # -- N2: exact identifiers, never normalized ---------------------------

    def test_project_whitespace_variant_rejected_not_repaired(self) -> None:
        """A whitespace-padded project id is NOT normalized to the registered id — an
        exactness requirement cannot survive normalize-then-compare. Repairing operator
        configuration silently is how a typo becomes authoritative state."""
        for padded in (" amiga", "amiga ", " amiga ", "\tamiga"):
            with self.subTest(padded=padded):
                r = run_record(self.workspace, "--project", padded, "--thread-id", "thr",
                               "--thread-project", "proj_amiga",
                               "--model", "m", "--reasoning-level", "low", "--source", "client/turn/requested")
                self.assertNotEqual(0, r.returncode, f"padded {padded!r} must be refused, not repaired")
        # The exact, un-padded id still records normally (control direction).
        r_ok = self._resolved("amiga", "thr_ok")
        self.assertEqual(0, r_ok.returncode, r_ok.stderr[:500])
        self.assertEqual(1, len(rows_for(self.workspace, "amiga")))
        self.assertEqual([], any_record_file(self.workspace)[1:] if any_record_file(self.workspace) else [],
                         "only the exact project's file exists")

    # -- F4: budget boundary ------------------------------------------------

    def test_boundary_crossing_write_refused_without_modifying(self) -> None:
        """A log just under budget plus one row must refuse WITHOUT modifying the file,
        and the file must remain readable afterwards. A wedge is worse than a refusal."""
        import record_executed_triple as mod  # type: ignore
        budget = mod.RECORD_FILE_BUDGET_BYTES
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)

        def line_of_size(target: int) -> str:
            # A COMPLETE resolved row (all identity fields) so a same-triple re-fire is
            # a no-op under N1, which is how we prove the file stayed readable.
            row = {"thread_id": "old", "status": "resolved", "provider": "pi",
                   "model": "M", "reasoning_level": "low", "source": "client/turn/requested",
                   "pad": "x" * 8}
            line = json.dumps(row, sort_keys=True, separators=(",", ":"))
            frame = len(line) - 8  # length minus the 8 pad chars
            pad_len = max(0, (target - 1) - frame)
            row["pad"] = "x" * pad_len
            return json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"

        just_under = line_of_size(budget - 64)
        self.assertLess(len(just_under), budget)
        path.write_text(just_under, encoding="utf-8")
        before = path.read_bytes()
        self.assertLess(len(before), budget)

        # Recording a NEW thread would push the output over budget: refuse.
        r = self._resolved("amiga", "thr_new")
        self.assertNotEqual(0, r.returncode, "a boundary-crossing write must fail closed")
        self.assertEqual(before, path.read_bytes(), "the file is not modified on refusal")

        # The file remains readable: a same-triple re-fire of the existing row is a
        # no-op (it had to READ the file to decide the triple matches) and leaves the
        # file untouched — proving the project is not wedged after the refusal.
        r2 = self._resolved("amiga", "old", model="M", reasoning="low", source="client/turn/requested")
        self.assertEqual(0, r2.returncode, r2.stderr[:500])
        self.assertIn("noop", r2.stdout)
        self.assertEqual(before, path.read_bytes(), "a no-op does not rewrite")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows))
        self.assertEqual("old", rows[0]["thread_id"])

    # -- read-side budget / corruption (fail closed) -----------------------

    def test_oversized_log_is_refused_without_partial_rewrite(self) -> None:
        import record_executed_triple as mod  # type: ignore
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"thread_id": "old", "status": "resolved",
                               "pad": "x" * (mod.RECORD_FILE_BUDGET_BYTES + 4096)})
        path.write_text(original + "\n", encoding="utf-8")
        before = path.read_bytes()

        r = self._resolved("amiga", "new")
        self.assertNotEqual(0, r.returncode, "an oversized log must fail closed")
        self.assertEqual(before, path.read_bytes(), "no partial rewrite may land")

    def test_malformed_record_is_refused_not_dropped(self) -> None:
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = '{"thread_id":"good","status":"resolved"}\n{not valid json\n'
        path.write_text(original, encoding="utf-8")
        before = path.read_bytes()

        r = self._resolved("amiga", "new")
        self.assertNotEqual(0, r.returncode, "a malformed line must fail closed")
        self.assertEqual(before, path.read_bytes(), "the corrupt log is not rewritten")

    # -- F5: observable refusals (nonzero + stderr) ------------------------

    def test_refusals_are_observable_nonzero_with_stderr(self) -> None:
        """Every refusal exits nonzero with a non-empty stderr so the plugin's async
        close-handler can log it — a silent refusal is indistinguishable from an event
        that never happened."""
        # registry refusal
        r_reg = run_record(self.workspace, "--project", "amigaa", "--thread-id", "t",
                           "--thread-project", "proj_amiga", "--model", "m",
                           "--reasoning-level", "low", "--source", "client/turn/requested")
        self.assertNotEqual(0, r_reg.returncode)
        self.assertTrue(r_reg.stderr.strip(), "registry refusal must explain itself on stderr")

        # write-boundary refusal
        import record_executed_triple as mod  # type: ignore
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"thread_id": "old", "status": "resolved",
                                    "pad": "x" * (mod.RECORD_FILE_BUDGET_BYTES - 128)}) + "\n", encoding="utf-8")
        r_bud = self._resolved("amiga", "new")
        self.assertNotEqual(0, r_bud.returncode)
        self.assertTrue(r_bud.stderr.strip(), "budget refusal must explain itself on stderr")

        # corruption refusal
        path.write_text('{"thread_id":"good","status":"resolved"}\n{bad\n', encoding="utf-8")
        r_cor = self._resolved("amiga", "new")
        self.assertNotEqual(0, r_cor.returncode)
        self.assertTrue(r_cor.stderr.strip(), "corruption refusal must explain itself on stderr")


if __name__ == "__main__":
    unittest.main()
