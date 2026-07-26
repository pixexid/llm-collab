"""Regression guard for GH-1549 / TASK-D9FF3E.

Closes the same-family documentation gaps GH-1539 risk-accepted and GH-1549
fixed in the llm-collab repo:

  Class A — bare runnable axsend/axsend-ensure examples in llm-collab docs
            outside the canonical absolute executable under bin/. The
            prose-noun exemption (`axsend confirm`, `--dry-run`) is recognized
            by the absence of a following shell argument list.
  Class D — the three no-terminal-artifact states must be named and handled:
            absent explicit review request, eyes-only current-head artifact,
            and prior-head-only artifacts after push invalidation. The silent
            ageing GH-1549 originally specified for them was deleted on
            2026-07-26; the three survive as a classification of non-signals,
            and nothing merges by elapsing.

The fixtures under tests/fixtures/gh1549_fallback_semantics/ encode the expected
disposition for each state so a future drift in either the docs or a runtime
implementation that consumes these scenarios is caught. Disposition is a function
of the diff's tier and whether a review is outstanding -- not of the state alone.
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
        self.assertIn("No explicit review request", text)
        self.assertRegex(text, r"gate violation to fix,\s+not a delay to wait out")
        self.assertRegex(text, r"with no clock attached to any\s+of them")
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
        self.assertIn("eyes-only current-head", text)
        self.assertIn("prior-head", text)
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
        # Positive anchor, INVERTED 2026-07-25 by the manual-only ruling on llm-collab#310.
        #
        # The old anchor required the gate to state that an explicit review request is NOT
        # required for a head to be reviewable. That was true only because automatic review
        # existed: a head became reviewable on its own and the fallback clock measured how long
        # to wait for an unrequested review. With auto review off account-wide, nothing arrives
        # unrequested, so the old sentence would assert something false and the fallback it
        # anchored is a path that always ends in silence.
        #
        # The replacement pins the invariant that actually governs now: Tier A must request and
        # never merges on silence, and an unrequested head does not wait at all.
        self.assertRegex(
            text,
            r"there is no silence fallback for an unrequested review",
            "the gate must state that an unrequested review has no fallback; under manual-only "
            "the old 'a request is NOT required' invariant is false",
        )
        self.assertRegex(text, r"never merge on silence")


class Gh1549FallbackFixturesTest(unittest.TestCase):
    """Per-project non-signal fixtures execute the variant assertions.

    Each fixture carries a project_cases array with concrete cases for
    project_id="amiga" and project_id="nuvyr" (the representative non-Amiga
    project used throughout the existing test suite). llm-collab AGENTS.md
    requires focused coverage for Amiga plus at least one non-Amiga project
    for shared contracts. Every case also names a tier, because under the
    manual-only gate the disposition of a non-signal is decided by the tier
    and by whether a review is outstanding -- not by which non-signal it is.
    subTest iterates each case so the contract is executed, not just declared.
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

    # The 15-minute coherence machine that lived here -- the timestamp parser, its four
    # validation tests, the later-of-both-anchors computation, and the meta-guards that
    # checked the guard -- is deleted with the clock it validated (2026-07-26, GH-313
    # finding 1). It computed when a silent head became merge-eligible. Nothing becomes
    # merge-eligible by elapsing now, so the computation has no referent; keeping it
    # green would certify the retired policy. What replaces it is the guard against
    # bringing the clock back.

    TIMING_FIELDS = (
        "clock_anchor",
        "clock_start_utc",
        "fallback_eligible_after_utc",
        "final_push_utc",
        "head_reviewable_utc",
    )

    def test_no_fixture_carries_a_timing_field(self) -> None:
        for variant in self.VARIANT_FILES:
            for case in self._project_cases(variant):
                with self.subTest(project_id=case["project_id"], variant=variant):
                    present = sorted(
                        field
                        for field in self.TIMING_FIELDS
                        if field in case["expected"] or field in case["pr_state"]
                    )
                    self.assertEqual(
                        [], present,
                        f"{variant} reintroduced silence-fallback timing: {present}",
                    )

    def test_every_case_states_that_silence_does_not_ripen_a_head(self) -> None:
        for variant in self.VARIANT_FILES:
            for case in self._project_cases(variant):
                with self.subTest(project_id=case["project_id"], variant=variant):
                    self.assertIs(
                        False, case["expected"]["merge_eligible_on_silence"]
                    )

    def test_no_fixture_mentions_the_deleted_fallback(self) -> None:
        """The mechanism is gone, so its vocabulary must be too.

        Added 2026-07-26 (GH-313 re-review). Deleting the clock while keeping keys like
        `eyes_blocks_fallback_when_no_review_pending` left the fixtures asserting how a
        non-existent mechanism behaves -- assertions no implementation can ever fail, on
        a thing no implementation can ever have. One description even re-stated the
        "later of the final push and the head becoming reviewable" anchor that
        test_the_clock_anchor_is_gone_with_the_clock forbids in the document.
        """
        # The one sanctioned use is the denial itself. It is subtracted from the text
        # rather than exempting the LINE that contains it: JSON puts a whole description
        # on one line, so a line-level exemption let any forbidden word ride along beside
        # the denial -- a mutation restoring the "reviewability clock starts at the later
        # of the final push" anchor passed straight through the first version of this
        # guard.
        sanctioned = re.compile(r"no elapsed time makes the head merge-eligible", re.I)
        forbidden = re.compile(r"fallback|clock|elapsed|expire", re.I)
        offenders: dict[str, list[str]] = {}
        for filename in self.VARIANT_FILES.values():
            raw = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
            hits = sorted({m.group(0).lower() for m in forbidden.finditer(sanctioned.sub("", raw))})
            if hits:
                offenders[filename] = hits
        self.assertFalse(
            offenders,
            "fixture still describes the deleted silence fallback:\n"
            + "\n".join(f"{name}: {v}" for name, v in offenders.items()),
        )

    # The disposition of a non-signal is NOT a property of the variant: it follows from
    # the tier of the diff and whether a review is actually outstanding. Asserting
    # "stuck, escalate" for every case (GH-313 re-review P1) was wrong in both
    # directions -- Tier A with no request is the author's own gate violation to fix,
    # not a wait to escalate, and Tier B/C with no request has nothing pending at all.
    DISPOSITION_BY_TIER_AND_REQUEST = {
        ("A", False): "gate_violation_request_required",
        ("A", True): "escalate_stuck_review",
        # A Tier B review is discretionary to REQUEST; once requested it is pending
        # exactly like a Tier A one, and requested-review precedence keeps it pending
        # until a terminal signal or a disposition. Omitting this pair let the fixtures
        # imply that anything below Tier A has nothing to wait for.
        ("B", True): "escalate_stuck_review",
        ("B", False): "no_review_pending",
        # A Tier C change nobody had to request can still BE requested -- by an operator
        # or another contributor. Requested-review precedence does not consult the tier,
        # so that request is pending like any other and its findings must be adjudicated.
        # Omitting the pair let a consumer treat it as (C, False) and merge with a review
        # outstanding.
        ("C", True): "escalate_stuck_review",
        ("C", False): "no_review_pending",
    }

    # `escalate_stuck_review` is where a requested review ENDS UP, not where it starts.
    # Collapsing every requested review to it made a just-posted request, an initial
    # request whose clock has expired, and an expired re-trigger indistinguishable -- so
    # these fixtures could certify a consumer that escalates immediately, or one that
    # skips the required re-trigger and goes straight to the operator. The phases are the
    # request-anchored ones that survived the silence-fallback deletion; the deleted thing
    # was a clock for a review nobody asked for, and these are clocks anchored to a
    # request that exists.
    REQUEST_PHASES = {
        "initial_pending": "wait_out_initial_request",
        "initial_expired": "issue_the_single_re_trigger",
        "retrigger_pending": "wait_out_the_re_trigger",
        "retrigger_expired": "escalate_stuck_review",
        # Escalating does not produce the disposition; it asks for one. The head stays
        # blocked while that decision is pending, and leaving the flow to end at the
        # escalate ACTION let a consumer either escalate again on every observation or
        # treat the escalation itself as terminal and merge.
        "escalated_awaiting_disposition": "blocked_pending_operator_disposition",
    }

    def test_the_flow_has_a_state_after_escalating(self) -> None:
        """Escalation is a request for a decision, not the decision.

        A consumer whose last modelled state is the escalate action has nothing to do on
        the next observation but escalate again -- or, worse, read escalation as the flow's
        terminal state and proceed.
        """
        self.assertEqual(
            "blocked_pending_operator_disposition",
            self.REQUEST_PHASES["escalated_awaiting_disposition"],
        )
        self.assertNotEqual(
            self.REQUEST_PHASES["retrigger_expired"],
            self.REQUEST_PHASES["escalated_awaiting_disposition"],
            "the action and the state that follows it must be distinguishable",
        )
        # And the blocked state is not a merge-eligible one under any tier.
        self.assertNotIn(
            "blocked_pending_operator_disposition",
            set(self.DISPOSITION_BY_TIER_AND_REQUEST.values()),
            "a tier disposition must never resolve to the pending-decision state",
        )

    def test_a_fixture_request_is_bound_to_a_head(self) -> None:
        """An unbound boolean cannot distinguish a stale request from a pending one.

        After a push, a request for the PRIOR head still satisfies
        `explicit_review_request`, so the fixtures read it as a request for the current
        head: Tier A could bypass the mandatory new-head request and walk to the waiver,
        and Tier B/C could be treated as pending on a request that no longer applies.
        """
        for variant in self.VARIANT_FILES:
            for case in self._project_cases(variant):
                state = case["pr_state"]
                with self.subTest(variant=variant, project_id=case["project_id"]):
                    if not state["explicit_review_request"]:
                        continue
                    self.assertIn(
                        "review_request_head_oid", state,
                        "a request in a fixture must name the head it was issued for",
                    )
                    self.assertEqual(
                        state["head_oid"], state["review_request_head_oid"],
                        "a request naming another head is stale and is not pending here",
                    )

    def test_a_requested_review_has_phases_not_one_disposition(self) -> None:
        """Escalation is the END of the request-anchored flow, not the whole of it."""
        self.assertEqual(
            "escalate_stuck_review",
            self.REQUEST_PHASES["retrigger_expired"],
            "escalation belongs to the expired re-trigger, nothing earlier",
        )
        self.assertNotEqual(
            self.REQUEST_PHASES["initial_pending"],
            self.REQUEST_PHASES["retrigger_expired"],
            "a just-posted request must not be indistinguishable from an exhausted one",
        )
        self.assertEqual(
            "issue_the_single_re_trigger",
            self.REQUEST_PHASES["initial_expired"],
            "the re-trigger is the only recovery, so it cannot be skipped",
        )
        # The tier matrix answers a different question -- is anything pending at all --
        # and it may only ever produce the phase flow's entry point or its end, never a
        # step in the middle.
        for key, disposition in self.DISPOSITION_BY_TIER_AND_REQUEST.items():
            if key[1]:
                with self.subTest(tier=key[0]):
                    self.assertEqual(
                        self.REQUEST_PHASES["retrigger_expired"], disposition,
                        "a requested review's tier disposition is the flow's END; the "
                        "phases in between are REQUEST_PHASES, and a consumer that reads "
                        "this matrix as the whole story escalates immediately",
                    )

    def test_the_phase_flow_is_documented_where_workers_read_it(self) -> None:
        """The phases must be findable, or the model here is a private fiction."""
        doc = (REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "one initial request per candidate final head",
            "single request-anchored re-trigger",
            "operator disposition",
        ):
            self.assertIn(phrase, doc)

    def test_disposition_follows_from_tier_and_request_not_from_the_variant(self) -> None:
        for variant in self.VARIANT_FILES:
            for case in self._project_cases(variant):
                key = (case["tier"], case["pr_state"]["explicit_review_request"])
                with self.subTest(
                    variant=variant, project_id=case["project_id"], tier=case["tier"]
                ):
                    self.assertIn(
                        key,
                        self.DISPOSITION_BY_TIER_AND_REQUEST,
                        f"{variant} declares an unmodelled tier/request pair {key}",
                    )
                    self.assertEqual(
                        self.DISPOSITION_BY_TIER_AND_REQUEST[key],
                        case["expected"]["disposition"],
                    )

    def test_every_variant_covers_both_a_tier_a_and_a_tier_bc_case(self) -> None:
        """A single-tier fixture would re-hide exactly the distinction this restores."""
        for variant in self.VARIANT_FILES:
            tiers = {case["tier"] for case in self._project_cases(variant)}
            with self.subTest(variant=variant):
                self.assertIn("A", tiers)
                self.assertTrue(
                    tiers - {"A"}, f"{variant} models Tier A only: {sorted(tiers)}"
                )

    def test_a_request_is_pending_whatever_tier_prompted_it(self) -> None:
        """Requested-review precedence does not consult the tier.

        The matrix held only (A, *) and (B/C, False), so every below-Tier-A fixture said
        nothing-pending. Tier decides whether a review must be REQUESTED, not what
        happens once one exists.
        """
        for tier in ("A", "B", "C"):
            with self.subTest(tier=tier):
                self.assertEqual(
                    "escalate_stuck_review",
                    self.DISPOSITION_BY_TIER_AND_REQUEST[(tier, True)],
                    "a requested review is pending regardless of tier",
                )
        self.assertEqual(
            "gate_violation_request_required",
            self.DISPOSITION_BY_TIER_AND_REQUEST[("A", False)],
        )
        for tier in ("B", "C"):
            self.assertEqual(
                "no_review_pending", self.DISPOSITION_BY_TIER_AND_REQUEST[(tier, False)]
            )

    def test_a_requested_tier_b_review_is_pending_like_any_other(self) -> None:
        """Tier B is discretionary to REQUEST, not discretionary once requested.

        The matrix previously held only ("A", *) and ("C", False), so every non-A
        fixture said nothing-pending and the suite could not distinguish "no review was
        owed" from "a review was asked for and never came".
        """
        self.assertEqual(
            "escalate_stuck_review", self.DISPOSITION_BY_TIER_AND_REQUEST[("B", True)]
        )
        self.assertEqual(
            "no_review_pending", self.DISPOSITION_BY_TIER_AND_REQUEST[("B", False)]
        )
        requested = [
            case
            for variant in self.VARIANT_FILES
            for case in self._project_cases(variant)
            if case["tier"] == "B" and case["pr_state"]["explicit_review_request"]
        ]
        self.assertTrue(
            requested, "no fixture exercises a voluntarily requested Tier B review"
        )
        for case in requested:
            with self.subTest(project_id=case["project_id"]):
                self.assertTrue(case["expected"]["review_is_pending"])
                self.assertEqual(
                    "escalate_stuck_review", case["expected"]["disposition"]
                )

    def test_no_case_is_terminal_and_only_a_pending_review_can_be_stuck(self) -> None:
        for variant in self.VARIANT_FILES:
            for case in self._project_cases(variant):
                expected = case["expected"]
                with self.subTest(variant=variant, project_id=case["project_id"]):
                    # Every one of these variants is defined as the ABSENCE of a signal.
                    self.assertIs(False, expected["terminal_signal_present"])
                    # A review is outstanding exactly when one was requested.
                    self.assertIs(
                        case["pr_state"]["explicit_review_request"],
                        expected["review_is_pending"],
                    )
                    if expected["disposition"] == "escalate_stuck_review":
                        self.assertTrue(
                            expected["review_is_pending"],
                            "nothing that was never requested can be a stuck review",
                        )

    def test_prior_head_artifacts_are_never_the_current_head(self) -> None:
        """Attribution is head identity alone; recency cannot revive a stale artifact."""
        for case in self._project_cases("prior_head_artifacts_only"):
            with self.subTest(project_id=case["project_id"], tier=case["tier"]):
                for artifact in case["stale_artifacts_for_prior_head"]:
                    self.assertNotEqual(artifact["oid"], case["pr_state"]["head_oid"])

    def test_eyes_only_cases_carry_an_eyes_artifact_on_the_current_head(self) -> None:
        """Otherwise the variant's own premise goes unchecked."""
        for case in self._project_cases("eyes_only_current_head"):
            artifacts = case["artifacts_for_current_head"]
            with self.subTest(project_id=case["project_id"], tier=case["tier"]):
                self.assertTrue(artifacts)
                for artifact in artifacts:
                    self.assertEqual("eyes", artifact["reaction"])
                    self.assertEqual(case["pr_state"]["head_oid"], artifact["oid"])


if __name__ == "__main__":
    unittest.main()
