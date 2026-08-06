"""GH-563 Slice 1A: bb client contract.

Every test drives recorded bb 0.35.1 fixtures. No test here contacts a live bb
server: liveness was proven in the GH-562 pilot, and this lane exists to freeze
the response/failure contract Slice 1B will call.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from llm_collab.bb_client import (
    EXECUTION_PROBE_EVENTS,
    MAX_EVENT_PAGE,
    MAX_RESPONSE_CHARS,
    PINNED_BB_VERSION,
    REFUSAL_AMBIGUOUS,
    REFUSAL_DISABLED,
    REFUSAL_IDENTITY_MISMATCH,
    REFUSAL_MALFORMED_RESPONSE,
    REFUSAL_PROFILE_MISMATCH,
    REFUSAL_TRANSPORT_FAILED,
    REFUSAL_VERSION_MISMATCH,
    SLICE_1A_PROFILE,
    BbClient,
    BbEventPage,
    BbProfile,
    BbQueued,
    BbRefusal,
    BbThread,
    BbTransportResult,
    BbTransportTimeout,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bb"

# The exact identities the recorded fixtures carry. Tests name them rather than
# passing placeholders, because identity binding is now part of the contract:
# a test that asked for "thr_x" and accepted a fixture for another thread would
# be asserting the defect.
SPAWNED_THREAD = "thr_9xirgjgdis"
SPAWNED_PROJECT = "proj_vny6bi5p8e"
SHOWN_THREAD = "thr_ru3nj2r8ur"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class RecordingTransport:
    """Replays recorded fixtures and records every argv it was asked to run.

    The call log is what proves the default-off and no-retry properties: those
    are claims about calls NOT made, and only a recorded log can show absence.
    """

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):  # noqa: ANN001 - transport protocol
        self.calls.append(list(argv))
        key = " ".join(argv[:2])
        response = self.responses.get(key, self.responses.get("*"))
        if isinstance(response, Exception):
            raise response
        if response is None:
            return BbTransportResult(0, "{}", "")
        return response

    def argv_for(self, *prefix: str) -> list[str]:
        return next(c for c in self.calls if c[: len(prefix)] == list(prefix))

    def count(self, *prefix: str) -> int:
        return len([c for c in self.calls if c[: len(prefix)] == list(prefix)])


def version_ok() -> BbTransportResult:
    return BbTransportResult(0, fixture("settings_version.json"), "")


def spawn_ok() -> BbTransportResult:
    return BbTransportResult(0, fixture("thread_spawn.json"), "")


def execution_ok() -> BbTransportResult:
    return BbTransportResult(0, fixture("thread_log_execution_high.json"), "")


def enabled_client(responses=None, **kwargs) -> tuple[BbClient, RecordingTransport]:
    merged = {"settings version": version_ok()}
    merged.update(responses or {})
    transport = RecordingTransport(merged)
    return BbClient(transport, enabled=True, **kwargs), transport


def spawning_client(**overrides) -> tuple[BbClient, RecordingTransport]:
    """A client whose spawn path is fully satisfied by recorded fixtures."""
    responses = {"thread spawn": spawn_ok(), "thread log": execution_ok()}
    responses.update(overrides)
    return enabled_client(responses)


def spawn(client: BbClient, *, profile: BbProfile = SLICE_1A_PROFILE):
    return client.spawn(project_id=SPAWNED_PROJECT, prompt="hello", profile=profile)


class DefaultOffTest(unittest.TestCase):
    def test_disabled_client_makes_no_call_of_any_kind(self):
        """AC7: default off means no process, no HTTP, no state read.

        Asserted as an empty call log rather than a return value, because the
        claim is about the absence of a native call.
        """
        transport = RecordingTransport({"*": version_ok()})
        client = BbClient(transport)  # enabled defaults to False
        for outcome in (
            client.verify_version(),
            client.spawn(project_id="p", prompt="x", profile=SLICE_1A_PROFILE),
            client.thread_state("thr_x"),
            client.events_after("thr_x", 0),
            client.queued_messages("thr_x"),
            client.send(thread_id="thr_x", message="m"),
        ):
            self.assertIsInstance(outcome, BbRefusal)
            self.assertEqual(REFUSAL_DISABLED, outcome.reason)
        self.assertEqual([], transport.calls, "disabled client must not touch the transport")


class VersionPinTest(unittest.TestCase):
    def test_pinned_version_is_accepted_with_exactly_one_probe(self):
        """AC1: the version cannot be known without asking, but asking twice is waste."""
        client, transport = enabled_client()
        self.assertEqual(PINNED_BB_VERSION, client.verify_version())
        self.assertEqual(PINNED_BB_VERSION, client.verify_version())
        self.assertEqual(
            1, transport.count("settings", "version"), "version must be probed once and cached"
        )

    def test_version_mismatch_refuses_and_performs_no_task_bearing_call(self):
        """AC1: a mismatch must not spawn. The proof is the absent spawn argv."""
        payload = json.loads(fixture("settings_version.json"))
        payload["currentVersion"] = "0.36.0"
        client, transport = enabled_client(
            {"settings version": BbTransportResult(0, json.dumps(payload), "")}
        )
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_VERSION_MISMATCH, outcome.reason)
        self.assertEqual(
            0,
            transport.count("thread", "spawn"),
            "a version mismatch must not reach a task-bearing operation",
        )

    def test_version_envelope_without_current_version_is_malformed(self):
        client, _ = enabled_client(
            {"settings version": BbTransportResult(0, '{"latestVersion": "0.35.1"}', "")}
        )
        outcome = client.verify_version()
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)


class EnvelopeValidationTest(unittest.TestCase):
    """AC3: spawn and show envelopes differ, and the difference is silent.

    Both fixtures are live recordings from bb 0.35.1, so these assertions are
    about observed behaviour, not documented behaviour.
    """

    def test_recorded_fixtures_really_do_differ_in_shape(self):
        spawn_payload = json.loads(fixture("thread_spawn.json"))
        show = json.loads(fixture("thread_show.json"))
        self.assertIn("id", spawn_payload)
        self.assertNotIn("thread", spawn_payload)
        self.assertNotIn("id", show)
        self.assertIn("thread", show)

    def test_spawn_envelope_carries_no_model_or_reasoning_level(self):
        """Why argv cannot be the profile proof: bb simply does not echo it here."""
        spawn_payload = json.loads(fixture("thread_spawn.json"))
        self.assertNotIn("model", spawn_payload)
        self.assertNotIn("reasoningLevel", spawn_payload)

    def test_spawn_validator_reads_the_top_level_envelope(self):
        thread = BbClient.validate_spawn_envelope(json.loads(fixture("thread_spawn.json")))
        self.assertIsInstance(thread, BbThread)
        self.assertEqual(SPAWNED_THREAD, thread.thread_id)
        self.assertEqual("pi", thread.provider_id)

    def test_show_validator_reads_the_nested_envelope(self):
        thread = BbClient.validate_show_envelope(json.loads(fixture("thread_show.json")))
        self.assertIsInstance(thread, BbThread)
        self.assertEqual(SHOWN_THREAD, thread.thread_id)

    def test_show_validator_refuses_a_spawn_envelope(self):
        """The exact misread that produced `status: None` during the pilot."""
        outcome = BbClient.validate_show_envelope(json.loads(fixture("thread_spawn.json")))
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_spawn_validator_refuses_a_show_envelope(self):
        outcome = BbClient.validate_spawn_envelope(json.loads(fixture("thread_show.json")))
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_missing_required_field_names_the_field(self):
        payload = json.loads(fixture("thread_spawn.json"))
        del payload["status"]
        outcome = BbClient.validate_spawn_envelope(payload)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertIn("status", outcome.detail)


class ProfileTest(unittest.TestCase):
    def test_frozen_triple_is_the_live_verified_one(self):
        """The installed CLI advertises low/high/max for k3 — there is no `medium`."""
        self.assertEqual("pi", SLICE_1A_PROFILE.provider)
        self.assertEqual("kimi-coding/k3", SLICE_1A_PROFILE.model)
        self.assertEqual("high", SLICE_1A_PROFILE.reasoning_level)

    def test_spawn_passes_the_supplied_triple_on_the_native_flag(self):
        """AC9: supplied, never selected — and on bb's real flag.

        The installed 0.35.1 CLI has `--reasoning-level`; `--effort` is rejected
        outright with `unknown option`, so the flag name is part of the contract.
        """
        client, transport = spawning_client()
        spawn(client)
        argv = transport.argv_for("thread", "spawn")
        self.assertNotIn("--effort", argv)
        self.assertEqual("pi", argv[argv.index("--provider") + 1])
        self.assertEqual("kimi-coding/k3", argv[argv.index("--model") + 1])
        self.assertEqual("high", argv[argv.index("--reasoning-level") + 1])

    def test_spawn_succeeds_only_after_reading_native_execution_evidence(self):
        thread = spawn(client=spawning_client()[0])
        self.assertIsInstance(thread, BbThread)
        self.assertEqual(SPAWNED_THREAD, thread.thread_id)

    def test_recorded_execution_event_reports_the_requested_level_exactly(self):
        """Equality is enforceable because bb was observed to record it verbatim."""
        events = json.loads(fixture("thread_log_execution_high.json"))
        execution = events[0]["data"]["execution"]
        self.assertEqual("kimi-coding/k3", execution["model"])
        self.assertEqual("high", execution["reasoningLevel"])

    def test_native_provider_disagreement_is_refused_as_an_orphan(self):
        """The native result is validated against the request, not trusted."""
        client, _ = spawning_client()
        outcome = spawn(
            client,
            profile=BbProfile(
                provider="claude-code", model="claude-opus-5", reasoning_level="high"
            ),
        )
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_PROFILE_MISMATCH, outcome.reason)
        self.assertEqual(SPAWNED_THREAD, outcome.native_thread_id)

    def test_native_model_disagreement_is_refused(self):
        """argv said k3; only the execution block can say what actually ran."""
        client, _ = spawning_client()
        outcome = spawn(
            client,
            profile=BbProfile(provider="pi", model="kimi-coding/k3-256k", reasoning_level="high"),
        )
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_PROFILE_MISMATCH, outcome.reason)
        self.assertIn("kimi-coding/k3", outcome.detail)
        self.assertEqual(SPAWNED_THREAD, outcome.native_thread_id)

    def test_native_reasoning_level_disagreement_is_refused(self):
        client, _ = spawning_client()
        outcome = spawn(
            client,
            profile=BbProfile(provider="pi", model="kimi-coding/k3", reasoning_level="max"),
        )
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_PROFILE_MISMATCH, outcome.reason)
        self.assertIn("max", outcome.detail)

    def test_project_disagreement_is_refused_as_an_orphan(self):
        """A thread created in the wrong project is a real thread, not a failure."""
        client, _ = spawning_client()
        outcome = client.spawn(
            project_id="proj_somewhere_else", prompt="hello", profile=SLICE_1A_PROFILE
        )
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_IDENTITY_MISMATCH, outcome.reason)
        self.assertEqual(
            SPAWNED_THREAD,
            outcome.native_thread_id,
            "the orphan's native id is the only way a caller can reconcile it",
        )

    def test_unverifiable_profile_leaves_an_orphan_rather_than_a_clean_failure(self):
        """A refusal with no native id reads as 'nothing happened' — it is retryable."""
        client, _ = spawning_client(**{"thread log": BbTransportResult(0, "[]", "")})
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(SPAWNED_THREAD, outcome.native_thread_id)

    def test_execution_probe_is_bounded(self):
        client, transport = spawning_client()
        spawn(client)
        argv = transport.argv_for("thread", "log")
        self.assertEqual(str(EXECUTION_PROBE_EVENTS), argv[argv.index("--limit") + 1])


class DeadlineTest(unittest.TestCase):
    def test_task_bearing_timeout_is_ambiguous_and_never_retried(self):
        """AC4 + lane contract: bb has no idempotency, so a retry is a second turn."""
        client, transport = enabled_client({"thread spawn": BbTransportTimeout("deadline")})
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)
        self.assertEqual(1, transport.count("thread", "spawn"), "a timed-out spawn is never retried")

    def test_a_read_gets_exactly_one_attempt_against_one_deadline(self):
        """A retry loop hands each attempt the full timeout, so N attempts is N deadlines.

        Slice 1A's contract is one native call per operation; this asserts the
        count, which is the only thing that distinguishes one deadline from
        several of the same size.
        """
        seen: list[float] = []

        class TimingOutReads(RecordingTransport):
            def __call__(self, argv, timeout):
                self.calls.append(list(argv))
                if argv[:2] == ["settings", "version"]:
                    return version_ok()
                seen.append(timeout)
                raise BbTransportTimeout("read timed out")

        transport = TimingOutReads()
        client = BbClient(transport, enabled=True, timeout_seconds=7.0)
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(1, transport.count("thread", "show"))
        self.assertEqual([7.0], seen)

    def test_malformed_read_is_not_retried(self):
        client, transport = enabled_client({"thread show": BbTransportResult(0, "not json", "")})
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)
        self.assertEqual(1, transport.count("thread", "show"))


class BoundedDecodingTest(unittest.TestCase):
    def test_oversized_stdout_is_refused_rather_than_truncated(self):
        client, _ = enabled_client(
            {"thread show": BbTransportResult(0, "x" * (MAX_RESPONSE_CHARS + 1), "")}
        )
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)
        self.assertIn("over the", outcome.detail)

    def test_oversized_stderr_is_bounded_before_it_reaches_a_detail_string(self):
        client, _ = enabled_client(
            {"thread show": BbTransportResult(1, "", "e" * (MAX_RESPONSE_CHARS + 1))}
        )
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)
        self.assertLess(len(outcome.detail), 200)

    def test_deeply_nested_json_becomes_a_typed_refusal(self):
        """Unconverted, this escapes the refusal contract as a bare RecursionError."""
        deep = "[" * 200_000 + "]" * 200_000
        client, _ = enabled_client({"thread show": BbTransportResult(0, deep, "")})
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)


class EventReplayTest(unittest.TestCase):
    def _log_client(self, payload: object, **kwargs):
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return enabled_client({"thread log": BbTransportResult(0, body, "")}, **kwargs)

    def test_replay_passes_the_native_limit_explicitly(self):
        """bb's own default is 100. An unstated limit is a cap we do not control."""
        client, transport = self._log_client(fixture("thread_log_after_seq.json"))
        client.events_after(SHOWN_THREAD, 0)
        argv = transport.argv_for("thread", "log")
        self.assertEqual(str(MAX_EVENT_PAGE), argv[argv.index("--limit") + 1])
        self.assertEqual("0", argv[argv.index("--after-seq") + 1])

    def test_a_short_page_is_not_truncated(self):
        client, _ = self._log_client(fixture("thread_log_after_seq.json"))
        page = client.events_after(SHOWN_THREAD, 0)
        self.assertIsInstance(page, BbEventPage)
        self.assertEqual((1, 2, 3), tuple(e.seq for e in page.events))
        self.assertFalse(page.truncated)
        self.assertIsNone(page.next_after_seq)

    def test_a_full_page_declares_its_own_truncation(self):
        """A list of exactly `limit` items is indistinguishable from a complete history."""
        entries = [
            {"id": f"evt_{n}", "type": "turn/started", "threadId": SHOWN_THREAD, "seq": n}
            for n in range(1, 4)
        ]
        client, _ = self._log_client(entries)
        page = client.events_after(SHOWN_THREAD, 0, limit=3)
        self.assertTrue(page.truncated)
        self.assertEqual(3, page.next_after_seq)

    def test_events_from_another_thread_are_refused(self):
        entries = [{"id": "evt_1", "type": "turn/started", "threadId": "thr_someone_else", "seq": 1}]
        client, _ = self._log_client(entries)
        outcome = client.events_after(SHOWN_THREAD, 0)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_IDENTITY_MISMATCH, outcome.reason)

    def test_sequences_that_do_not_advance_past_after_seq_are_refused(self):
        """The recorded defect: `[1,2,3]` was accepted for `after_seq=40`."""
        entries = [
            {"id": f"evt_{n}", "type": "turn/started", "threadId": SHOWN_THREAD, "seq": n}
            for n in (1, 2, 3)
        ]
        client, _ = self._log_client(entries)
        outcome = client.events_after(SHOWN_THREAD, 40)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)
        self.assertIn("40", outcome.detail)

    def test_non_increasing_sequences_are_refused(self):
        entries = [
            {"id": "evt_a", "type": "turn/started", "threadId": SHOWN_THREAD, "seq": 5},
            {"id": "evt_b", "type": "turn/started", "threadId": SHOWN_THREAD, "seq": 5},
        ]
        client, _ = self._log_client(entries)
        outcome = client.events_after(SHOWN_THREAD, 0)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_a_boolean_sequence_is_not_an_integer_sequence(self):
        """`True` passes `isinstance(x, int)`; it is still not a sequence number."""
        entries = [{"id": "evt_a", "type": "turn/started", "threadId": SHOWN_THREAD, "seq": True}]
        client, _ = self._log_client(entries)
        outcome = client.events_after(SHOWN_THREAD, 0)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_a_page_larger_than_the_requested_limit_is_refused(self):
        entries = [
            {"id": f"evt_{n}", "type": "turn/started", "threadId": SHOWN_THREAD, "seq": n}
            for n in range(1, 6)
        ]
        client, _ = self._log_client(entries)
        outcome = client.events_after(SHOWN_THREAD, 0, limit=2)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_a_limit_beyond_the_page_bound_is_refused(self):
        client, transport = self._log_client(fixture("thread_log_after_seq.json"))
        outcome = client.events_after(SHOWN_THREAD, 0, limit=MAX_EVENT_PAGE + 1)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(0, transport.count("thread", "log"))


