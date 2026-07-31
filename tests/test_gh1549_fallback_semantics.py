"""Regression guard for GH-1549 / TASK-D9FF3E.

Closes the same-family documentation gaps GH-1539 risk-accepted and GH-1549
fixed in the llm-collab repo:

  Class A — bare runnable axsend/axsend-ensure examples in llm-collab docs
            outside the canonical absolute executable under bin/. The
            prose-noun exemption (`axsend confirm`, `--dry-run`) is recognized
            by the absence of a following shell argument list.
  Class D — a missing automatic pass, an eyes-only artifact, and prior-head
            artifacts are non-signals. Every PR remains blocked at every tier;
            nothing merges by elapsing.

The fixtures under tests/fixtures/gh1549_fallback_semantics/ pin that shared
outcome for Amiga and a non-Amiga project.
"""

from __future__ import annotations

import json
import re
import unittest
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
    """Class D: the three named states, and the timer that no longer exists.

    Silent-fallback AGEING is gone as of 2026-07-26. Deleting the clock for only the
    unrequested-review variant left eyes-only and prior-head still ripening a head on
    silence -- the same defect under a narrower name -- so all three clocks are deleted.
    The three variants survive purely as a classification of non-signals: they say what
    an artifact is NOT, and none of them can make a head merge-eligible.
    """

    def test_commit_push_prs_doc_names_the_surviving_variants(self) -> None:
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        # Both survivors still enumerated, so drift cannot silently drop one.
        self.assertIn("Eyes-only current-head artifact", text)
        self.assertIn("Prior-head artifacts only", text)

    def test_no_variant_carries_a_timer(self) -> None:
        """What was deleted is the CLOCK, not the classification.

        Revised 2026-07-26 (GH-313 finding 1). The earlier version asserted the
        unrequested-review variant had disappeared from the document entirely, which
        both misstated the ruling and left the other two variants' clocks unexamined --
        so the suite certified a document that still aged a head into a merge.
        """
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        self.assertIn("No terminal first pass", text)
        self.assertIn("Eyes-only current-head artifact", text)
        self.assertIn("Prior-head artifacts only", text)
        for revived in (
            "15-minute",
            "15 minutes",
            "resettable fallback",
            "fallback clock",
            "fallback timeout",
        ):
            self.assertNotIn(
                revived, text, f"the deleted silence timer came back as {revived!r}"
            )

    def test_review_and_handoff_doc_references_the_surviving_variants(self) -> None:
        text = (
            REPO_ROOT / "docs" / "workflows" / "review-and-handoff.md"
        ).read_text(encoding="utf-8")
        self.assertIn("eyes-only artifact", text)
        self.assertIn("prior-head artifact", text)
        self.assertIn("commit-push-prs.md", text)
        self.assertNotIn("no explicit review request (the reviewability clock", text)

    def test_the_clock_anchor_is_gone_with_the_clock(self) -> None:
        """GH-1539's "later of final push and head-reviewable" anchor is retired.

        It described where the silence fallback started counting. With nothing counting,
        an anchor is not a weaker invariant -- it is a claim that a clock exists.
        """
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(text, r"later of the\s+final push")
        self.assertRegex(text, r"no elapsed time is ever a terminal\s+signal")

    def test_report_and_escalate_behavior_is_preserved(self) -> None:
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        # A pending/request state that ages out must remain reportable as a
        # stuck review even though it no longer blocks the fallback.
        self.assertRegex(text, r"reports? (?:a stalled trigger|the review-infrastructure blocker)")

    def test_every_pr_waits_for_the_automatic_first_pass(self) -> None:
        text = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text(encoding="utf-8")
        self.assertIn("GitHub Codex review is configured to start when a PR opens", text)
        self.assertIn("Every PR waits for that first pass before merge", text)
        self.assertRegex(text, r"silence and elapsed time are\s+never a substitute")


class Gh1549FallbackFixturesTest(unittest.TestCase):
    """The three non-signals block every PR, regardless of tier."""

    VARIANT_FILES = {
        "absent_request": "absent_request.json",
        "eyes_only_current_head": "eyes_only_current_head.json",
        "prior_head_artifacts_only": "prior_head_artifacts_only.json",
    }
    REQUIRED_PROJECTS = {"amiga", "nuvyr"}

    def _load(self, variant: str) -> dict:
        return json.loads(
            (FIXTURES_DIR / self.VARIANT_FILES[variant]).read_text(encoding="utf-8")
        )

    def _project_cases(self, variant: str) -> list[dict]:
        cases = self._load(variant).get("project_cases")
        self.assertIsInstance(cases, list)
        return cases

    def test_fixture_registry_is_exact(self) -> None:
        self.assertEqual(
            set(self.VARIANT_FILES.values()),
            {path.name for path in FIXTURES_DIR.glob("*.json")},
        )

    def test_each_variant_covers_amiga_and_a_non_amiga_project(self) -> None:
        for variant in self.VARIANT_FILES:
            with self.subTest(variant=variant):
                self.assertEqual(
                    self.REQUIRED_PROJECTS,
                    {case["project_id"] for case in self._project_cases(variant)},
                )

    def test_non_signals_block_every_tier_without_ripening(self) -> None:
        for variant in self.VARIANT_FILES:
            for case in self._project_cases(variant):
                expected = case["expected"]
                with self.subTest(variant=variant, project=case["project_id"]):
                    self.assertIs(False, expected["terminal_signal_present"])
                    self.assertIs(False, expected["merge_eligible_on_silence"])
                    self.assertIn(
                        expected["disposition"],
                        {
                            "manual_fallback_required",
                            "review_infrastructure_blocker",
                            "first_pass_pending",
                        },
                    )

    def test_absent_request_requires_tier_a_manual_fallback_only(self) -> None:
        cases = {case["tier"]: case for case in self._project_cases("absent_request")}
        self.assertEqual(cases["A"]["expected"]["disposition"], "manual_fallback_required")
        self.assertEqual(cases["C"]["expected"]["disposition"], "review_infrastructure_blocker")

    def test_retired_manual_request_machine_is_absent(self) -> None:
        retired = (
            "no_review_pending",
            "retrigger",
            "request_phase",
            "explicit_review_request",
        )
        for filename in self.VARIANT_FILES.values():
            text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
            for term in retired:
                with self.subTest(filename=filename, term=term):
                    self.assertNotIn(term, text)

    def test_prior_head_artifacts_are_not_current(self) -> None:
        for case in self._project_cases("prior_head_artifacts_only"):
            for artifact in case["stale_artifacts_for_prior_head"]:
                self.assertNotEqual(artifact["oid"], case["head_oid"])

    def test_eyes_is_pickup_not_a_verdict(self) -> None:
        for case in self._project_cases("eyes_only_current_head"):
            for artifact in case["artifacts_for_current_head"]:
                self.assertEqual("eyes", artifact["reaction"])


if __name__ == "__main__":
    unittest.main()
