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
SCRIPT = REPO_ROOT / "bin" / "record_executed_triples.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))

PY = sys.executable

RECORD_FILE = "thread-executed-triples.jsonl"
NATIVE_PROJECT = {"amiga": "proj_amiga", "nuvyr_app": "proj_nuvyr"}


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


class RecordExecutedTriplesTest(unittest.TestCase):
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

    def _resolved(self, project: str, thread_id: str, thread_project: str | None = None,
                  *, model: str = "zai/glm-5.2", reasoning: str = "high",
                  source: str = "client/turn/requested", provider: str = "pi") -> subprocess.CompletedProcess:
        native = thread_project if thread_project is not None else NATIVE_PROJECT[project]
        return run_record(self.workspace, "--thread-id", thread_id,
                          "--thread-project", native, "--provider", provider,
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

    # -- GH-710: record ONLY executed-triple evidence (client/turn/requested) -----

    def test_turn_requested_source_records_with_triple(self) -> None:
        """A client/turn/requested result IS executed-triple evidence (bb_client.py:21-24
        names its execution block the authoritative record of the profile bb actually
        ran) and records, with evidence=executed, the source preserved, and the full
        (provider, model, reasoning_level) triple. Mutation proof: reverting the
        accepted source back to client/thread/start fails this test."""
        r = self._resolved("amiga", "thr_exec", source="client/turn/requested",
                           model="zai/glm-5.2", reasoning="high", provider="pi")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("recorded resolved", r.stdout)
        rows = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("resolved", row["status"])
        self.assertEqual("executed", row["evidence"])
        self.assertEqual("client/turn/requested", row["source"])
        self.assertEqual("zai/glm-5.2", row["model"])
        self.assertEqual("high", row["reasoning_level"])
        self.assertEqual("pi", row["provider"])

    def test_thread_start_refused_with_own_distinct_reason(self) -> None:
        """client/thread/start is handled EXPLICITLY: on this SDK its payload carries
        no execution options (GH-706), so it is not executed evidence — refused
        observably with its OWN distinct reason (not the unrecognised-source one)
        and writes no row. If thread/start ever carries execution options, this
        marker is the tripwire that makes the change visible."""
        r = self._resolved("amiga", "thr_start", source="client/thread/start")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("ignored thread_start_not_executed", r.stdout)
        self.assertNotIn("out_of_contract", r.stdout,
                         "thread/start has its own distinct reason, not the unrecognised one")
        self.assertEqual([], rows_for(self.workspace, "amiga"), "thread/start writes no row")
        # An accepted turn-derived row in the same workspace still records (control).
        self._resolved("amiga", "thr_ok", source="client/turn/requested")
        self.assertEqual(1, len(rows_for(self.workspace, "amiga")))

    def test_unrecognised_source_refused_observably_writes_nothing(self) -> None:
        """An unrecognised source cannot be classified into an artifact that claims a
        classification: refused observably (ignored out_of_contract marker naming the
        source) and writes no row. The gate admits ONE named source
        (client/turn/requested); it is not removed. Mutation proof: removing this
        refusal fails this test."""
        for source in ("client/turn/start", "client/something/else", "not-a-source", "client/turn/x"):
            with self.subTest(source=source):
                r = self._resolved("amiga", f"thr_bad_{source}", source=source)
                self.assertEqual(0, r.returncode, r.stderr[:500])
                self.assertIn("ignored out_of_contract", r.stdout)
                self.assertIn(source, r.stdout, "the marker names the refused source")
                self.assertEqual([], rows_for(self.workspace, "amiga"),
                                 f"source {source!r} must write no row")

    def test_resolved_identity_excludes_source(self) -> None:
        """source is not in RESOLVED_IDENTITY_FIELDS: every stored row has
        source client/turn/requested by construction (other sources are refused), so
        source carries no identity. A same-(provider,model,reasoning_level) re-fire
        is a no-op; the identity is exactly those three fields."""
        import record_executed_triples as mod  # type: ignore
        self.assertEqual(("provider", "model", "reasoning_level"), mod.RESOLVED_IDENTITY_FIELDS)
        self.assertNotIn("source", mod.RESOLVED_IDENTITY_FIELDS)
        self._resolved("amiga", "thr_id", model="m", reasoning="low", source="client/turn/requested")
        before = state_file(self.workspace, "amiga").read_text(encoding="utf-8")
        r = self._resolved("amiga", "thr_id", model="m", reasoning="low", source="client/turn/requested")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("noop", r.stdout)
        self.assertEqual(before, state_file(self.workspace, "amiga").read_text(encoding="utf-8"))


    def test_unresolved_row_carries_no_evidence_label(self) -> None:
        """An unresolved row records a FAILED resolution — no value was read, so
        there is no source and no evidence label (a failed resolution executed
        nothing). It must not inherit `executed` (the assume-the-label defect)."""
        run_record(self.workspace, "--thread-id", "thr_U",
                   "--thread-project", "proj_amiga", "--unresolved", "profile_not_resolved")
        row = rows_for(self.workspace, "amiga")[0]
        self.assertNotIn("evidence", row, "an unresolved row has no source to derive a label from")
        self.assertNotIn("executed", json.dumps(row))

    def test_state_file_name_states_what_it_holds(self) -> None:
        """The persisted artifact's name states what the rows ARE — the triples that
        executed. thread-creation-defaults.jsonl must not survive holding
        turn-derived rows (GH-710)."""
        self._resolved("amiga", "thr_L", source="client/turn/requested")
        name = state_file(self.workspace, "amiga").name
        self.assertEqual(RECORD_FILE, name)
        self.assertIn("executed", name)
        self.assertNotIn("creation", name)

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
        run_record(self.workspace, "--thread-id", "thr_C",
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
        r = run_record(self.workspace, "--thread-id", "thr_R",
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
        r = run_record(self.workspace, "--thread-id", "thr_P",
                       "--thread-project", "proj_amiga",
                       "--model", "m", "--reasoning-level", "low", "--source", "client/turn/requested")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIsNone(rows_for(self.workspace, "amiga")[0]["provider"])

    # -- unresolved direction ----------------------------------------------

    def test_unresolvable_profile_records_typed_failure(self) -> None:
        r = run_record(self.workspace, "--thread-id", "thr_U",
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
        r = run_record(self.workspace, "--thread-id", "thr_E",
                       "--thread-project", "proj_amiga",
                       "--unresolved", "profile_resolution_error", "--failure-detail", "loopback timeout")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        row = rows_for(self.workspace, "amiga")[0]
        self.assertEqual("profile_resolution_error", row["failure_reason"])
        self.assertEqual("loopback timeout", row["failure_detail"])

    # -- S1 / T1: native project ownership ---------------------------------

    def test_amiga_thread_records_only_amiga(self) -> None:
        r = self._resolved("amiga", "thr_amiga", thread_project="proj_amiga", model="m1")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertEqual(
            [], rows_for(self.workspace, "nuvyr_app"),
            "an Amiga thread must write into no other project's artifact",
        )
        amiga = rows_for(self.workspace, "amiga")
        self.assertEqual(1, len(amiga), "the native Amiga id must resolve exactly to Amiga")
        self.assertEqual("amiga", amiga[0]["project_id"])

    def test_non_amiga_thread_records_only_non_amiga(self) -> None:
        r = self._resolved("nuvyr_app", "thr_nuvyr", thread_project="proj_nuvyr", model="m2")
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertEqual(
            [], rows_for(self.workspace, "amiga"),
            "a non-Amiga thread must write into no other project's artifact",
        )
        nuvyr = rows_for(self.workspace, "nuvyr_app")
        self.assertEqual(1, len(nuvyr), "the native Nuvyr id must resolve exactly to Nuvyr")
        self.assertEqual("nuvyr_app", nuvyr[0]["project_id"])

    def test_unknown_native_project_is_ignored_and_writes_nothing(self) -> None:
        r = run_record(
            self.workspace, "--thread-id", "thr_unknown", "--thread-project", "proj_outside",
            "--model", "m", "--reasoning-level", "low", "--source", "client/turn/requested",
        )
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("ignored unknown_thread_project", r.stdout)
        self.assertIn("proj_outside", r.stdout)
        self.assertEqual([], any_record_file(self.workspace), "an unknown native id writes nowhere")

    def test_duplicate_native_project_refuses_and_names_collision(self) -> None:
        write_projects(self.workspace, [
            {"id": "amiga", "bb": {"project_id": "proj_shared"}},
            {"id": "nuvyr_app", "bb": {"project_id": "proj_shared"}},
        ])
        r = self._resolved("amiga", "thr_collision", thread_project="proj_shared")
        self.assertNotEqual(0, r.returncode, "duplicate native ownership must fail closed")
        self.assertIn("collision", r.stderr)
        self.assertIn("amiga", r.stderr)
        self.assertIn("nuvyr_app", r.stderr)
        self.assertEqual([], any_record_file(self.workspace), "a collided native id writes nowhere")

    def test_malformed_bb_block_refuses_instead_of_dropping_candidate(self) -> None:
        write_projects(self.workspace, [
            {"id": "amiga", "bb": {"project_id": "proj_amiga"}},
            {"id": "nuvyr_app", "bb": "proj_nuvyr"},
        ])
        r = self._resolved("amiga", "thr_malformed", thread_project="proj_amiga")
        self.assertNotEqual(0, r.returncode, "a malformed bb block must fail closed")
        self.assertIn("bb block is malformed", r.stderr)
        self.assertIn("nuvyr_app", r.stderr)
        self.assertEqual([], any_record_file(self.workspace), "malformed registry ownership writes nowhere")

    def test_project_without_bb_block_remains_uncovered(self) -> None:
        write_projects(self.workspace, [
            {"id": "amiga", "bb": {"project_id": "proj_amiga"}},
            {"id": "docs"},
        ])
        r = run_record(
            self.workspace, "--thread-id", "thr_docs", "--thread-project", "docs",
            "--model", "m", "--reasoning-level", "low", "--source", "client/turn/requested",
        )
        self.assertEqual(0, r.returncode, r.stderr[:500])
        self.assertIn("ignored unknown_thread_project", r.stdout)
        self.assertEqual([], any_record_file(self.workspace), "a project without a bb block stays uncovered")

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
                        "source": "client/turn/requested", "evidence": "executed"})
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
                        "source": "client/turn/requested", "evidence": "executed"}) + "\n",
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
        self.assertEqual(
            "project 'amiga' bb.project_id ' proj_amiga ' has surrounding whitespace; "
            "refusing to record (match raw, reject padded — GH-695 P2-D)",
            r.stderr.strip(),
        )
        self.assertFalse(path.exists(), "no record file is written for a refused scope")

    def test_padded_bb_project_id_rejected_not_normalized_non_amiga(self) -> None:
        """Shared-contract mutation proof on the NON-Amiga path."""
        write_projects(self.workspace, [
            {"id": "nuvyr_app", "bb": {"enabled": True, "project_id": "\tproj_nuvyr"}},
        ])
        r = self._resolved("nuvyr_app", "thr_N", thread_project="\tproj_nuvyr")
        self.assertNotEqual(0, r.returncode, "non-Amiga path must also refuse a padded bb.project_id")
        self.assertEqual(
            "project 'nuvyr_app' bb.project_id '\\tproj_nuvyr' has surrounding whitespace; "
            "refusing to record (match raw, reject padded — GH-695 P2-D)",
            r.stderr.strip(),
        )

    # -- F2: state root -----------------------------------------------------

    def test_state_lives_under_configured_project_state_root(self) -> None:
        """Records live under {project_state_root}/{project_id}/, the root the Project
        Boundary rule owns — not a second invented root, and not under the checkout."""
        self._resolved("amiga", "thr_S")
        path = state_file(self.workspace, "amiga")
        self.assertTrue(path.exists(), path)
        self.assertTrue("project-state" in path.parts, f"under configured state root: {path}")
        self.assertNotIn("records", path.parts, "no second runtime-state root")

    # -- F4: budget boundary ------------------------------------------------

    def test_boundary_crossing_write_refused_without_modifying(self) -> None:
        """A log just under budget plus one row must refuse WITHOUT modifying the file,
        and the file must remain readable afterwards. A wedge is worse than a refusal."""
        import record_executed_triples as mod  # type: ignore
        budget = mod.RECORD_FILE_BUDGET_BYTES
        path = state_file(self.workspace, "amiga")
        path.parent.mkdir(parents=True, exist_ok=True)

        def line_of_size(target: int) -> str:
            # A COMPLETE resolved row (all identity fields + project_id, so P2-C load
            # validation passes) so a same-triple re-fire is a no-op under N1, which is
            # how we prove the file stayed readable.
            row = {"thread_id": "old", "project_id": "amiga", "status": "resolved", "provider": "pi",
                   "model": "M", "reasoning_level": "low", "source": "client/turn/requested",
                   "evidence": "executed", "pad": "x" * 8}
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
        import record_executed_triples as mod  # type: ignore
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
        write_projects(self.workspace, [{"id": "amiga", "bb": "proj_amiga"}])
        r_reg = run_record(self.workspace, "--thread-id", "t",
                           "--thread-project", "proj_amiga", "--model", "m",
                           "--reasoning-level", "low", "--source", "client/turn/requested")
        self.assertNotEqual(0, r_reg.returncode)
        self.assertTrue(r_reg.stderr.strip(), "registry refusal must explain itself on stderr")

        write_projects(self.workspace, [
            {"id": "amiga", "bb": {"enabled": True, "project_id": "proj_amiga"}},
            {"id": "nuvyr_app", "bb": {"enabled": True, "project_id": "proj_nuvyr"}},
        ])

        # write-boundary refusal
        import record_executed_triples as mod  # type: ignore
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