class SendTest(unittest.TestCase):
    def test_send_returns_a_typed_acceptance_bound_to_the_thread(self):
        client, transport = enabled_client(
            {"thread tell": BbTransportResult(0, fixture("thread_tell_queued.json"), "")}
        )
        queued = client.send(thread_id=SPAWNED_THREAD, message="m")
        self.assertIsInstance(queued, BbQueued)
        self.assertEqual(SPAWNED_THREAD, queued.thread_id)
        self.assertEqual("queue", queued.mode)
        argv = transport.argv_for("thread", "tell")
        self.assertIn("--json", argv)
        self.assertEqual("queue", argv[argv.index("--mode") + 1])

    def test_send_refuses_a_response_for_another_thread(self):
        client, _ = enabled_client(
            {"thread tell": BbTransportResult(0, fixture("thread_tell_queued.json"), "")}
        )
        outcome = client.send(thread_id="thr_someone_else", message="m")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_IDENTITY_MISMATCH, outcome.reason)

    def test_send_refuses_unvalidated_text(self):
        """BbClient is the sole response validator; raw stdout is not a result."""
        client, _ = enabled_client(
            {"thread tell": BbTransportResult(0, "Thread thr_x updated", "")}
        )
        outcome = client.send(thread_id=SPAWNED_THREAD, message="m")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_send_refuses_a_response_that_does_not_report_ok(self):
        payload = json.loads(fixture("thread_tell_queued.json"))
        payload["ok"] = "true"  # truthy string, not the boolean bb returns
        client, _ = enabled_client(
            {"thread tell": BbTransportResult(0, json.dumps(payload), "")}
        )
        outcome = client.send(thread_id=SPAWNED_THREAD, message="m")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_send_refuses_steering(self):
        """Steering is GH-562 case 4 and stays Phase 2."""
        client, transport = enabled_client()
        outcome = client.send(thread_id=SPAWNED_THREAD, message="m", mode="steer-if-active")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(0, transport.count("thread", "tell"))


class ThreadStateTest(unittest.TestCase):
    def test_thread_state_returns_the_requested_thread(self):
        client, _ = enabled_client(
            {"thread show": BbTransportResult(0, fixture("thread_show.json"), "")}
        )
        thread = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(thread, BbThread)
        self.assertEqual("idle", thread.status)

    def test_thread_state_refuses_a_response_for_another_thread(self):
        """The recorded defect: a state read for one thread answered with another."""
        client, _ = enabled_client(
            {"thread show": BbTransportResult(0, fixture("thread_show.json"), "")}
        )
        outcome = client.thread_state("thr_someone_else")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_IDENTITY_MISMATCH, outcome.reason)

    def test_queue_drain_observation_counts_queued_messages(self):
        client, _ = enabled_client(
            {"thread queue": BbTransportResult(0, fixture("thread_queue_empty.json"), "")}
        )
        self.assertEqual(0, client.queued_messages(SHOWN_THREAD))

    def test_transport_failure_is_typed_not_raised(self):
        client, _ = enabled_client({"thread show": BbTransportResult(1, "", "boom")})
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_TRANSPORT_FAILED, outcome.reason)
        self.assertIn("boom", outcome.detail)


if __name__ == "__main__":
    unittest.main()
