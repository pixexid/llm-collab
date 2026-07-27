"""Pi runtime fingerprint pinning and native-session exclusivity (GH-319).

Every case here is a way a Pi worker wakes with a configuration nobody registered:
a model changed on the thread since registration, a presentation catalogue that
substituted one silently, a live configuration that cannot be read at all, and two
logical workers sharing one native thread so either can change the other's model.

The refusal direction is deliberate and asserted: refusing to wake costs a delay,
because the mailbox is durable-first and the packet stays pull-pending. Waking the
wrong model costs a turn charged to the wrong plan on a worker never configured for
it, and that is not recoverable by retrying.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

from _pi_fingerprint import (  # noqa: E402
    FINGERPRINT_FIELDS,
    MAX_SESSIONS_SCANNED,
    REFUSE_DUPLICATE_NATIVE_SESSION,
    REFUSE_SESSION_REGISTRY_UNPROVABLE,
    REFUSE_FINGERPRINT_INCOMPLETE,
    REFUSE_FINGERPRINT_MISMATCH,
    REFUSE_FINGERPRINT_UNPROVEN,
    REFUSE_REASONING_LEVEL_UNSUPPORTED,
    PiFingerprintRefused,
    assert_fingerprint_matches,
    assert_native_session_is_exclusive,
    assert_reasoning_level_supported,
    observed_fingerprint,
    pinned_fingerprint,
)


# Glim's real target, from the machine: Pi's own default is zai/glm-5.2/max.
GLIM = {"provider": "zai", "model": "glm-5.2", "reasoning_level": "max"}


def runtime(fingerprint=GLIM, *, native="pi-thread-glim"):
    entry = {"family": "pi", "session_id": native}
    if fingerprint is not None:
        entry["fingerprint"] = dict(fingerprint)
    return entry


def session(session_id, native, *, status="active", family="pi"):
    return {
        "session_id": session_id,
        "status": status,
        "runtime": {"family": family, "session_id": native},
    }


class PiFingerprintTest(unittest.TestCase):
    def test_a_matching_live_session_is_proved_and_returns_what_it_observed(self):
        observed = assert_fingerprint_matches(runtime(), dict(GLIM))
        self.assertEqual(GLIM, observed)

    def test_a_changed_model_refuses(self):
        """The live thread moved off the pinned model since registration."""
        live = {**GLIM, "model": "glm-5.1"}
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_fingerprint_matches(runtime(), live)
        self.assertEqual(REFUSE_FINGERPRINT_MISMATCH, caught.exception.reason)
        self.assertIn("glm-5.1", caught.exception.detail)
        self.assertIn("glm-5.2", caught.exception.detail)

    def test_each_pinned_field_is_load_bearing(self):
        """A pin that only notices the model is not a fingerprint.

        Provider decides which plan is charged and reasoning level decides what a turn
        costs; either changing silently is the failure this exists to catch.
        """
        for field, wrong in (
            ("provider", "openai-codex"),
            ("model", "glm-4.7"),
            ("reasoning_level", "low"),
        ):
            with self.subTest(field=field):
                live = {**GLIM, field: wrong}
                with self.assertRaises(PiFingerprintRefused) as caught:
                    assert_fingerprint_matches(runtime(), live)
                self.assertEqual(REFUSE_FINGERPRINT_MISMATCH, caught.exception.reason)
                self.assertIn(field, caught.exception.detail)

    def test_an_unreadable_live_configuration_refuses_rather_than_passes(self):
        """The case a stale presentation catalogue produces.

        `pi-gui`'s installed selector does not list `k3` even though Pi supports it, so
        "cannot read the live model" is a real state and it is exactly when a silent
        substitution would happen. Treating it as a pass would make the pin decorative.
        """
        for live in ({}, {"provider": "zai"}, {"provider": "zai", "model": "glm-5.2"}, None):
            with self.subTest(live=live):
                with self.assertRaises(PiFingerprintRefused) as caught:
                    assert_fingerprint_matches(runtime(), live)
                self.assertEqual(REFUSE_FINGERPRINT_UNPROVEN, caught.exception.reason)

    def test_a_partial_pin_is_refused_as_incomplete_not_accepted_as_a_pin(self):
        """A binding that pins two of three cannot detect a change in the third."""
        for partial in (
            {"provider": "zai", "model": "glm-5.2"},
            {"provider": "zai", "reasoning_level": "max"},
            {"model": "glm-5.2", "reasoning_level": "max"},
            {},
        ):
            with self.subTest(partial=sorted(partial)):
                with self.assertRaises(PiFingerprintRefused) as caught:
                    assert_fingerprint_matches(runtime(partial), dict(GLIM))
                self.assertEqual(REFUSE_FINGERPRINT_INCOMPLETE, caught.exception.reason)
                self.assertEqual({}, pinned_fingerprint(runtime(partial)))

    def test_a_binding_with_no_fingerprint_is_refused(self):
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_fingerprint_matches(runtime(None), dict(GLIM))
        self.assertEqual(REFUSE_FINGERPRINT_INCOMPLETE, caught.exception.reason)

    def test_pi_field_spellings_are_read_not_re_derived(self):
        """Pi names the reasoning level three ways; only one will be present.

        Encoding one spelling and calling the others unreadable would refuse a healthy
        session, which is the mirror error of accepting an unreadable one.
        """
        for name in ("reasoning_level", "reasoningLevel", "thinking_level", "thinkingLevel"):
            with self.subTest(name=name):
                live = {"provider": "zai", "model": "glm-5.2", name: "max"}
                self.assertEqual(GLIM, observed_fingerprint(live))
                self.assertEqual(GLIM, assert_fingerprint_matches(runtime(), live))
        for name in ("model", "model_id", "modelId"):
            with self.subTest(name=name):
                live = {"provider": "zai", name: "glm-5.2", "reasoning_level": "max"}
                self.assertEqual(GLIM, assert_fingerprint_matches(runtime(), live))

    def test_the_three_real_worker_fingerprints_are_expressible(self):
        """Glim, Kimi and Relay, with the models this machine actually offers.

        Verified against `~/.pi/agent/models-store.json` rather than the issue text, since
        the issue deliberately declines to freeze Relay's model.
        """
        workers = {
            "glim": {"provider": "zai", "model": "glm-5.2", "reasoning_level": "max"},
            "kimi": {"provider": "kimi-coding", "model": "k3", "reasoning_level": "max"},
            # Operator's choice, 2026-07-26: Sol at medium.
            "relay": {
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "reasoning_level": "medium",
            },
        }
        for name, fingerprint in workers.items():
            with self.subTest(worker=name):
                self.assertEqual(
                    fingerprint,
                    assert_fingerprint_matches(runtime(fingerprint), dict(fingerprint)),
                )
                # And each is distinguishable from the others: a binding for one worker
                # must refuse the live configuration of another.
                for other, live in workers.items():
                    if other == name:
                        continue
                    with self.assertRaises(PiFingerprintRefused):
                        assert_fingerprint_matches(runtime(fingerprint), dict(live))


class PiNativeSessionExclusivityTest(unittest.TestCase):
    def test_an_unclaimed_native_session_is_exclusive(self):
        assert_native_session_is_exclusive(
            "pi-thread-glim", [session("SESSION-KIMI", "pi-thread-kimi")]
        )

    def test_two_bindings_on_one_native_thread_refuse(self):
        """The issue's named unsafe case, and the reason it is unsafe.

        Pi keeps model selection on the thread, so two logical workers sharing one native
        thread means either can silently change the other's model.
        """
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_native_session_is_exclusive(
                "pi-thread-glim",
                [session("SESSION-OTHER", "pi-thread-glim")],
            )
        self.assertEqual(REFUSE_DUPLICATE_NATIVE_SESSION, caught.exception.reason)
        self.assertIn("SESSION-OTHER", caught.exception.detail)

    def test_a_binding_does_not_collide_with_itself(self):
        """Re-registration must not read as a duplicate claim."""
        assert_native_session_is_exclusive(
            "pi-thread-glim",
            [session("SESSION-GLIM", "pi-thread-glim")],
            owner_session_id="SESSION-GLIM",
        )

    def test_a_retired_claimant_does_not_block(self):
        """A stopped or superseded session cannot change anyone's model."""
        for status in ("stopped", "superseded", "expired"):
            with self.subTest(status=status):
                assert_native_session_is_exclusive(
                    "pi-thread-glim",
                    [session("SESSION-OLD", "pi-thread-glim", status=status)],
                )

    def test_a_same_id_session_of_another_family_does_not_block(self):
        """Native ids are namespaced per family; a codex thread is not a Pi thread."""
        assert_native_session_is_exclusive(
            "pi-thread-glim",
            [session("SESSION-CDX", "pi-thread-glim", family="codex_app")],
        )

    def test_a_missing_native_id_is_refused(self):
        for native in (None, "", "   ", 7):
            with self.subTest(native=native):
                with self.assertRaises(PiFingerprintRefused) as caught:
                    assert_native_session_is_exclusive(native, [])
                self.assertEqual(REFUSE_FINGERPRINT_INCOMPLETE, caught.exception.reason)

    def test_a_level_the_model_maps_to_null_is_refused_at_registration(self):
        """`k3` maps `medium` to null: the model does not support it.

        Pinning it would compare a level the model never runs against whatever it actually
        ran, and refuse at every wake instead of once at registration.
        """
        k3_map = {"off": None, "minimal": None, "low": "low", "medium": None,
                  "high": "high", "xhigh": None, "max": "max"}
        pinned = {"provider": "kimi-coding", "model": "k3", "reasoning_level": "medium"}
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_reasoning_level_supported(runtime(pinned), k3_map)
        self.assertEqual(REFUSE_REASONING_LEVEL_UNSUPPORTED, caught.exception.reason)
        # The two unsupported cases share a reason code, so the MESSAGE is what tells an
        # operator which remedy applies: an unsupported level needs a different level, a
        # remapped one needs the effective level. Asserting only the code cannot tell them
        # apart, and a mutation collapsing one into the other would pass.
        self.assertIn("does not support", caught.exception.detail)
        self.assertNotIn("remaps", caught.exception.detail)
        # And the level it DOES support is accepted.
        ok = {"provider": "kimi-coding", "model": "k3", "reasoning_level": "max"}
        assert_reasoning_level_supported(runtime(ok), k3_map)

    def test_a_remapped_level_is_refused_because_it_could_never_match(self):
        """`gpt-5.6-sol` remaps `minimal` to `low`.

        A live session honestly reports `low`, so a pin of `minimal` never matches -- the
        mirror of the null case, and caught in the same place.
        """
        sol_map = {"xhigh": "xhigh", "max": "max", "minimal": "low"}
        pinned = {"provider": "openai-codex", "model": "gpt-5.6-sol",
                  "reasoning_level": "minimal"}
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_reasoning_level_supported(runtime(pinned), sol_map)
        self.assertEqual(REFUSE_REASONING_LEVEL_UNSUPPORTED, caught.exception.reason)
        self.assertIn("low", caught.exception.detail)

    def test_relays_configured_level_passes_through_unchanged(self):
        """Sol at medium, the operator's choice: absent from the map, so no override.

        A key mapped to null means unsupported and a key mapped elsewhere means remapped,
        so ABSENT is the case that passes through -- which is why `medium` is valid here
        and not on `k3`. Read from the model record rather than assumed either way.
        """
        sol_map = {"xhigh": "xhigh", "max": "max", "minimal": "low"}
        relay = {"provider": "openai-codex", "model": "gpt-5.6-sol",
                 "reasoning_level": "medium"}
        assert_reasoning_level_supported(runtime(relay), sol_map)
        self.assertEqual(relay, assert_fingerprint_matches(runtime(relay), dict(relay)))

    def test_glims_max_survives_its_models_remapping(self):
        """`glm-5.2` maps max to max, so Glim's pin is stable; low and medium are not."""
        glm_map = {"minimal": None, "low": "high", "medium": "high", "high": "high",
                   "max": "max"}
        assert_reasoning_level_supported(runtime(GLIM), glm_map)
        for unstable in ("low", "medium"):
            with self.subTest(level=unstable):
                with self.assertRaises(PiFingerprintRefused):
                    assert_reasoning_level_supported(
                        runtime({**GLIM, "reasoning_level": unstable}), glm_map
                    )

    def test_every_refusal_reason_is_distinct(self):
        """Five refusals, five codes: an operator reading a drift record needs to know
        which one happened, because the remedies differ -- re-register, fix the thread
        binding, pin the effective level, or investigate why the live config is unreadable."""
        codes = {
            REFUSE_FINGERPRINT_MISMATCH,
            REFUSE_FINGERPRINT_UNPROVEN,
    REFUSE_REASONING_LEVEL_UNSUPPORTED,
            REFUSE_DUPLICATE_NATIVE_SESSION,
            REFUSE_FINGERPRINT_INCOMPLETE,
        }
        self.assertEqual(5, len(codes))
        self.assertEqual(3, len(FINGERPRINT_FIELDS))


