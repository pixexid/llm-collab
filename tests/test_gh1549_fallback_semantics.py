"""Regression guard for GH-1549 / TASK-D9FF3E.

Closes the same-family documentation gaps GH-1539 risk-accepted and GH-1549
fixed in the llm-collab repo:

  Class A — bare runnable axsend/axsend-ensure examples in llm-collab docs
            outside the canonical absolute executable under bin/. The
            prose-noun exemption (`axsend confirm`, `--dry-run`) is recognized
            by the absence of a following shell argument list.
  Class D — silent-fallback ageing must explicitly name and handle the three
            no-terminal-artifact variants: absent explicit review request,
            eyes-only current-head artifact, and prior-head-only artifacts
            after push invalidation.

The fallback-semantics fixtures under tests/fixtures/gh1549_fallback_semantics/
encode the expected disposition for each variant so a future drift in either
the docs or a runtime implementation that consumes these scenarios is caught.
"""

from __future__ import annotations

import inspect
import json
import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = (
    REPO_ROOT / "tests" / "fixtures" / "gh1549_fallback_semantics"
)
# The workstation-specific absolute path that AGENTS Project Boundary forbids
# in shared docs/tests. The portable forms are the relative `bin/axsend...`
# (run from the checkout root) or the exact absolute command `deliver.py`
# prints. This literal is intentionally the workstation-specific string so the
# portability guard fails if it reappears in net-new shared doc lines.
WORKSTATION_BIN_PREFIX = "/Users/pixexid/Projects/llm-collab/bin/"

# llm-collab guidance docs covered by GH-1549. The axbridge README is the
# canonical command source and is intentionally excluded from the scans.
GUIDANCE_DOCS = [
    "docs/workflows/commit-push-prs.md",
    "docs/workflows/review-and-handoff.md",
    "docs/workflows/claude-code-desktop-computer-use-bridge.md",
    "docs/workflows/session-startup.md",
    "docs/workflows/session-autobridge-runbook.md",
    "docs/adapters/pm2.md",
    "docs/schema-reference.md",
]

_SUBCOMMANDS = ["ring", "check", "state", "tree", "confirm", "dump"]
# Runnable-command shape: bare axsend/axsend-ensure, a subcommand, then a shell
# argument (flag, <placeholder>, or bare value). Anchored to a non-path
# preceding boundary so any path-prefixed invocation (`bin/axsend...`,
# `/.../bin/axsend...`, `$AX axsend...`) does not match.
_RUNNABLE_AX_RE = re.compile(
    r"(?:^|[^/\w])"            # preceding boundary, not a path char
    r"(?!\$AX[_A-Z]?\s)"       # not the $AX shell-variable form
    r"axsend(?:-ensure)?"
    r"\s+"
    r"(" + "|".join(_SUBCOMMANDS) + r")"
    r"\s+"
    r"(?:--[\w-]+|<[^>]+>|\w)"  # a shell argument: flag, placeholder, or value
)


def _bare_runnable_ax_lines(text: str) -> list[str]:
    """Flag a bare `axsend <subcommand> <arg>` that is NOT path-anchored.

    A command preceded by any `/` (e.g. `bin/axsend...` or an absolute path)
    or by `$AX` is path-anchored and accepted. A bare `axsend ring --app ...`
    with a non-path preceding boundary is a runnable command that depends on
    PATH and is flagged.
    """
    hits: list[str] = []
    for line in text.splitlines():
        if _RUNNABLE_AX_RE.search(line):
            hits.append(line.strip())
    return hits


def _workstation_path_lines(text: str) -> list[str]:
    """Flag any workstation-specific /Users/pixexid/.../bin/ path in shared docs.

    AGENTS Project Boundary: do not hardcode one workstation's path in shared
    docs. Net-new guidance must use the portable `bin/axsend...` form or the
    exact absolute command `deliver.py` prints.
    """
    return [
        line.strip()
        for line in text.splitlines()
        if WORKSTATION_BIN_PREFIX in line
    ]


