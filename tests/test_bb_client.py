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
    PINNED_BB_VERSION,
    REFUSAL_AMBIGUOUS,
    REFUSAL_DISABLED,
    REFUSAL_MALFORMED_RESPONSE,
    REFUSAL_PROFILE_MISMATCH,
    REFUSAL_TIMED_OUT,
    REFUSAL_TRANSPORT_FAILED,
    REFUSAL_VERSION_MISMATCH,
    SLICE_1A_PROFILE,
    BbClient,
    BbProfile,
    BbRefusal,
    BbThread,
    BbTransportResult,
    BbTransportTimeout,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bb"


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


def version_ok() -> BbTransportResult:
    return BbTransportResult(0, fixture("settings_version.json"), "")


def enabled_client(responses=None, **kwargs) -> tuple[BbClient, RecordingTransport]:
    merged = {"settings version": version_ok()}
    merged.update(responses or {})
    transport = RecordingTransport(merged)
    return BbClient(transport, enabled=True, **kwargs), transport


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
        probes = [c for c in transport.calls if c[:2] == ["settings", "version"]]
        self.assertEqual(1, len(probes), "version must be probed exactly once and cached")

    def test_version_mismatch_refuses_and_performs_no_task_bearing_call(self):
        """AC1: a mismatch must not spawn. The proof is the absent spawn argv."""
        payload = json.loads(fixture("settings_version.json"))
        payload["currentVersion"] = "0.36.0"
        client, transport = enabled_client(
            {"settings version": BbTransportResult(0, json.dumps(payload), "")}
        )
        outcome = client.spawn(project_id="p", prompt="x", profile=SLICE_1A_PROFILE)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_VERSION_MISMATCH, outcome.reason)
        self.assertNotIn(
            ["thread", "spawn"],
            [c[:2] for c in transport.calls],
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
        spawn = json.loads(fixture("thread_spawn.json"))
        show = json.loads(fixture("thread_show.json"))
        self.assertIn("id", spawn)
        self.assertNotIn("thread", spawn)
        self.assertNotIn("id", show)
        self.assertIn("thread", show)

    def test_spawn_validator_reads_the_top_level_envelope(self):
        thread = BbClient.validate_spawn_envelope(json.loads(fixture("thread_spawn.json")))
        self.assertIsInstance(thread, BbThread)
        self.assertTrue(thread.thread_id.startswith("thr_"))
        self.assertEqual("pi", thread.provider_id)

    def test_show_validator_reads_the_nested_envelope(self):
        thread = BbClient.validate_show_envelope(json.loads(fixture("thread_show.json")))
        self.assertIsInstance(thread, BbThread)
        self.assertTrue(thread.thread_id.startswith("thr_"))

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
    def test_spawn_passes_the_supplied_triple_verbatim(self):
        """AC9: supplied, never selected. The argv is the evidence."""
        client, transport = enabled_client(
            {"thread spawn": BbTransportResult(0, fixture("thread_spawn.json"), "")}
        )
        client.spawn(project_id="proj_x", prompt="hello", profile=SLICE_1A_PROFILE)
        spawn_argv = next(c for c in transport.calls if c[:2] == ["thread", "spawn"])
        self.assertIn("--provider", spawn_argv)
        self.assertEqual("pi", spawn_argv[spawn_argv.index("--provider") + 1])
        self.assertEqual("kimi-coding/k3", spawn_argv[spawn_argv.index("--model") + 1])
        self.assertEqual("low", spawn_argv[spawn_argv.index("--effort") + 1])

    def test_native_provider_disagreement_is_refused(self):
        """The native result is validated against the request, not trusted."""
        client, _ = enabled_client(
            {"thread spawn": BbTransportResult(0, fixture("thread_spawn.json"), "")}
        )
        outcome = client.spawn(
            project_id="proj_x",
            prompt="hello",
            profile=BbProfile(provider="claude-code", model="claude-opus-5", effort="low"),
        )
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_PROFILE_MISMATCH, outcome.reason)


class RetryPolicyTest(unittest.TestCase):
    def test_task_bearing_timeout_is_ambiguous_and_never_retried(self):
        """AC4 + lane contract: bb has no idempotency, so a retry is a second turn."""
        client, transport = enabled_client(
            {"thread spawn": BbTransportTimeout("deadline exceeded")}
        )
        outcome = client.spawn(project_id="p", prompt="x", profile=SLICE_1A_PROFILE)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)
        spawns = [c for c in transport.calls if c[:2] == ["thread", "spawn"]]
        self.assertEqual(1, len(spawns), "a timed-out spawn must never be retried")

    def test_read_probe_may_retry_within_one_deadline(self):
        class FlakyReads(RecordingTransport):
            def __call__(self, argv, timeout):
                self.calls.append(list(argv))
                if argv[:2] == ["settings", "version"]:
                    return version_ok()
                if len([c for c in self.calls if c[:2] == ["thread", "show"]]) == 1:
                    raise BbTransportTimeout("first read timed out")
                return BbTransportResult(0, fixture("thread_show.json"), "")

        transport = FlakyReads()
        client = BbClient(transport, enabled=True, read_probe_attempts=2)
        thread = client.thread_state("thr_x")
        self.assertIsInstance(thread, BbThread)
        self.assertEqual(2, len([c for c in transport.calls if c[:2] == ["thread", "show"]]))

    def test_malformed_read_is_not_retried(self):
        client, transport = enabled_client(
            {"thread show": BbTransportResult(0, "not json", "")}, read_probe_attempts=3
        )
        outcome = client.thread_state("thr_x")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)
        self.assertEqual(1, len([c for c in transport.calls if c[:2] == ["thread", "show"]]))


class OperationsTest(unittest.TestCase):
    def test_events_after_seq_returns_typed_events(self):
        client, transport = enabled_client(
            {"thread log": BbTransportResult(0, fixture("thread_log_after_seq.json"), "")}
        )
        events = client.events_after("thr_x", 0)
        self.assertTrue(events)
        self.assertTrue(all(isinstance(e.seq, int) for e in events))
        argv = next(c for c in transport.calls if c[:2] == ["thread", "log"])
        self.assertIn("--after-seq", argv)

    def test_queue_drain_observation_counts_queued_messages(self):
        client, _ = enabled_client(
            {"thread queue": BbTransportResult(0, fixture("thread_queue_empty.json"), "")}
        )
        self.assertEqual(0, client.queued_messages("thr_x"))

    def test_send_defaults_to_queue_and_refuses_steering(self):
        """AC6-adjacent: steering is GH-562 case 4 and stays Phase 2."""
        client, transport = enabled_client(
            {"thread tell": BbTransportResult(0, "Thread thr_x updated", "")}
        )
        self.assertEqual("Thread thr_x updated", client.send(thread_id="thr_x", message="m"))
        argv = next(c for c in transport.calls if c[:2] == ["thread", "tell"])
        self.assertEqual("queue", argv[argv.index("--mode") + 1])

        outcome = client.send(thread_id="thr_x", message="m", mode="steer-if-active")
        self.assertIsInstance(outcome, BbRefusal)

    def test_transport_failure_is_typed_not_raised(self):
        client, _ = enabled_client(
            {"thread show": BbTransportResult(1, "", "boom")}
        )
        outcome = client.thread_state("thr_x")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_TRANSPORT_FAILED, outcome.reason)
        self.assertIn("boom", outcome.detail)


if __name__ == "__main__":
    unittest.main()
