"""GH-563 Slice 1A: bb client contract.

Every test drives recorded bb 0.35.1 fixtures. No test here contacts a live bb
server: liveness was proven in the GH-562 pilot, and this lane exists to freeze
the response/failure contract Slice 1B will call.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_collab.bb_client import (
    EXECUTION_PROBE_EVENTS,
    KILL_CHILD_REAP_TIMEOUT_SECONDS,
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
    BbChildReapTimeout,
    BbClient,
    BbEventPage,
    BbProfile,
    BbQueued,
    BbRefusal,
    BbThread,
    BbTransportResult,
    BbTransportTimeout,
    BbExecutableRefused,
    BbProjectIdRefused,
    BbResponseDecodeError,
    BbResponseTooLarge,
    _read_bounded,
    subprocess_transport,
    bb_executable_from_project,
    bb_project_id_from_project,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bb"

# A version bb will never report, so a future pin bump cannot silently turn the
# mismatch case into a match. The previous literal was the next release and did
# exactly that when the pin moved to it.
MISMATCHED_BB_VERSION = "0.0.1"

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
    # The recorded historical k3 spawn is hidden. Mutate only visibility to the
    # newly required state; a live BB 0.37.0 visible Luna spawn proves the field
    # and k3 is unavailable this billing cycle, so the exact old profile cannot
    # be re-induced for a fresh combined recording.
    envelope = json.loads(fixture("thread_spawn.json"))
    envelope["visibility"] = "visible"
    return BbTransportResult(0, json.dumps(envelope), "")


def execution_ok() -> BbTransportResult:
    return BbTransportResult(0, fixture("thread_log_execution_high.json"), "")


def execution_events(**mutations) -> BbTransportResult:
    """The recorded execution log with the spawn-authority event field replaced.

    Mutating the archived event rather than hand-writing one keeps the rest of
    the record real, so the test isolates exactly the selector under proof.
    """
    events = json.loads(fixture("thread_log_execution_high.json"))
    for key, value in mutations.items():
        if key == "type":
            events[0]["type"] = value
        else:
            events[0]["data"][key] = value
    return BbTransportResult(0, json.dumps(events), "")


def spawn_envelope_without(field: str) -> BbTransportResult:
    envelope = json.loads(spawn_ok().stdout)
    del envelope[field]
    return BbTransportResult(0, json.dumps(envelope), "")


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
        payload["currentVersion"] = MISMATCHED_BB_VERSION
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
        thread = BbClient.validate_spawn_envelope(json.loads(spawn_ok().stdout))
        self.assertIsInstance(thread, BbThread)
        self.assertEqual(SPAWNED_THREAD, thread.thread_id)
        self.assertEqual("pi", thread.provider_id)

    def test_spawn_validator_refuses_a_hidden_thread(self):
        outcome = BbClient.validate_spawn_envelope(json.loads(fixture("thread_spawn.json")))
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_IDENTITY_MISMATCH, outcome.reason)
        self.assertIn("visibility", outcome.detail)

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
        payload = json.loads(spawn_ok().stdout)
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
        self.assertEqual("visible", argv[argv.index("--visibility") + 1])

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

    def test_execution_evidence_from_another_event_type_is_refused(self):
        """The profile record must come from the spawn's own turn event.

        The archived spawn also emits `client/thread/start` carrying `source:
        "spawn"`, so source alone does not identify the authority.
        """
        client, _ = spawning_client(
            **{"thread log": execution_events(type="client/thread/start")}
        )
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(SPAWNED_THREAD, outcome.native_thread_id)

    def test_execution_evidence_from_a_non_spawn_source_is_refused(self):
        """A later `client/turn/requested` from a tell carries the same type."""
        client, _ = spawning_client(**{"thread log": execution_events(source="tell")})
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(SPAWNED_THREAD, outcome.native_thread_id)

    def test_an_object_envelope_without_an_id_is_ambiguous(self):
        """Routing through orphan() is not enough — orphan() must also refuse cleanly.

        With no recoverable id there is nothing to reconcile against, and bb may
        still have created the thread, so the only honest surface is ambiguous.
        """
        client, _ = spawning_client(
            **{"thread spawn": BbTransportResult(0, '{"projectId": "p"}', "")}
        )
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)
        self.assertIsNone(outcome.native_thread_id)

    def test_a_non_object_envelope_is_ambiguous(self):
        """Valid JSON of the wrong shape still followed an exit-0 spawn."""
        client, _ = spawning_client(**{"thread spawn": BbTransportResult(0, "[]", "")})
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)

    def test_a_malformed_envelope_still_reports_the_created_thread(self):
        """bb created the thread before this envelope existed.

        Dropping the id here would report a real thread as a clean failure and
        invite a retry that creates a second one.
        """
        client, _ = spawning_client(**{"thread spawn": spawn_envelope_without("status")})
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)
        self.assertEqual(SPAWNED_THREAD, outcome.native_thread_id)

    def test_native_provider_disagreement_is_refused_as_an_orphan(self):
        """The native result is validated against the request, not trusted."""
        client, _ = spawning_client()
        outcome = spawn(
            client,
            profile=BbProfile(
                provider="claude-code", model="claude-opus-5[1m]", reasoning_level="high"
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
        """AC4: native spawn has no caller replay key, so retry is a second turn."""
        client, transport = enabled_client({"thread spawn": BbTransportTimeout("deadline")})
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)
        self.assertEqual(1, transport.count("thread", "spawn"), "a timed-out spawn is never retried")

    def test_task_bearing_reap_timeout_states_that_exit_status_is_unavailable(self):
        client, transport = enabled_client(
            {
                "thread spawn": BbChildReapTimeout(
                    "SIGKILL sent; exit status is unavailable and the child remains unreaped"
                )
            }
        )
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)
        self.assertIn("exit status is unavailable", outcome.detail)
        self.assertIn("remains unreaped", outcome.detail)
        self.assertEqual(1, transport.count("thread", "spawn"))

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
        """The invariant here is the bounded detail; a nonzero exit is a transport failure.

        The size bound is checked before the exit code so the stream never reaches
        a detail string — but the exit code is known either way, and reporting a
        reported failure as malformed would let the task seam convert it.
        """
        client, _ = enabled_client(
            {"thread show": BbTransportResult(1, "", "e" * (MAX_RESPONSE_CHARS + 1))}
        )
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_TRANSPORT_FAILED, outcome.reason)
        self.assertLess(len(outcome.detail), 200)

    def test_an_oversized_nonzero_task_response_stays_a_transport_failure(self):
        """The size bound must not broaden the transport-failure classification.

        A nonzero exit keeps the classification it already had; that is not a
        claim it had no side effect, which stays unestablished pending GH-570.
        Without this, an ordinary spawn rejection carrying a large diagnostic
        would silently become retry-suppressing.
        """
        oversized = "x" * (MAX_RESPONSE_CHARS + 1)
        for stream, response in (
            ("stdout", BbTransportResult(1, oversized, "")),
            ("stderr", BbTransportResult(1, "", oversized)),
        ):
            with self.subTest(stream=stream):
                client, _ = spawning_client(**{"thread spawn": response})
                outcome = spawn(client)
                self.assertIsInstance(outcome, BbRefusal)
                self.assertEqual(REFUSAL_TRANSPORT_FAILED, outcome.reason)
                self.assertLess(len(outcome.detail), 200)

    def test_deeply_nested_json_becomes_a_typed_refusal(self):
        """Unconverted, this escapes the refusal contract as a bare RecursionError."""
        deep = "[" * 200_000 + "]" * 200_000
        client, _ = enabled_client({"thread show": BbTransportResult(0, deep, "")})
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_an_overlong_json_integer_becomes_a_typed_refusal(self):
        """A bare ValueError, not a JSONDecodeError — the size bound never sees it.

        sys.get_int_max_str_digits() is 4300 by default, far below
        MAX_RESPONSE_CHARS, so this arrives as a well-formed response that the
        decoder refuses on a limit of its own.
        """
        self.assertLess(sys.get_int_max_str_digits(), MAX_RESPONSE_CHARS)
        client, _ = enabled_client(
            {"thread show": BbTransportResult(0, "1" * 5000, "")}
        )
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_reader_decode_failure_is_ambiguous_for_tasks(self):
        client, _ = spawning_client(
            **{"thread spawn": BbResponseDecodeError("invalid native text")}
        )
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal, "failures=gh586_task_decode_is_typed")
        self.assertEqual(
            REFUSAL_AMBIGUOUS,
            outcome.reason,
            "failures=gh586_task_decode_must_be_ambiguous",
        )

    def test_reader_decode_failure_is_malformed_for_reads(self):
        client, _ = enabled_client(
            {"thread show": BbResponseDecodeError("invalid native text")}
        )
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal, "failures=gh586_read_decode_is_typed")
        self.assertEqual(
            REFUSAL_MALFORMED_RESPONSE,
            outcome.reason,
            "failures=gh586_read_decode_must_be_malformed",
        )

    def test_an_oversized_task_response_is_ambiguous_on_either_stream(self):
        """The size bound is checked before the exit code, so exit-0 reaches it.

        Left clean this bypasses both the decode conversion and spawn()'s orphan
        seam, so an oversized spawn response could invite a duplicate spawn.
        """
        oversized = "x" * (MAX_RESPONSE_CHARS + 1)
        for stream, response in (
            ("stdout", BbTransportResult(0, oversized, "")),
            ("stderr", BbTransportResult(0, "{}", oversized)),
        ):
            with self.subTest(stream=stream):
                client, _ = spawning_client(**{"thread spawn": response})
                outcome = spawn(client)
                self.assertIsInstance(outcome, BbRefusal)
                self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)

    def test_an_oversized_read_response_stays_malformed(self):
        """A read performed nothing, so its size bound is not ambiguous."""
        client, _ = enabled_client(
            {"thread show": BbTransportResult(0, "x" * (MAX_RESPONSE_CHARS + 1), "")}
        )
        outcome = client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, outcome.reason)

    def test_native_overflow_is_ambiguous_for_tasks_but_malformed_for_reads(self):
        """A native stream overflow must use the same task/read split as post-return bounds."""
        task_client, _ = spawning_client(
            **{"thread spawn": BbResponseTooLarge("native stdout exceeded the bound")}
        )
        task_outcome = spawn(task_client)
        self.assertIsInstance(task_outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, task_outcome.reason)

        read_client, _ = enabled_client(
            {"thread show": BbResponseTooLarge("native stdout exceeded the bound")}
        )
        read_outcome = read_client.thread_state(SHOWN_THREAD)
        self.assertIsInstance(read_outcome, BbRefusal)
        self.assertEqual(REFUSAL_MALFORMED_RESPONSE, read_outcome.reason)

    def test_an_undecodable_task_response_is_ambiguous_not_a_clean_failure(self):
        """bb exited 0, so the thread exists; only its report was unreadable.

        Calling this malformed would invite the retry that creates a second real
        thread, which is the whole reason task-bearing calls are never retried.
        """
        client, _ = spawning_client(**{"thread spawn": BbTransportResult(0, "not json", "")})
        outcome = spawn(client)
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)

    def test_an_undecodable_read_response_stays_malformed(self):
        """A read performed nothing, so there is nothing to be ambiguous about."""
        client, _ = enabled_client({"thread show": BbTransportResult(0, "not json", "")})
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

    def test_turn_scoped_event_preserves_its_native_turn_identity(self):
        entries = [
            {
                "id": "evt_terminal",
                "type": "turn/completed",
                "threadId": SHOWN_THREAD,
                "seq": 1,
                "scope": {"kind": "turn", "turnId": "turn-native-1"},
            }
        ]
        client, _ = self._log_client(entries)
        page = client.events_after(SHOWN_THREAD, 0)
        self.assertEqual("turn-native-1", page.events[0].turn_id)

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
        """Ambiguous, not a clean identity failure: bb already exited 0."""
        client, _ = enabled_client(
            {"thread tell": BbTransportResult(0, fixture("thread_tell_queued.json"), "")}
        )
        outcome = client.send(thread_id="thr_someone_else", message="m")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)

    def test_send_refuses_a_malformed_mode_as_ambiguous(self):
        payload = json.loads(fixture("thread_tell_queued.json"))
        payload["mode"] = "steer"
        client, _ = enabled_client(
            {"thread tell": BbTransportResult(0, json.dumps(payload), "")}
        )
        outcome = client.send(thread_id=SPAWNED_THREAD, message="m")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)

    def test_send_refuses_a_non_object_envelope_as_ambiguous(self):
        client, _ = enabled_client({"thread tell": BbTransportResult(0, "[]", "")})
        outcome = client.send(thread_id=SPAWNED_THREAD, message="m")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)

    def test_send_rejects_an_unsupported_mode_before_calling_bb(self):
        """The pre-execution boundary: nothing ran, so this stays a clean refusal.

        Without this the ambiguous rule above would swallow the whole method and
        a caller could never distinguish 'we did not try' from 'we cannot tell'.
        """
        client, transport = enabled_client({})
        outcome = client.send(thread_id=SPAWNED_THREAD, message="m", mode="steer")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertNotEqual(REFUSAL_AMBIGUOUS, outcome.reason)
        self.assertEqual(0, transport.count("thread", "tell"))

    def test_send_refuses_unvalidated_text(self):
        """BbClient is the sole response validator; raw stdout is not a result.

        Ambiguous rather than malformed: `tell` is task-bearing, so an exit-0
        response we cannot read means the message may already be queued.
        """
        client, _ = enabled_client(
            {"thread tell": BbTransportResult(0, "Thread thr_x updated", "")}
        )
        outcome = client.send(thread_id=SPAWNED_THREAD, message="m")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)

    def test_send_refuses_a_response_that_does_not_report_ok(self):
        payload = json.loads(fixture("thread_tell_queued.json"))
        payload["ok"] = "true"  # truthy string, not the boolean bb returns
        client, _ = enabled_client(
            {"thread tell": BbTransportResult(0, json.dumps(payload), "")}
        )
        outcome = client.send(thread_id=SPAWNED_THREAD, message="m")
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_AMBIGUOUS, outcome.reason)

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


class ProductionTransportTest(unittest.TestCase):
    """GH-570: the production transport bounds each stream WHILE READING.

    Slice 1A's client check runs after the transport returns, so it can refuse an
    oversized response but cannot stop one being read into memory. These tests are
    about the read boundary, which is why they drive a real subprocess rather than
    a fixture: a fake transport cannot demonstrate that bytes were never
    accumulated.
    """

    def _python(self, script: str) -> list[str]:
        return [sys.executable, "-c", script]

    def test_a_normal_response_is_returned_intact(self):
        transport = subprocess_transport(self._python("print('{\"ok\": true}')"))
        result = transport([], 30.0)
        self.assertEqual(0, result.exit_code)
        self.assertIn('"ok": true', result.stdout)

    def test_oversized_stdout_raises_instead_of_accumulating(self):
        """The bound is the point: refusing after the fact is what 1A already did."""
        script = f"import sys; sys.stdout.write('x' * {MAX_RESPONSE_CHARS + 4096})"
        transport = subprocess_transport(self._python(script))
        with self.assertRaises(BbResponseTooLarge):
            transport([], 30.0)

    def test_oversized_stderr_raises_too(self):
        """stderr is untrusted input as much as stdout, and reaches detail strings."""
        script = f"import sys; sys.stderr.write('e' * {MAX_RESPONSE_CHARS + 4096})"
        transport = subprocess_transport(self._python(script))
        with self.assertRaises(BbResponseTooLarge):
            transport([], 30.0)

    def test_a_response_exactly_at_the_bound_is_accepted(self):
        """Off-by-one matters: the cap is a limit, not a forbidden size."""
        transport = subprocess_transport(
            self._python(f"import sys; sys.stdout.write('x' * {MAX_RESPONSE_CHARS})"),
            max_response_chars=MAX_RESPONSE_CHARS,
        )
        result = transport([], 30.0)
        self.assertEqual(MAX_RESPONSE_CHARS, len(result.stdout))

    def test_one_char_over_the_bound_is_refused(self):
        transport = subprocess_transport(
            self._python(f"import sys; sys.stdout.write('x' * {MAX_RESPONSE_CHARS + 1})"),
            max_response_chars=MAX_RESPONSE_CHARS,
        )
        with self.assertRaises(BbResponseTooLarge):
            transport([], 30.0)

    def test_the_stream_is_never_asked_for_more_than_the_bound_plus_one(self):
        """The load-bearing property: bytes are not accumulated, then rejected.

        Asserting only that an exception is raised does NOT discriminate — a
        transport that reads the whole stream and checks the length afterwards
        raises too, while having already materialised it. This counts what the
        stream was actually asked to hand over.
        """

        class CountingStream:
            def __init__(self, total: int) -> None:
                self.remaining = total
                self.served = 0

            def read(self, size: int = -1) -> str:
                take = self.remaining if size is None or size < 0 else min(size, self.remaining)
                self.remaining -= take
                self.served += take
                return "x" * take

        limit = 4096
        stream = CountingStream(limit * 50)
        with self.assertRaises(BbResponseTooLarge):
            _read_bounded(stream, limit)
        self.assertLessEqual(
            stream.served,
            limit + 1,
            f"read {stream.served} chars for a {limit} bound — the stream was accumulated",
        )

    def test_a_stream_shorter_than_the_bound_is_read_whole(self):
        """The bound must not truncate a legitimate response."""

        class Stream:
            def __init__(self, text: str) -> None:
                self.buf = text

            def read(self, size: int = -1) -> str:
                take = self.buf if size is None or size < 0 else self.buf[:size]
                self.buf = self.buf[len(take):]
                return take

        self.assertEqual("hello", _read_bounded(Stream("hello"), 4096))

    def test_a_nonzero_exit_is_reported_not_raised(self):
        """A failing bb call is a result to classify, not a transport error."""
        transport = subprocess_transport(
            self._python("import sys; sys.stderr.write('boom'); sys.exit(3)")
        )
        result = transport([], 30.0)
        self.assertEqual(3, result.exit_code)
        self.assertIn("boom", result.stderr)

    def test_a_slow_child_raises_the_timeout_the_client_maps_to_ambiguous(self):
        transport = subprocess_transport(self._python("import time; time.sleep(30)"))
        with self.assertRaises(BbTransportTimeout):
            transport([], 0.5)

    def test_killed_child_is_reaped_with_its_exit_status_intact(self):
        """Bounding the reap must not turn the common completed wait into a leak."""
        import llm_collab.bb_client as bb

        process = None

        class ExitingAfterKill:
            def __init__(self, *_args, **_kwargs):
                nonlocal process
                process = self
                self.stdout = io.StringIO("{}")
                self.stderr = io.StringIO("")
                self.wait_calls = []
                self.returncode = None

            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                if len(self.wait_calls) == 1:
                    raise subprocess.TimeoutExpired("fake-bb", timeout)
                self.returncode = -9
                return self.returncode

            def kill(self):
                pass

        transport = subprocess_transport(["fake-bb"])
        with patch.object(bb.subprocess, "Popen", ExitingAfterKill):
            with self.assertRaises(BbTransportTimeout):
                transport([], 0.5)

        self.assertIsNotNone(process)
        self.assertEqual(-9, process.returncode)
        self.assertEqual(KILL_CHILD_REAP_TIMEOUT_SECONDS, process.wait_calls[1])

    def test_unreapable_child_reports_a_bounded_explicit_outcome(self):
        """GH-653: an unkillable child cannot wedge the transport's caller."""
        import llm_collab.bb_client as bb

        test_case = self
        process = None

        class NeverExits:
            def __init__(self, *_args, **_kwargs):
                nonlocal process
                process = self
                self.stdout = io.StringIO("{}")
                self.stderr = io.StringIO("")
                self.wait_calls = []

            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                if timeout is None:
                    test_case.fail("kill_child called process.wait() without a timeout")
                raise subprocess.TimeoutExpired("fake-bb", timeout)

            def kill(self):
                pass

        transport = subprocess_transport(["fake-bb"])
        started = time.monotonic()
        with patch.object(bb.subprocess, "Popen", NeverExits):
            with self.assertRaises(BbChildReapTimeout) as raised:
                transport([], 0.5)

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNotNone(process)
        self.assertEqual(KILL_CHILD_REAP_TIMEOUT_SECONDS, process.wait_calls[1])
        self.assertIn("exit status is unavailable", str(raised.exception))
        self.assertIn("remains unreaped", str(raised.exception))

    def test_reap_timeout_abandons_only_daemon_reader_threads(self):
        """Blocked readers may survive the call, but must not hold interpreter exit."""
        import llm_collab.bb_client as bb

        release_reads = threading.Event()
        reads_started = [threading.Event(), threading.Event()]
        streams = []

        class BlockingStream:
            def __init__(self, index):
                self.index = index
                self.closed = False
                streams.append(self)

            def read(self, _size=-1):
                reads_started[self.index].set()
                release_reads.wait()
                return ""

            def close(self):
                self.closed = True

        class NeverExits:
            def __init__(self, *_args, **_kwargs):
                self.stdout = BlockingStream(0)
                self.stderr = BlockingStream(1)

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("fake-bb", timeout)

            def kill(self):
                pass

        before_threads = set(threading.enumerate())
        readers = []
        try:
            transport = subprocess_transport(["fake-bb"])
            with patch.object(bb.subprocess, "Popen", NeverExits):
                with self.assertRaises(BbChildReapTimeout):
                    transport([], 0.1)

            self.assertTrue(all(started.wait(1) for started in reads_started))
            readers = [
                thread
                for thread in threading.enumerate()
                if thread not in before_threads and thread.name.startswith("bb-subprocess-")
            ]
            self.assertEqual(2, len(readers), "both blocked readers must be accounted for")
            self.assertTrue(
                all(thread.daemon for thread in readers),
                "a reap timeout left a non-daemon reader able to wedge interpreter exit",
            )
            self.assertTrue(all(thread.is_alive() for thread in readers))
            self.assertTrue(
                all(not stream.closed for stream in streams),
                "blocked descriptors were described as closed even though their reads survived",
            )
        finally:
            release_reads.set()
            for thread in readers:
                thread.join(timeout=1)

    def test_reap_timeout_does_not_delay_interpreter_exit(self):
        script = textwrap.dedent(
            """
            import subprocess
            import threading
            from unittest.mock import patch

            import llm_collab.bb_client as bb

            never = threading.Event()

            class BlockingStream:
                def read(self, _size=-1):
                    never.wait()
                    return ""

                def close(self):
                    raise AssertionError("reap timeout tried to close a blocked stream")

            class NeverExits:
                def __init__(self, *_args, **_kwargs):
                    self.stdout = BlockingStream()
                    self.stderr = BlockingStream()

                def wait(self, timeout=None):
                    raise subprocess.TimeoutExpired("fake-bb", timeout)

                def kill(self):
                    pass

            transport = bb.subprocess_transport(["fake-bb"])
            with patch.object(bb.subprocess, "Popen", NeverExits):
                try:
                    transport([], 0.1)
                except bb.BbChildReapTimeout:
                    pass
                else:
                    raise AssertionError("reap timeout was not reported")
            """
        )
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertLess(
            time.monotonic() - started,
            1.0,
            "blocked reader threads delayed interpreter exit",
        )

    def test_launch_time_is_charged_against_the_budget(self):
        """GH-584: launch cost is subtracted from the post-launch waits."""
        import llm_collab.bb_client as bb

        timed_waits = []

        class SlowLaunch:
            def __init__(self, *_args, **_kwargs):
                time.sleep(0.2)
                self.stdout = io.StringIO("{}")
                self.stderr = io.StringIO("")

            def wait(self, timeout=None):
                if timeout is not None:
                    timed_waits.append(timeout)
                    raise subprocess.TimeoutExpired("irrelevant", timeout)
                return 0

            def kill(self):
                pass

        transport = subprocess_transport(["irrelevant"])
        with patch.object(bb.subprocess, "Popen", SlowLaunch):
            with self.assertRaises(BbTransportTimeout):
                transport([], 0.5)
        self.assertLess(
            timed_waits[0],
            0.4,
            f"launch cost was not charged against the budget: wait got {timed_waits[0]:.2f}s",
        )

    def test_stalled_launch_returns_without_a_non_daemon_survivor(self):
        """GH-592: a permanently stalled launch cannot wedge interpreter exit."""
        import llm_collab.bb_client as bb

        launch_started = threading.Event()
        launch_finished = threading.Event()
        release_launch = threading.Event()
        late_killed = threading.Event()
        late_waited = threading.Event()
        launched = []

        class StalledLaunch:
            def __init__(self, *_args, **_kwargs):
                launch_started.set()
                release_launch.wait()
                self.stdout = io.StringIO("{}")
                self.stderr = io.StringIO("")
                launch_finished.set()
                launched.append(self)

            def wait(self, timeout=None):
                if timeout is not None and timeout <= 0:
                    raise subprocess.TimeoutExpired("irrelevant", timeout)
                late_waited.set()
                return 0

            def kill(self):
                late_killed.set()

        transport = subprocess_transport(["irrelevant"])
        before_threads = set(threading.enumerate())
        launch_threads = []
        started = time.monotonic()
        try:
            with patch.object(bb.subprocess, "Popen", StalledLaunch):
                with self.assertRaises(BbTransportTimeout):
                    transport([], 0.2)
            elapsed = time.monotonic() - started
            self.assertLess(
                elapsed,
                0.5,
                f"launch call exceeded its bound while Popen was still stalling: {elapsed:.2f}s",
            )
            self.assertTrue(launch_started.is_set(), "fake Popen never entered its stall")
            self.assertFalse(
                launch_finished.is_set(),
                "transport returned only after the fake Popen stall finished",
            )
            launch_threads = [
                thread
                for thread in threading.enumerate()
                if thread not in before_threads and thread.name == "bb-subprocess-launch"
            ]
            self.assertTrue(launch_threads, "transport created no visible launch thread")
            self.assertTrue(
                all(thread.daemon for thread in launch_threads),
                "stalled launch left a non-daemon launch thread alive",
            )
        finally:
            release_launch.set()
            for thread in launch_threads:
                thread.join(timeout=1.0)
        self.assertTrue(late_killed.is_set(), "late process was not killed")
        self.assertTrue(late_waited.is_set(), "late process was not reaped")
        self.assertTrue(launched[0].stdout.closed, "late stdout pipe was not closed")
        self.assertTrue(launched[0].stderr.closed, "late stderr pipe was not closed")

    def test_the_deadline_bounds_the_call_not_each_wait(self):
        """GH-584: one end-to-end deadline, not one per wait.

        The child closes stdout early and holds stderr open past the bound. With
        a fresh `timeout_seconds` handed to each wait, the stderr wait restarted
        the clock and the call returned after roughly twice the configured
        bound; a third interval was then available to process.wait(). Asserting
        only "it raises" cannot tell the two apart, so this asserts WHEN.
        """
        # stdout must consume MOST of the budget before closing. If it closed
        # immediately the first wait would cost nothing, the stderr wait would
        # fit inside the bound, and the per-wait bug would not manifest at all --
        # the first version of this test passed against the defect for exactly
        # that reason. os.close(1) rather than sys.stdout.close(): the latter
        # did not produce EOF on the read side, so the FIRST wait timed out in
        # both variants and the test could not tell them apart. Measured:
        # 0.41s fixed, 0.78s with the per-wait bound.
        script = (
            "import sys,os,time; "
            "sys.stdout.write('{}'); sys.stdout.flush(); "
            "time.sleep(0.35); os.close(1); "
            "time.sleep(5)"
        )
        transport = subprocess_transport(self._python(script))
        started = time.monotonic()
        with self.assertRaises(BbTransportTimeout):
            transport([], 0.4)
        elapsed = time.monotonic() - started
        # Cumulative: ~0.4s. Per-wait: ~0.35 on stdout, then a FRESH 0.4 on
        # stderr, so 0.75s or more.
        self.assertLess(
            elapsed,
            0.6,
            f"deadline was applied per-wait, not to the call: {elapsed:.2f}s for a 0.4s bound",
        )

    def test_timeout_kills_child_before_waiting_for_readers(self):
        """Partial output plus open pipes must not make the deadline wait for EOF."""
        script = (
            "import sys,time; "
            "sys.stdout.write('partial'); sys.stdout.flush(); "
            "sys.stderr.write('partial'); sys.stderr.flush(); "
            "time.sleep(2)"
        )
        transport = subprocess_transport(self._python(script))
        started = time.monotonic()
        with self.assertRaises(BbTransportTimeout):
            transport([], 0.2)
        self.assertLess(
            time.monotonic() - started,
            1.0,
            "timeout returned only after the child released its reader pipes",
        )

    def test_undecodable_reader_failure_is_typed_at_the_transport_boundary(self):
        script = "import sys; sys.stdout.buffer.write(b'\\xff'); sys.stdout.flush()"
        transport = subprocess_transport(self._python(script))
        with self.assertRaises(BbResponseDecodeError):
            transport([], 30.0)