def _ref_resolves(ref: str) -> bool:
    """Return True if a git ref resolves in this checkout."""
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _resolve_base_ref() -> str | None:
    """Resolve a base ref for scoping net-new lines, remote-independently.

    Order: origin/main if it verifies, else local main if it verifies, else
    None (caller should skipTest with a visible no-base-ref reason).
    """
    for ref in ("origin/main", "main"):
        if _ref_resolves(ref):
            return ref
    return None


def _net_added_lines(base_ref: str, rel: str) -> list[str]:
    """Return the net-new added lines of `rel` between base_ref and HEAD."""
    import subprocess

    result = subprocess.run(
        ["git", "diff", f"{base_ref}..HEAD", "--", rel],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        line[1:].strip()
        for line in result.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


class Gh1549ClassARunnableExamplesTest(unittest.TestCase):
    """Class A: no bare runnable axsend/axsend-ensure examples outside bin/."""

    def test_guidance_docs_have_no_bare_runnable_ax_commands(self) -> None:
        failures: dict[str, list[str]] = {}
        for rel in GUIDANCE_DOCS:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            hits = _bare_runnable_ax_lines(text)
            if hits:
                failures[rel] = hits
        self.assertFalse(
            failures,
            "bare runnable axsend/axsend-ensure example(s) found that are not "
            "path-anchored (use `bin/axsend...`, an absolute path, or $AX):\n"
            + "\n".join(
                f"{rel}:\n  " + "\n  ".join(hits)
                for rel, hits in failures.items()
            ),
        )

    def test_net_new_doc_lines_do_not_hardcode_a_workstation_bin_path(self) -> None:
        # GH-1549 portability guard (round-1 P2 #1 + round-2 corrections):
        # AGENTS Project Boundary forbids hardcoding one workstation's
        # /Users/.../bin/ path in shared docs. This guard is DIFF-SCOPED: it
        # inspects only the net-new lines this lane added against a base ref,
        # so the pre-existing AX= assignment block (which predates this lane
        # and is out of scope) is not flagged, AND future assignment-shaped
        # workstation paths cannot sneak past a content-pattern exemption.
        #
        # Base-ref resolution is remote-independent: origin/main first, then
        # local main, then skipTest with a visible reason.
        base_ref = _resolve_base_ref()
        if base_ref is None:
            self.skipTest(
                "no base ref available: neither origin/main nor local main "
                "resolves in this checkout; cannot scope net-new lines for "
                "the workstation-path portability guard"
            )
        failures: dict[str, list[str]] = {}
        for rel in GUIDANCE_DOCS:
            added = _net_added_lines(base_ref, rel)
            hits = [line for line in added if WORKSTATION_BIN_PREFIX in line]
            if hits:
                failures[rel] = hits
        self.assertFalse(
            failures,
            "GH-1549 net-new doc lines hardcode the workstation-specific "
            f"{WORKSTATION_BIN_PREFIX} (use portable `bin/axsend...` or the "
            "exact command deliver.py prints):\n"
            + "\n".join(
                f"{rel}:\n  " + "\n  ".join(hits)
                for rel, hits in failures.items()
            ),
        )

    def test_portability_guard_falls_back_to_local_main_when_origin_absent(self) -> None:
        # Coverage seam: when origin/main does not resolve but local main does,
        # _resolve_base_ref must return "main" (not None) so the guard runs
        # against the local main ref instead of skipping.
        import subprocess

        def fake_verify(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
            if "origin/main" in cmd:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: ambiguous argument")
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_verify):
            base = _resolve_base_ref()
        self.assertEqual(base, "main")

    def test_portability_guard_skips_when_neither_base_ref_resolves(self) -> None:
        # Coverage seam: when neither origin/main nor local main resolves,
        # _resolve_base_ref must return None so the guard skipTests with a
        # visible no-base-ref reason rather than passing vacuously.
        import subprocess

        def fake_verify(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: bad revision")

        with mock.patch("subprocess.run", side_effect=fake_verify):
            base = _resolve_base_ref()
        self.assertIsNone(base)


class Gh1549ClassDFallbackSemanticsTest(unittest.TestCase):
    """Class D: silent-fallback ageing handles the three named variants."""

    def test_commit_push_prs_doc_names_all_three_variants(self) -> None:
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        # The doc must explicitly enumerate the three no-terminal-artifact
        # variants so future drift cannot silently drop one.
        self.assertIn("No explicit review request", text)
        self.assertIn("Eyes-only current-head artifact", text)
        self.assertIn("Prior-head artifacts only", text)

    def test_review_and_handoff_doc_references_the_three_variants(self) -> None:
        text = (
            REPO_ROOT / "docs" / "workflows" / "review-and-handoff.md"
        ).read_text(encoding="utf-8")
        # The compact mirror must name all three variants and point at the
        # full enumeration in commit-push-prs.md.
        self.assertIn("no explicit review request", text)
        self.assertIn("eyes-only current-head", text)
        self.assertIn("prior-head", text)
        self.assertIn("commit-push-prs.md", text)

    def test_later_of_clock_anchor_is_preserved(self) -> None:
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        # The GH-1539 invariant: the fallback clock anchors at the later of
        # the final push and the head becoming reviewable. This must not be
        # weakened to commit age alone.
        self.assertRegex(
            text,
            r"later of the\s+final push",
        )
        self.assertRegex(text, r"head becoming reviewable")

    def test_report_and_escalate_behavior_is_preserved(self) -> None:
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        # A pending/request state that ages out must remain reportable as a
        # stuck review even though it no longer blocks the fallback.
        self.assertRegex(text, r"report (?:and escalate|it|the stuck review)|escalate the stuck review")

    def test_fallback_gate_does_not_require_an_explicit_review_request(self) -> None:
        # GH-1549 round-1 P2 #2: the three-variant block says an open/ready PR
        # is reviewable with NO explicit review request, but the canonical
        # fallback gate previously listed "review request visible" /
        # "review-request visibility exists" as a required condition, which
        # contradicts the absent-request variant. The reconciled wording
        # replaces both with "visible for review" plus an explicit
        # "NOT required" qualifier.
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        # The contradictory phrasings the canonical gate used before the fix.
        # Either form, anywhere in the fallback-gate material, fails the guard.
        contradictory_phrases = [
            r"review[- ]request visibility exists",
            r"review request visible\b",
            r"review-request visibility\b",
        ]
        contradictory = []
        for line in text.splitlines():
            for phrase in contradictory_phrases:
                if re.search(phrase, line, re.I):
                    contradictory.append(line.strip())
        self.assertFalse(
            contradictory,
            "commit-push-prs.md fallback gate still treats review-request "
            "visibility as a required condition (contradicts the "
            "absent-request variant):\n" + "\n".join(contradictory),
        )
        # Positive anchor: the gate must now state an explicit request is NOT
        # required, matching the absent-request variant.
        self.assertRegex(
            text,
            r"explicit\s+review\s+request\s+is\s+NOT\s+required",
        )


class Gh1549FallbackFixturesTest(unittest.TestCase):
    """Per-project fallback-semantics fixtures execute the variant assertions.

    Each fixture carries a project_cases array with concrete paired cases for
    project_id="amiga" and project_id="nuvyr" (the representative non-Amiga
    project used throughout the existing test suite). llm-collab AGENTS.md
    requires focused coverage for Amiga plus at least one non-Amiga project
    for shared contracts. subTest iterates each project case through the
    variant-specific assertions so the shared fallback contract is executed,
    not just declared as metadata.
    """

    VARIANT_FILES = {
        "absent_request": "absent_request.json",
        "eyes_only_current_head": "eyes_only_current_head.json",
        "prior_head_artifacts_only": "prior_head_artifacts_only.json",
    }
    REQUIRED_PROJECTS = ("amiga", "nuvyr")

    def _load(self, variant: str) -> dict:
        path = FIXTURES_DIR / self.VARIANT_FILES[variant]
        self.assertTrue(path.exists(), f"missing fixture: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _project_cases(self, variant: str) -> list[dict]:
        data = self._load(variant)
        cases = data.get("project_cases")
        self.assertIsInstance(
            cases,
            list,
            f"{self.VARIANT_FILES[variant]} must have a project_cases array",
        )
        return cases

    def _assert_fixture_registry_complete(
        self,
        discovered_filenames: set[str],
    ) -> None:
        registered_filenames = list(self.VARIANT_FILES.values())
        self.assertEqual(
            len(registered_filenames),
            len(set(registered_filenames)),
            "VARIANT_FILES must register each fixture basename exactly once",
        )
        self.assertEqual(
            set(registered_filenames),
            discovered_filenames,
            "VARIANT_FILES must exactly cover every top-level JSON fixture",
        )

    def test_all_three_variant_fixtures_exist(self) -> None:
        for variant, filename in self.VARIANT_FILES.items():
            path = FIXTURES_DIR / filename
            self.assertTrue(
                path.exists(),
                f"fixture for variant {variant!r} missing at {path}",
            )

    def test_fixture_registry_covers_complete_json_directory(self) -> None:
        self._assert_fixture_registry_complete(
            {path.name for path in FIXTURES_DIR.glob("*.json")}
        )

    def test_fixture_registry_rejects_adversarial_future_file(self) -> None:
        discovered = {path.name for path in FIXTURES_DIR.glob("*.json")}
        with self.assertRaises(AssertionError):
            self._assert_fixture_registry_complete(
                discovered | {"future_variant.json"}
            )

    def test_each_fixture_has_paired_amiga_and_nuvyr_cases(self) -> None:
        # Every fixture must carry concrete paired cases for both required
        # projects, each with its own pr_state and expected outcome.
        for variant, filename in self.VARIANT_FILES.items():
            cases = self._project_cases(variant)
            declared = [c["project_id"] for c in cases]
            missing = set(self.REQUIRED_PROJECTS) - set(declared)
            self.assertFalse(
                missing,
                f"{filename} project_cases missing required projects: "
                f"{sorted(missing)} (must include both amiga and nuvyr)",
            )

    def test_fallback_fixture_coherence_for_every_case(self) -> None:
        for variant in self.VARIANT_FILES:
            for case in self._project_cases(variant):
                fallback_utc = case["expected"].get(
                    "fallback_eligible_after_utc"
                )
                if fallback_utc is None:
                    continue
                with self.subTest(
                    project_id=case["project_id"],
                    variant=variant,
                ):
                    self._assert_fallback_case_coherent(case)

    def _assert_fallback_case_coherent(self, case: dict) -> None:
        pr = case["pr_state"]
        fallback_utc = case["expected"]["fallback_eligible_after_utc"]
        self.assertFalse(
            pr["explicit_review_request"],
            "fallback eligibility and an explicit current-head review request "
            "are mutually exclusive",
        )
        clock_start = max(
            datetime.fromisoformat(pr["final_push_utc"]),
            datetime.fromisoformat(pr["head_reviewable_utc"]),
        )
        self.assertEqual(
            datetime.fromisoformat(fallback_utc),
            clock_start + timedelta(minutes=15),
            "fallback eligibility must be exactly 15 minutes after "
            "later_of(final_push, head_reviewable)",
        )

    def test_generic_fixture_coherence_guard_is_registered(self) -> None:
        self.assertTrue(
            callable(
                getattr(
                    type(self),
                    "test_fallback_fixture_coherence_for_every_case",
                    None,
                )
            )
        )
        source = inspect.getsource(
            type(self).test_fallback_fixture_coherence_for_every_case
        )
        self.assertIn("self._assert_fallback_case_coherent(case)", source)

    def test_fixture_coherence_guard_rejects_named_mutations(self) -> None:
        case = self._project_cases("eyes_only_current_head")[0]
        explicit_request = json.loads(json.dumps(case))
        explicit_request["pr_state"]["explicit_review_request"] = True
        with self.assertRaises(AssertionError):
            self._assert_fallback_case_coherent(explicit_request)

        shifted_timestamp = json.loads(json.dumps(case))
        shifted_timestamp["expected"]["fallback_eligible_after_utc"] = (
            "2026-07-18T12:16:00Z"
        )
        with self.assertRaises(AssertionError):
            self._assert_fallback_case_coherent(shifted_timestamp)

    def test_absent_request_variant_per_project(self) -> None:
        # Absent explicit review request: the reviewability clock still starts
        # at the later of the final push and the head becoming reviewable;
        # absence neither pre-expires nor indefinitely extends the fallback;
        # a stuck state remains reported/escalated.
        for case in self._project_cases("absent_request"):
            pid = case["project_id"]
            pr = case["pr_state"]
            expected = case["expected"]
            with self.subTest(project_id=pid, variant="absent_request"):
                self.assertEqual(
                    expected["clock_anchor"],
                    "later_of(final_push, head_reviewable)",
                )
                self.assertFalse(expected["fallback_blocked_by_absent_request"])
                self.assertFalse(
                    expected["absent_request_extends_fallback_indefinitely"]
                )
                self.assertFalse(expected["commit_age_pre_expires_fallback"])
                self.assertTrue(expected["stuck_state_remains_reported_and_escalated"])
                # The clock starts at the later of final push and head-reviewable.
                self.assertEqual(
                    expected["clock_start_utc"],
                    max(pr["final_push_utc"], pr["head_reviewable_utc"]),
                )

    def test_eyes_only_variant_per_project(self) -> None:
        # Eyes-only current-head artifact: non-terminal, non-blocking once no
        # review is pending, does not restart or suppress the fallback.
        for case in self._project_cases("eyes_only_current_head"):
            pid = case["project_id"]
            expected = case["expected"]
            with self.subTest(project_id=pid, variant="eyes_only_current_head"):
                self.assertFalse(expected["eyes_is_terminal"])
                self.assertFalse(
                    expected["eyes_blocks_fallback_when_no_review_pending"]
                )
                self.assertFalse(expected["eyes_restarts_or_suppresses_fallback"])
                self.assertTrue(expected["stuck_state_remains_reported_and_escalated"])

    def test_prior_head_variant_per_project(self) -> None:
        # Prior-head artifacts after a push: neither the stale verdict body
        # nor the stale reaction is head-attributable for the current head;
        # the clock anchors to the current head's push/reviewable time, which
        # must postdate every prior-head artifact.
        for case in self._project_cases("prior_head_artifacts_only"):
            pid = case["project_id"]
            pr = case["pr_state"]
            expected = case["expected"]
            stale = case["stale_artifacts_for_prior_head"]
            with self.subTest(project_id=pid, variant="prior_head_artifacts_only"):
                self.assertFalse(
                    expected["prior_head_verdict_is_head_attributable_for_current_head"]
                )
                self.assertFalse(
                    expected[
                        "prior_head_reaction_is_head_attributable_for_current_head"
                    ]
                )
                self.assertEqual(
                    expected["clock_anchor"],
                    "later_of(final_push, head_reviewable)",
                )
                self.assertTrue(expected["stuck_state_remains_reported_and_escalated"])
                # The clock must anchor to the current head, not to any
                # prior-head artifact timestamp.
                for artifact in stale:
                    self.assertGreater(expected["clock_start_utc"], artifact["utc"])


if __name__ == "__main__":
    unittest.main()
