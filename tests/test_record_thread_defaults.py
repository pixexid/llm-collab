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
SCRIPT = REPO_ROOT / "bin" / "record_thread_defaults.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))

PY = sys.executable

RECORD_FILE = "thread-creation-defaults.jsonl"


def run_record(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(workspace), timeout=60,
    )


def state_file(workspace: Path, project: str) -> Path:
    return workspace / "project-state" / project / RECORD_FILE


def rows_for(workspace: Path, project: str) -> list[dict]:
    path = state_file(workspace, project)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def any_record_file(workspace: Path) -> list[Path]:
    return [p for p in (workspace).rglob(RECORD_FILE)]


def write_projects(workspace: Path, projects: list[dict]) -> None:
    (workspace / "projects.json").write_text(json.dumps({"projects": projects}), encoding="utf-8")


class RecordThreadDefaultsTest(unittest.TestCase):
    """Each test runs in an isolated temp workspace. collab.config.json anchors
    find_workspace_root() at the temp dir and sets project_state_root; projects.json
    registers two projects with distinct bb.project_id scopes (Amiga + a registered
    non-Amiga project, for the shared-contract mutation proofs)."""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="lc-et-", dir="/tmp"))
        (self.workspace / "collab.config.json").write_text(json.dumps({
            "project_state_root": str(self.workspace / "project-state"),
        }), encoding="utf-8")
        write_projects(self.workspace, [
            {"id": "amiga", "bb": {"enabled": True, "project_id": "proj_amiga"}},
            {"id": "nuvyr_app", "bb": {"enabled": True, "project_id": "proj_nuvyr"}},
        ])
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def _resolved(self, project: str, thread_id: str, thread_project: str = "proj_amiga",
                  *, model: str = "zai/glm-5.2", reasoning: str = "high",
                  source: str = "client/thread/start", provider: str = "pi") -> subprocess.CompletedProcess:
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
        self.assertEqual("client/thread/start", row["source"])
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

    # -- GH-695 head 3: record ONLY creation defaults; refuse every other source -----

    def test_creation_default_source_records(self) -> None:
        """A client/thread/start result IS a creation-time default and records, with
        evidence=creation_defaults and the source preserved."""
        self._resolved("amiga", "thr_create", source="client/thread/start")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("resolved", row["status"])
        self.assertEqual("creation_defaults", row["evidence"])
        self.assertEqual("client/thread/start", row["source"])

    def test_turn_sourced_result_refused_observably_writes_nothing(self) -> None:
        """A turn-derived source (client/turn/requested or client/turn/start) is OUT
        OF THIS ARTIFACT'S CONTRACT: refused observably (exit 0 + ignored marker
        naming the source) and writes no row, so the store name stays true. This is
        the test that fails if turn sources are ever re-admitted into an artifact
        documented as creation defaults."""
        for source in ("client/turn/requested", "client/turn/start"):
            with self.subTest(source=source):
                r = self._resolved("amiga", f"thr_{source}", source=source)
                self.assertEqual(0, r.returncode, r.stderr[:500])
                self.assertIn("ignored out_of_contract", r.stdout)
                self.assertIn(source, r.stdout, "the marker names the refused source")
                self.assertIn("GH-695 P1-B", r.stdout, "the marker names the deferred re-scope")
                self.assertEqual([], rows_for(self.workspace, "amiga"),
                                 f"source {source!r} must write no row")
        # An in-contract thread in the same workspace still records normally (control).
        self._resolved("amiga", "thr_ok", source="client/thread/start")
        self.assertEqual(1, len(rows_for(self.workspace, "amiga")))

    def test_unrecognised_source_refused_observably_writes_nothing(self) -> None:
        """An unrecognised source cannot be classified into an artifact that claims a
        classification: refused observably (ignored marker naming the source) and
        writes no row. Do not write a row you cannot classify."""
        for source in ("client/something/else", "not-a-source", "client/turn/x"):
            with self.subTest(source=source):
                r = self._resolved("amiga", f"thr_bad_{source}", source=source)
                self.assertEqual(0, r.returncode, r.stderr[:500])
                self.assertIn("ignored out_of_contract", r.stdout)
                self.assertIn(source, r.stdout)
                self.assertEqual([], rows_for(self.workspace, "amiga"))

    def test_resolved_identity_excludes_source(self) -> None:
        """source is no longer in RESOLVED_IDENTITY_FIELDS: every stored row has
        source client/thread/start by construction (turn sources are refused), so
        source carries no identity. A same-(provider,model,reasoning_level) re-fire
        is a no-op; the identity is exactly those three fields."""
        import record_thread_defaults as mod  # type: ignore
        self.assertEqual(("provider", "model", "reasoning_level"), mod.RESOLVED_IDENTITY_FIELDS)
        self.assertNotIn("source", mod.RESOLVED_IDENTITY_FIELDS)
        self._resolved("amiga", "thr_id", model="m", reasoning="low", source="client/thread/start")
        before = state_file(self.workspace, "amiga").read_text(encoding="utf-8")
        r = self._resolved("amiga", "thr_id", model="m", reasoning="low", source="client/thread/start")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("noop", r.stdout)
        self.assertEqual(before, state_file(self.workspace, "amiga").read_text(encoding="utf-8"))


    def test_unresolved_row_carries_no_evidence_label(self) -> None:
        """An unresolved row records a FAILED resolution — no value was read, so
        there is no source and no evidence label. It must not inherit
        creation_defaults (the assume-the-label defect)."""
        run_record(self.workspace, "--project", "amiga", "--thread-id", "thr_U",
                   "--thread-project", "proj_amiga", "--unresolved", "profile_not_resolved")
        row = rows_for(self.workspace, "amiga")[0]
        self.assertNotIn("evidence", row, "an unresolved row has no source to derive a label from")
        self.assertNotIn("creation_defaults", json.dumps(row))

    def test_state_file_name_does_not_call_defaults_executed(self) -> None:
        """The persisted artifact's name does not call the rows 'executed' — the
        label follows the value, and a creation-time default is not executed evidence."""
        self._resolved("amiga", "thr_L", source="client/thread/start")
        self.assertEqual(RECORD_FILE, state_file(self.workspace, "amiga").name)
        self.assertNotIn("executed", state_file(self.workspace, "amiga").name)

    # -- N1: provenance immutable once resolved ---------------------------

    def test_re_fire_same_resolved_triple_is_noop(self) -> None:
        """A re-fire with the SAME resolved triple is a no-op: no rewrite, no duplicate."""
        self._resolved("amiga", "thr_S", model="m", reasoning="low", source="client/thread/start")
        before = state_file(self.workspace, "amiga").read_text(encoding="utf-8")
        r = self._resolved("amiga", "thr_S", model="m", reasoning="low", source="client/thread/start")
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
                       "--model", "m", "--reasoning-level", "low", "--source", "client/thread/start")
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
                       "--model", "m", "--reasoning-level", "low", "--source", "client/thread/start")
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

    # -- GH-695 P2-C: cross-project rows refused on load -------------------

    def test_cross_project_row_refused_on_load_amiga(self) -> None:
        """A loaded row whose project_id is missing or belongs to another project is
        refused (fail closed), not silently reused. A matching thread_id in a
        cross-project row must not make the recorder report no-op/conflict and
        preserve the wrong project's data."""
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)
        # A row for the SAME thread id but a DIFFERENT project (nuvyr), plus a row
        # missing project_id entirely — both must be refused.
        path.write_text(
            json.dumps({"thread_id": "thr_A", "project_id": "nuvyr_app", "status": "resolved",
                        "provider": "pi", "model": "m", "reasoning_level": "low",
                        "source": "client/thread/start", "evidence": "creation_defaults"})
            + "\n"
            + json.dumps({"thread_id": "thr_B", "status": "resolved"}) + "\n",
            encoding="utf-8",
        )
        before = path.read_bytes()
        r = self._resolved("amiga", "thr_A")
        self.assertNotEqual(0, r.returncode, "a cross-project row must fail closed, not be loaded")
        self.assertIn("cross-project", r.stderr)
        self.assertEqual(before, path.read_bytes(), "the corrupt/cross-project log is not rewritten")

    def test_cross_project_row_refused_on_load_non_amiga(self) -> None:
        """Shared-contract mutation proof on the NON-Amiga path: nuvyr's file with an
        amiga row is refused too."""
        path = state_file(self.workspace, "nuvyr_app")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"thread_id": "thr_N", "project_id": "amiga", "status": "resolved",
                        "provider": "pi", "model": "m", "reasoning_level": "low",
                        "source": "client/thread/start", "evidence": "creation_defaults"}) + "\n",
            encoding="utf-8",
        )
        before = path.read_bytes()
        r = self._resolved("nuvyr_app", "thr_N", thread_project="proj_nuvyr")
        self.assertNotEqual(0, r.returncode, "non-Amiga path must also refuse a cross-project row")
        self.assertIn("cross-project", r.stderr)
        self.assertEqual(before, path.read_bytes())

    # -- GH-695 P2-D: padded bb.project_id rejected, matched raw ------------

    def test_padded_bb_project_id_rejected_not_normalized_amiga(self) -> None:
        """A padded registry bb.project_id is REJECTED, not stripped — the recorder and
        spawn_gate must enforce the same scope. (Mutating the fix back to .strip()
        makes this pass instead of refusing, proving the gate.)"""
        write_projects(self.workspace, [
            {"id": "amiga", "bb": {"enabled": True, "project_id": " proj_amiga "}},
        ])
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)
        r = self._resolved("amiga", "thr_A", thread_project=" proj_amiga ")
        self.assertNotEqual(0, r.returncode, "a padded bb.project_id must be refused, not normalized")
        self.assertIn("surrounding whitespace", r.stderr)
        self.assertFalse(path.exists(), "no record file is written for a refused scope")

    def test_padded_bb_project_id_rejected_not_normalized_non_amiga(self) -> None:
        """Shared-contract mutation proof on the NON-Amiga path."""
        write_projects(self.workspace, [
            {"id": "nuvyr_app", "bb": {"enabled": True, "project_id": "\tproj_nuvyr"}},
        ])
        r = self._resolved("nuvyr_app", "thr_N", thread_project="\tproj_nuvyr")
        self.assertNotEqual(0, r.returncode, "non-Amiga path must also refuse a padded bb.project_id")
        self.assertIn("surrounding whitespace", r.stderr)

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
                               "--model", "m", "--reasoning-level", "low", "--source", "client/thread/start")
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
                               "--model", "m", "--reasoning-level", "low", "--source", "client/thread/start")
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
        import record_thread_defaults as mod  # type: ignore
        budget = mod.RECORD_FILE_BUDGET_BYTES
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)

        def line_of_size(target: int) -> str:
            # A COMPLETE resolved row (all identity fields + project_id, so P2-C load
            # validation passes) so a same-triple re-fire is a no-op under N1, which is
            # how we prove the file stayed readable.
            row = {"thread_id": "old", "project_id": "amiga", "status": "resolved", "provider": "pi",
                   "model": "M", "reasoning_level": "low", "source": "client/thread/start",
                   "evidence": "creation_defaults", "pad": "x" * 8}
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
        r2 = self._resolved("amiga", "old", model="M", reasoning="low", source="client/thread/start")
        self.assertEqual(0, r2.returncode, r2.stderr[:500])
        self.assertIn("noop", r2.stdout)
        self.assertEqual(before, path.read_bytes(), "a no-op does not rewrite")
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows))
        self.assertEqual("old", rows[0]["thread_id"])

    # -- read-side budget / corruption (fail closed) -----------------------

    def test_oversized_log_is_refused_without_partial_rewrite(self) -> None:
        import record_thread_defaults as mod  # type: ignore
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"thread_id": "old", "project_id": "amiga", "status": "resolved",
                               "pad": "x" * (mod.RECORD_FILE_BUDGET_BYTES + 4096)})
        path.write_text(original + "\n", encoding="utf-8")
        before = path.read_bytes()

        r = self._resolved("amiga", "new")
        self.assertNotEqual(0, r.returncode, "an oversized log must fail closed")
        self.assertEqual(before, path.read_bytes(), "no partial rewrite may land")

    def test_malformed_record_is_refused_not_dropped(self) -> None:
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)
        # The good line carries project_id so it passes P2-C and the refusal is the
        # malformed second line, not a missing project_id.
        original = '{"thread_id":"good","project_id":"amiga","status":"resolved"}\n{not valid json\n'
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
                           "--reasoning-level", "low", "--source", "client/thread/start")
        self.assertNotEqual(0, r_reg.returncode)
        self.assertTrue(r_reg.stderr.strip(), "registry refusal must explain itself on stderr")

        # write-boundary refusal
        import record_thread_defaults as mod  # type: ignore
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"thread_id": "old", "project_id": "amiga", "status": "resolved",
                                    "pad": "x" * (mod.RECORD_FILE_BUDGET_BYTES - 128)}) + "\n", encoding="utf-8")
        r_bud = self._resolved("amiga", "new")
        self.assertNotEqual(0, r_bud.returncode)
        self.assertTrue(r_bud.stderr.strip(), "budget refusal must explain itself on stderr")

        # corruption refusal
        path.write_text('{"thread_id":"good","project_id":"amiga","status":"resolved"}\n{bad\n', encoding="utf-8")
        r_cor = self._resolved("amiga", "new")
        self.assertNotEqual(0, r_cor.returncode)
        self.assertTrue(r_cor.stderr.strip(), "corruption refusal must explain itself on stderr")


if __name__ == "__main__":
    unittest.main()