def pi_session(session_id, native="THREAD-1", *, status="active", expires=None,
               project_id=None):
    record = {
        "session_id": session_id,
        "status": status,
        "runtime": {"family": "pi", "session_id": native},
    }
    if expires is not None:
        record["lease_expires_utc"] = expires
    if project_id is not None:
        record["project_id"] = project_id
    return record


class PiExclusivityEvidenceTest(unittest.TestCase):
    """Findings from the GH-323 review: silence is not proof of an unclaimed thread."""

    def test_an_unreadable_registry_refuses_rather_than_reading_as_empty(self):
        """Approving exactly when a claimant cannot be inspected is the worst case.

        `None`, a mapping, or a malformed record all used to mean "found no claimant",
        so exclusivity was granted precisely when the evidence was missing.
        """
        for registry in (None, {"a": 1}, "sessions", 7):
            with self.subTest(registry=registry):
                with self.assertRaises(PiFingerprintRefused) as caught:
                    assert_native_session_is_exclusive("THREAD-1", registry)
                self.assertEqual(
                    REFUSE_SESSION_REGISTRY_UNPROVABLE, caught.exception.reason
                )

    def test_a_malformed_record_refuses_rather_than_being_skipped(self):
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_native_session_is_exclusive("THREAD-1", [pi_session("S1", "OTHER"), 42])
        self.assertEqual(REFUSE_SESSION_REGISTRY_UNPROVABLE, caught.exception.reason)

    def test_enumeration_is_bounded(self):
        """An untrusted registry must not decide how long registration takes."""
        registry = [pi_session(f"S{i}", "OTHER") for i in range(MAX_SESSIONS_SCANNED + 1)]
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_native_session_is_exclusive("THREAD-1", registry)
        self.assertEqual(REFUSE_SESSION_REGISTRY_UNPROVABLE, caught.exception.reason)

    def test_an_expired_lease_stops_blocking_a_replacement(self):
        """Expiry lives in `lease_expires_utc`; the status stays active.

        There is no `expired` status, so reading status alone kept a lapsed session
        claiming its native thread forever and no replacement could ever be registered --
        the same lease-expiry trap that stranded two packets in llm-collab#324.
        """
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        lapsed = pi_session("S1", "THREAD-1", expires="2026-07-27T11:00:00Z")
        assert_native_session_is_exclusive("THREAD-1", [lapsed], now_utc=now)

        live = pi_session("S2", "THREAD-1", expires="2026-07-27T13:00:00Z")
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_native_session_is_exclusive("THREAD-1", [live], now_utc=now)
        self.assertEqual(REFUSE_DUPLICATE_NATIVE_SESSION, caught.exception.reason)

    def test_an_undatable_lease_still_blocks(self):
        """A claimant we cannot date is one we cannot prove is gone."""
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        for expires in (None, "", "not-a-date"):
            with self.subTest(expires=expires):
                record = pi_session("S1", "THREAD-1")
                if expires is not None:
                    record["lease_expires_utc"] = expires
                with self.assertRaises(PiFingerprintRefused):
                    assert_native_session_is_exclusive("THREAD-1", [record], now_utc=now)