class BbExecutableFromProjectTest(unittest.TestCase):
    """GH-728: the one resolver seam owns the executable rule — configured
    argv or refusal, never a silent PATH default."""

    def test_configured_executable_is_returned_as_a_copy(self):
        project = {"bb": {"enabled": True, "executable": ["/opt/bb", "--wrapper"]}}
        resolved = bb_executable_from_project(project)
        self.assertEqual(["/opt/bb", "--wrapper"], resolved)
        self.assertIsNot(resolved, project["bb"]["executable"])

    def test_absent_executable_refuses_without_path_fallback(self):
        with self.assertRaisesRegex(BbExecutableRefused, "non-empty list"):
            bb_executable_from_project({"bb": {"enabled": True}})

    def test_missing_or_malformed_bb_block_refuses(self):
        for project in (None, {}, {"bb": "yes"}, {"bb": {"executable": "bb"}}):
            with self.assertRaises(BbExecutableRefused, msg=repr(project)):
                bb_executable_from_project(project)

    def test_refusal_is_a_value_error_for_existing_caller_contracts(self):
        with self.assertRaises(ValueError):
            bb_executable_from_project({"bb": {"executable": []}})


class BbProjectIdFromProjectTest(unittest.TestCase):
    """GH-731: one seam owns raw-match-rejects-padded semantics."""

    def test_raw_value_and_fallback_are_returned_without_normalizing(self):
        self.assertEqual(
            "native-project",
            bb_project_id_from_project(
                {"bb": {"project_id": "native-project"}}, "collab-project"
            ),
        )
        self.assertEqual(
            "collab-project",
            bb_project_id_from_project({"bb": {}}, "collab-project"),
        )

    def test_exact_repo_mapping_is_selected_and_incomplete_shapes_refuse(self):
        project = {
            "bb": {
                "project_id": "legacy-project",
                "project_ids": {"app": "native-app", "docs": "native-docs"},
            }
        }
        self.assertEqual(
            "native-app",
            bb_project_id_from_project(project, "collab-project", "app"),
        )
        self.assertEqual(
            "native-docs",
            bb_project_id_from_project(project, "collab-project", "docs"),
        )
        for malformed, target in (([], "app"), ({"app": "native-app"}, "docs")):
            with self.subTest(malformed=malformed, target=target), self.assertRaises(
                BbProjectIdRefused
            ):
                bb_project_id_from_project(
                    {"bb": {"project_ids": malformed}}, "collab-project", target
                )
        for value in (None, "", 7):
            with self.subTest(value=value), self.assertRaises(BbProjectIdRefused):
                bb_project_id_from_project(
                    {"bb": {"project_ids": {"app": value}}},
                    "collab-project",
                    "app",
                )

    def test_padded_mapped_value_is_rejected_not_normalized(self):
        with self.assertRaises(BbProjectIdRefused) as raised:
            bb_project_id_from_project(
                {"bb": {"project_ids": {"docs": " native-docs "}}},
                "collab-project",
                "docs",
            )
        self.assertEqual(" native-docs ", raised.exception.value)
        self.assertEqual("bb.project_ids['docs']", raised.exception.field)

    def test_padded_value_is_rejected_not_normalized(self):
        with self.assertRaises(BbProjectIdRefused) as raised:
            bb_project_id_from_project(
                {"bb": {"project_id": " native-project "}}, "collab-project"
            )
        self.assertEqual(" native-project ", raised.exception.value)