class PiReasoningLevelVocabularyTest(unittest.TestCase):
    def test_an_unknown_level_is_refused_rather_than_passed_through(self):
        """A typo was absent from the sparse map for the same reason a real level is.

        Treating absence as pass-through pinned a value Pi can only reject or substitute,
        so the binding stayed mismatched at every wake with nothing to point at.
        """
        runtime = {"fingerprint": {"provider": "openai", "model": "gpt-5.6-sol",
                                   "reasoning_level": "medum"}}
        with self.assertRaises(PiFingerprintRefused) as caught:
            assert_reasoning_level_supported(runtime, {"high": "high"})
        self.assertEqual(
            REFUSE_REASONING_LEVEL_UNSUPPORTED, caught.exception.reason
        )

    def test_a_known_level_absent_from_the_map_still_passes_through(self):
        runtime = {"fingerprint": {"provider": "openai", "model": "gpt-5.6-sol",
                                   "reasoning_level": "medium"}}
        assert_reasoning_level_supported(runtime, {"high": "high"})


class PiSharedContractProjectCoverageTest(unittest.TestCase):
    """Shared `bin/` contracts on Amiga and on a non-Amiga project.

    These were exercised only with project-less records, so neither required class was
    covered and a project-specific consumer could diverge with the suite green.
    """

    def test_exclusivity_holds_for_both_project_classes(self):
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        for project_id in ("amiga", "nuvyr"):
            with self.subTest(project_id=project_id):
                native = f"THREAD-{project_id}"
                holder = pi_session("S1", native, project_id=project_id)
                with self.assertRaises(PiFingerprintRefused) as caught:
                    assert_native_session_is_exclusive(native, [holder], now_utc=now)
                self.assertEqual(
                    REFUSE_DUPLICATE_NATIVE_SESSION, caught.exception.reason
                )
                # The owner may re-register its own binding.
                assert_native_session_is_exclusive(
                    native, [holder], owner_session_id="S1", now_utc=now
                )

    def test_the_fingerprint_pin_holds_for_both_project_classes(self):
        for project_id in ("amiga", "nuvyr"):
            with self.subTest(project_id=project_id):
                runtime = {
                    "project_id": project_id,
                    "fingerprint": {
                        "provider": "openai",
                        "model": "gpt-5.6-sol",
                        "reasoning_level": "medium",
                    },
                }
                observed = assert_fingerprint_matches(
                    runtime,
                    {"provider": "openai", "model": "gpt-5.6-sol",
                     "reasoningLevel": "medium"},
                )
                self.assertEqual("gpt-5.6-sol", observed["model"])
                with self.assertRaises(PiFingerprintRefused) as caught:
                    assert_fingerprint_matches(
                        runtime,
                        {"provider": "openai", "model": "k3",
                         "reasoningLevel": "medium"},
                    )
                self.assertEqual(
                    REFUSE_FINGERPRINT_MISMATCH, caught.exception.reason
                )


if __name__ == "__main__":
    unittest.main()
