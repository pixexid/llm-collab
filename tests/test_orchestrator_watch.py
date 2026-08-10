"""Discriminating tests for the standard orchestrator watchers (GH-727)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import select
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))


def load_module():
    spec = importlib.util.spec_from_file_location(
        "orchestrator_watch", ROOT / "bin" / "orchestrator_watch.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


watch = load_module()
RECORDED_THREAD_LIST = ROOT / "tests" / "fixtures" / "bb" / "thread_list.json"


def config() -> watch.WatcherConfig:
    return watch.WatcherConfig(
        bb_executable=("configured-bb", "--wrapper"),
        bb_project_id="native-project",
        github_repo="owner/repo",
        timeout_seconds=5.0,
    )


def signature(*, state: str = "open", merged: bool = False, head: str = "a" * 40):
    return {"state": state, "merged": merged, "head": head, "timeline": []}


def recorded_thread_list() -> list[dict]:
    return json.loads(RECORDED_THREAD_LIST.read_text(encoding="utf-8"))


class RecordedThreadListTest(unittest.TestCase):
    def test_recorded_live_thread_list_accepts_integer_archived_at(self) -> None:
        payload = recorded_thread_list()
        try:
            rows = watch.thread_rows(payload)
        except watch.ProbeError as error:
            self.fail(f"recorded live bb data must accept integer archivedAt: {error}")
        self.assertEqual(payload, rows)
        self.assertGreater(
            sum(row["archivedAt"] is not None for row in rows),
            0,
            "recorded fixture must contain archived rows",
        )
        self.assertGreater(
            sum(row["archivedAt"] is None and row["status"] == "active" for row in rows),
            0,
            "recorded fixture must contain an active row",
        )
        self.assertTrue(
            all(
                type(row["archivedAt"]) is int
                for row in rows
                if row["archivedAt"] is not None
            ),
            "recorded archivedAt values must be integer timestamps",
        )

    def test_recorded_live_thread_list_completes_both_shared_probes(self) -> None:
        payload = recorded_thread_list()
        self.assertTrue(
            watch.worker_cycle(config(), {}, call=lambda *_: payload, emit=lambda _line: None)
        )

        def call(_executable, argv, _timeout):
            if argv[:2] == ("settings", "version"):
                return {"currentVersion": watch.PINNED_BB_VERSION}
            return payload

        self.assertTrue(
            watch.heartbeat_cycle(
                config(), call=call, enumerate_open=lambda *_, **__: [], emit=lambda _line: None
            )
        )

    def test_integer_archived_at_is_not_counted_as_a_live_worker(self) -> None:
        payload = recorded_thread_list()
        archived = next(row for row in payload if isinstance(row["archivedAt"], int))
        active = next(row for row in payload if row["archivedAt"] is None and row["status"] == "active")
        probe_rows = [{**archived, "archivedAt": 0, "status": "active"}, active]
        messages = []

        def call(_executable, argv, _timeout):
            if argv[:2] == ("settings", "version"):
                return {"currentVersion": watch.PINNED_BB_VERSION}
            return probe_rows

        self.assertTrue(
            watch.heartbeat_cycle(
                config(), call=call, enumerate_open=lambda *_, **__: [], emit=messages.append
            )
        )
        self.assertIn("liveWorkers=1", messages[-1])


class ProbeShapeTest(unittest.TestCase):
    def test_wrong_shape_valid_json_is_not_a_worker_sample_or_marker_refresh(self) -> None:
        for payload in ({}, {"error": "backend unavailable"}, "not a list"):
            with self.subTest(payload=payload), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    "worker-lifecycle",
                    "project-a",
                    "session-a",
                    lambda: watch.worker_cycle(
                        config(), {}, call=lambda *_: payload, emit=lambda _line: None
                    ),
                    emit=lambda _line: None,
                )
            self.assertFalse(
                completed,
                "wrong-shape valid JSON must not be a successful worker sample",
            )
            writer.assert_not_called()

    def test_wrong_shape_valid_json_is_not_a_heartbeat_sample_or_marker_refresh(self) -> None:
        for version_payload in ({}, {"currentVersion": ""}, {"currentVersion": " "}):
            def call(_executable, argv, _timeout):
                return (
                    version_payload
                    if argv[:2] == ("settings", "version")
                    else {"threads": []}
                )

            with self.subTest(payload=version_payload), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    "heartbeat",
                    "project-a",
                    "session-a",
                    lambda: watch.heartbeat_cycle(
                        config(),
                        call=call,
                        enumerate_open=lambda *_, **__: [],
                        emit=lambda _line: None,
                    ),
                    emit=lambda _line: None,
                )
            self.assertFalse(
                completed,
                "wrong-shape valid JSON must not be a successful heartbeat sample",
            )
            writer.assert_not_called()

    def test_wrong_shape_thread_list_is_not_a_heartbeat_sample_or_marker_refresh(self) -> None:
        for thread_payload in ({}, {"error": "backend unavailable"}, "not a list"):
            def call(_executable, argv, _timeout):
                return (
                    {"currentVersion": watch.PINNED_BB_VERSION}
                    if argv[:2] == ("settings", "version")
                    else thread_payload
                )

            with self.subTest(payload=thread_payload), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    "heartbeat",
                    "project-a",
                    "session-a",
                    lambda: watch.heartbeat_cycle(
                        config(),
                        call=call,
                        enumerate_open=lambda *_, **__: [],
                        emit=lambda _line: None,
                    ),
                    emit=lambda _line: None,
                )
            self.assertFalse(
                completed,
                "wrong-shape thread data must not be a successful heartbeat sample",
            )
            writer.assert_not_called()

    def test_failed_probe_does_not_refresh_marker(self) -> None:
        def failed(*_args, **_kwargs):
            raise watch.ProbeError("probe failed")

        with mock.patch.object(watch._watcher_liveness, "write_marker") as writer:
            completed = watch.run_once(
                "worker-lifecycle",
                "project-a",
                "session-a",
                lambda: watch.worker_cycle(config(), {}, call=failed),
                emit=lambda _line: None,
            )
        self.assertFalse(completed, "a failed probe must skip marker refresh")
        writer.assert_not_called()

    def test_incomplete_worker_row_does_not_refresh_marker(self) -> None:
        invalid_rows = (
            {"id": "", "status": "idle"},
            {"id": " ", "status": "idle"},
            {"id": "thread-a", "status": ""},
            {"id": "thread-a", "status": " "},
            {"id": "thread-a", "status": "idle", "archivedAt": ""},
            {"id": "thread-a", "status": "idle", "archivedAt": " "},
            {"id": "thread-a", "status": "idle", "archivedAt": {}},
        )
        for row in invalid_rows:
            with self.subTest(row=row), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    "worker-lifecycle",
                    "project-a",
                    "session-a",
                    lambda: watch.worker_cycle(
                        config(),
                        {},
                        call=lambda *_: {"threads": [row]},
                        emit=lambda _line: None,
                    ),
                    emit=lambda _line: None,
                )
            self.assertFalse(
                completed,
                "an incomplete worker identity row must fail the sample",
            )
            writer.assert_not_called()

    def test_unusable_pr_identity_does_not_persist_or_refresh_marker(self) -> None:
        invalid_signatures = (
            {},
            {"state": "", "merged": False, "head": "a" * 40},
            {"state": "draft", "merged": False, "head": "a" * 40},
            {"state": "open", "merged": False, "head": ""},
            {"state": "closed", "merged": False, "head": "g" * 40},
            {"state": "open", "merged": False, "head": "a" * 39},
        )
        for sample in invalid_signatures:
            state = {}
            with self.subTest(sample=sample), mock.patch.object(
                watch.pr_watch, "snapshot", return_value=(sample, {})
            ), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    "pr-artifacts",
                    "project-a",
                    "session-a",
                    lambda: watch.pr_cycle(
                        config(),
                        state,
                        enumerate_prs=lambda *_, **__: [17],
                        emit=lambda _line: None,
                    ),
                    emit=lambda _line: None,
                )
            self.assertFalse(
                completed,
                "unsupported PR state or invalid head must fail the sample",
            )
            self.assertEqual({}, state, "an unusable PR identity must not be persisted")
            writer.assert_not_called()


class EventDeliveryTest(unittest.TestCase):
    def test_property_a_undelivered_event_fails_without_advancing_baseline_or_marker(
        self,
    ) -> None:
        class ClosedMonitor:
            def __init__(self) -> None:
                self.attempts = []
                self.delivered = []

            def write(self, text):
                self.attempts.append(text)
                raise BrokenPipeError("Monitor closed stdout")

            def flush(self):
                raise BrokenPipeError("Monitor closed stdout")

        baseline = {"statuses": {"thread-a": "active"}}
        state = json.loads(json.dumps(baseline))
        monitor = ClosedMonitor()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker-lifecycle.json"
            watch.save_state(path, baseline)

            def check() -> bool:
                complete = watch.worker_cycle(
                    config(),
                    state["statuses"],
                    call=lambda *_: {
                        "threads": [{"id": "thread-a", "status": "idle"}]
                    },
                )
                if complete:
                    watch.save_state(path, state)
                return complete

            with mock.patch.object(watch.sys, "stdout", monitor), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    "worker-lifecycle", "project-a", "session-a", check
                )
            persisted = watch.load_state(path, {})

        with self.subTest(property="event delivery"):
            self.assertTrue(monitor.attempts, "the event write must be attempted")
            self.assertEqual([], monitor.delivered, "the event must not be delivered")
        with self.subTest(property="cycle result"):
            self.assertFalse(
                completed, "an undelivered event must fail the complete cycle"
            )
        with self.subTest(property="baseline preservation"):
            self.assertEqual(
                (baseline, baseline),
                (state, persisted),
                "an undelivered event must not advance in-memory or persisted baseline",
            )
        with self.subTest(property="marker preservation"):
            self.assertEqual(
                [],
                writer.call_args_list,
                "an undelivered event must not refresh the liveness marker",
            )

    def test_failure_event_is_flushed_immediately_to_non_tty_stdout(self) -> None:
        read_fd, write_fd = os.pipe()
        stdout = os.fdopen(write_fd, "w", encoding="utf-8")
        try:
            self.assertFalse(stdout.isatty(), "the test must exercise piped stdout")
            self.assertFalse(
                stdout.line_buffering,
                "the pipe must start block-buffered so an unflushed print is invisible",
            )

            def failed():
                raise watch.ProbeError("probe failed")

            with mock.patch.object(watch.sys, "stdout", stdout):
                self.assertFalse(
                    watch.run_once(
                        "heartbeat", "project-a", "session-a", failed
                    )
                )
                readable, _, _ = select.select([read_fd], [], [], 0)
                self.assertEqual(
                    [read_fd],
                    readable,
                    "watcher failure events must be flushed immediately to non-TTY stdout",
                )
                output = os.read(read_fd, 4096).decode("utf-8")
            self.assertIn("HEARTBEAT CHECK FAILED — probe failed", output)
        finally:
            stdout.close()
            os.close(read_fd)

    def test_every_production_emit_default_uses_the_flushing_emitter(self) -> None:
        for function in (
            watch.worker_cycle,
            watch.pr_cycle,
            watch.heartbeat_cycle,
            watch.run_once,
        ):
            with self.subTest(function=function.__name__):
                self.assertIs(
                    function.__kwdefaults__["emit"],
                    watch.emit_event,
                    f"{function.__name__} must use the shared flushing emitter",
                )

    def test_output_failure_does_not_escape_the_persistent_loop_path(self) -> None:
        class BrokenStdout:
            def write(self, _text):
                raise OSError("reader closed")

            def flush(self):
                raise OSError("reader closed")

        def failed():
            raise watch.ProbeError("probe failed")

        survived = True
        with mock.patch.object(watch.sys, "stdout", BrokenStdout()):
            try:
                completed = watch.run_once(
                    "heartbeat",
                    "project-a",
                    "session-a",
                    failed,
                )
            except Exception:
                survived = False
                completed = True
        self.assertTrue(
            survived,
            "a Monitor output failure must not terminate the persistent watcher",
        )
        self.assertFalse(
            completed,
            "surviving an output failure must not turn a failed cycle into success",
        )


class StatePersistenceTest(unittest.TestCase):
    def test_state_within_bound_round_trips_through_save_and_load(self) -> None:
        state = {"statuses": {"thread-a": "idle", "thread-b": "active"}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "MAX_STATE_BYTES", 128
        ):
            path = Path(directory) / "worker-lifecycle.json"
            watch.save_state(path, state)
            self.assertEqual(
                state,
                watch.load_state(path, {}),
                "state within the shared byte bound must round-trip",
            )

    def test_oversized_state_fails_cycle_without_replacement_or_marker_refresh(
        self,
    ) -> None:
        state = {"signatures": {"1": "x" * 100}, "terminal_left": {}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pr-artifacts.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            previous = path.read_bytes()

            def save() -> bool:
                watch.save_state(path, state)
                return True

            with mock.patch.object(
                watch, "MAX_STATE_BYTES", 64
            ), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    "pr-artifacts",
                    "project-a",
                    "session-a",
                    save,
                    emit=lambda _line: None,
                )
            self.assertFalse(
                completed,
                "state exceeding MAX_STATE_BYTES must fail the cycle before marker refresh",
            )
            self.assertEqual(
                previous,
                path.read_bytes(),
                "oversized state must not replace the last readable state",
            )
            writer.assert_not_called()


class EnumerationTest(unittest.TestCase):
    def test_property_b_over_bound_thread_enumeration_fails_visibly_without_marker(
        self,
    ) -> None:
        limit = 256
        thread_payload = json.dumps(
            {
                "threads": [
                    {"id": f"thread-{number}", "status": "idle"}
                    for number in range(30)
                ]
            }
        )
        self.assertGreater(
            len(thread_payload),
            limit,
            "the fixture must independently exceed the configured response bound",
        )
        version_payload = json.dumps(
            {"currentVersion": watch.PINNED_BB_VERSION}
        )
        script = (
            "import sys\n"
            f"version = {version_payload!r}\n"
            f"threads = {thread_payload!r}\n"
            "print(version if sys.argv[1:3] == ['settings', 'version'] else threads)\n"
        )
        bounded_config = watch.WatcherConfig(
            bb_executable=(sys.executable, "-c", script),
            bb_project_id="native-project",
            github_repo="owner/repo",
            timeout_seconds=5.0,
        )

        for mode in ("worker-lifecycle", "heartbeat"):
            messages = []
            with self.subTest(mode=mode), mock.patch.object(
                watch, "THREAD_ENUM_MAX_RESPONSE_CHARS", limit
            ), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                if mode == "worker-lifecycle":
                    check = lambda: watch.worker_cycle(
                        bounded_config, {}, emit=messages.append
                    )
                else:
                    check = lambda: watch.heartbeat_cycle(
                        bounded_config,
                        enumerate_open=lambda *_, **__: [],
                        emit=messages.append,
                    )
                completed = watch.run_once(
                    mode,
                    "project-a",
                    "session-a",
                    check,
                    emit=messages.append,
                )
                self.assertFalse(
                    completed,
                    "thread enumeration exceeding the response size bound must fail the cycle",
                )
                self.assertTrue(
                    any(
                        "FAILED" in message and str(limit) in message
                        for message in messages
                    ),
                    "thread enumeration size refusal must be visible to the Monitor",
                )
                self.assertEqual(
                    [],
                    writer.call_args_list,
                    "over-bound thread enumeration must not refresh the marker",
                )

    def test_gh_pr_list_failure_skips_cycle_without_marker_refresh(self) -> None:
        state = {"signatures": {}, "terminal_left": {}}

        def failed(*_args):
            raise watch.ProbeError("gh failed")

        with mock.patch.object(watch._watcher_liveness, "write_marker") as writer:
            completed = watch.run_once(
                "pr-artifacts",
                "project-a",
                "session-a",
                lambda: watch.pr_cycle(config(), state, enumerate_prs=failed),
                emit=lambda _line: None,
            )
        self.assertFalse(completed, "gh pr list failure must skip the cycle")
        writer.assert_not_called()

    def test_enumeration_over_cap_is_detected_and_does_not_refresh_marker(self) -> None:
        payload = [{"number": 11}, {"number": 12}, {"number": 13}]

        def over_cap(_kind, _repo, _cap, _deadline, **_kwargs):
            return watch.open_numbers(
                "pr", "owner/repo", 2, call=lambda *_: payload
            )

        with mock.patch.object(watch._watcher_liveness, "write_marker") as writer:
            completed = watch.run_once(
                "pr-artifacts",
                "project-a",
                "session-a",
                lambda: watch.pr_cycle(
                    config(),
                    {"signatures": {}, "terminal_left": {}},
                    enumerate_prs=over_cap,
                    signature=lambda *_: signature(),
                ),
                emit=lambda _line: None,
            )
        self.assertFalse(
            completed,
            "over-cap enumeration must be detected and skip the cycle",
        )
        writer.assert_not_called()

    def test_gh_enumeration_requests_one_past_the_cap(self) -> None:
        seen = []

        def call(_executable, argv, _timeout):
            seen.append(argv)
            return [{"number": 1}, {"number": 2}]

        self.assertEqual([1, 2], watch.open_numbers("issue", "owner/repo", 2, call=call))
        self.assertIn("3", seen[0])

    def test_heartbeat_count_over_cap_skips_marker_refresh(self) -> None:
        def call(_executable, argv, _timeout):
            if argv[:2] == ("settings", "version"):
                return {"currentVersion": watch.PINNED_BB_VERSION}
            return {"threads": []}

        payload = [{"number": 21}, {"number": 22}, {"number": 23}]
        for capped_kind in ("pr", "issue"):
            def enumerate_open(kind, _repo, _cap, _deadline, **_kwargs):
                if kind == capped_kind:
                    return watch.open_numbers(kind, "owner/repo", 2, call=lambda *_: payload)
                return []

            with self.subTest(kind=capped_kind), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    "heartbeat",
                    "project-a",
                    "session-a",
                    lambda: watch.heartbeat_cycle(
                        config(),
                        call=call,
                        enumerate_open=enumerate_open,
                        emit=lambda _line: None,
                    ),
                )
                self.assertFalse(
                    completed,
                    f"an over-cap heartbeat {capped_kind} count must skip marker refresh",
                )
                writer.assert_not_called()


class PrTerminalWindowTest(unittest.TestCase):
    def test_first_open_pr_is_armed_from_empty_state(self) -> None:
        state = {}
        watch.pr_cycle(
            config(),
            state,
            enumerate_prs=lambda *_, **__: [3],
            signature=lambda *_: signature(),
            emit=lambda _line: None,
        )
        self.assertIn("3", state["signatures"])

    def test_reopen_resets_terminal_countdown(self) -> None:
        original = json.dumps(signature(state="closed"), sort_keys=True, separators=(",", ":"))
        state = {"signatures": {"7": original}, "terminal_left": {"7": 1}}
        samples = iter(
            [signature(state="open"), *[signature(state="closed") for _ in range(29)]]
        )
        for cycle in range(30):
            watch.pr_cycle(
                config(),
                state,
                enumerate_prs=(lambda *_, **__: [7])
                if cycle == 0
                else (lambda *_, **__: []),
                signature=lambda *_: next(samples),
                emit=lambda _line: None,
            )
        self.assertIn(
            "7",
            state["signatures"],
            "a reopened PR must receive a fresh full terminal countdown",
        )
        self.assertEqual(1, state["terminal_left"]["7"])

    def test_merged_pr_is_polled_for_full_window_before_retiring(self) -> None:
        encoded = json.dumps(signature(), sort_keys=True, separators=(",", ":"))
        state = {"signatures": {"9": encoded}, "terminal_left": {}}
        polls = 0

        def merged(*_args):
            nonlocal polls
            polls += 1
            return signature(state="closed", merged=True)

        for _ in range(29):
            watch.pr_cycle(
                config(),
                state,
                enumerate_prs=lambda *_, **__: [],
                signature=merged,
                emit=lambda _line: None,
            )
        self.assertIn(
            "9",
            state["signatures"],
            "a merged PR must remain armed through the full post-merge window",
        )
        watch.pr_cycle(
            config(),
            state,
            enumerate_prs=lambda *_, **__: [],
            signature=merged,
            emit=lambda _line: None,
        )
        self.assertEqual(30, polls)
        self.assertNotIn("9", state["signatures"])

    def test_failed_per_pr_poll_does_not_commit_partial_state_or_refresh_marker(self) -> None:
        encoded = json.dumps(signature(), sort_keys=True, separators=(",", ":"))
        state = {"signatures": {"1": encoded, "2": encoded}, "terminal_left": {}}
        before = json.loads(json.dumps(state))

        def sample(_repo, number, _deadline):
            if number == 2:
                raise watch.ProbeError("poll failed")
            return signature(head="b" * 40)

        with mock.patch.object(watch._watcher_liveness, "write_marker") as writer:
            completed = watch.run_once(
                "pr-artifacts",
                "project-a",
                "session-a",
                lambda: watch.pr_cycle(
                    config(),
                    state,
                    enumerate_prs=lambda *_, **__: [],
                    signature=sample,
                ),
                emit=lambda _line: None,
            )
        self.assertFalse(completed, "a failed per-PR poll must make the cycle incomplete")
        self.assertEqual(before, state, "a failed cycle must not commit partial PR state")
        writer.assert_not_called()


class MarkerRefreshTest(unittest.TestCase):
    def test_pr_signature_forwards_the_cycle_deadline_to_snapshot(self) -> None:
        sample = signature()
        with mock.patch.object(
            watch.pr_watch, "snapshot", return_value=(sample, {})
        ) as snapshot:
            self.assertEqual(sample, watch.pr_signature("owner/repo", 17, 123.0))
        snapshot.assert_called_once_with("owner/repo", "17", 123.0)

    def test_heartbeat_marker_stays_fresh_during_nontrivial_next_cycle_checks(self) -> None:
        now = 0.0
        last_refresh = 0.0  # the preceding completed cycle's run_once marker

        def sleep(seconds):
            nonlocal now
            now += seconds

        def write_marker(_project, _name, _session):
            nonlocal last_refresh
            last_refresh = now

        with mock.patch.object(
            watch._watcher_liveness, "write_marker", side_effect=write_marker
        ):
            watch.heartbeat_wait("project-a", "session-a", True, sleep=sleep)
        sleep(watch.WATCHER_CYCLE_DEADLINE_SECONDS)
        age = now - last_refresh
        self.assertLessEqual(
            age,
            watch._watcher_liveness.WATCHER_MARKER_STALE_AFTER_SECONDS,
            "heartbeat marker must stay fresh during non-trivial cycle checks",
        )

    def test_failed_heartbeat_cycle_does_not_refresh_during_report_wait(self) -> None:
        with mock.patch.object(watch._watcher_liveness, "write_marker") as writer:
            watch.heartbeat_wait(
                "project-a", "session-a", False, sleep=lambda _seconds: None
            )
        writer.assert_not_called()

    def test_intermediate_marker_failure_reports_and_wait_continues(self) -> None:
        sleeps = []
        messages = []
        survived = True
        with mock.patch.object(
            watch._watcher_liveness,
            "write_marker",
            side_effect=[OSError("mount hiccup"), *([None] * 8)],
        ) as writer:
            try:
                watch.heartbeat_wait(
                    "project-a",
                    "session-a",
                    True,
                    sleep=sleeps.append,
                    emit=messages.append,
                )
            except OSError:
                survived = False
        self.assertTrue(
            survived,
            "an intermediate marker failure must not terminate the heartbeat watcher",
        )
        self.assertEqual(
            watch.HEARTBEAT_REPORT_SECONDS,
            sum(sleeps),
            "the report wait must continue after an intermediate marker failure",
        )
        self.assertEqual(9, writer.call_count, "later marker refreshes must still run")
        self.assertEqual(
            ["HEARTBEAT MARKER WRITE FAILED — mount hiccup"],
            messages,
        )

    def test_pr_cycle_uses_one_cumulative_deadline_and_does_not_refresh_on_exhaustion(
        self,
    ) -> None:
        now = 0.0
        seen_deadlines = []

        def monotonic():
            return now

        def enumerate_prs(_kind, _repo, _cap, deadline, **_kwargs):
            self.assertEqual(300.0, deadline)
            return [1, 2, 3]

        def sample(_repo, _number, deadline):
            nonlocal now
            seen_deadlines.append(deadline)
            now += 120.0  # each item fits 300s; all three together do not
            return signature()

        with mock.patch.object(watch._watcher_liveness, "write_marker") as writer:
            completed = watch.run_once(
                "pr-artifacts",
                "project-a",
                "session-a",
                lambda: watch.pr_cycle(
                    config(),
                    {"signatures": {}, "terminal_left": {}},
                    enumerate_prs=enumerate_prs,
                    signature=sample,
                    monotonic=monotonic,
                    emit=lambda _line: None,
                ),
                emit=lambda _line: None,
            )
        self.assertFalse(
            completed,
            "per-item work that collectively exceeds the cumulative deadline must fail the cycle",
        )
        self.assertEqual(
            [300.0, 300.0, 300.0],
            seen_deadlines,
            "one cumulative deadline must be shared across every PR",
        )
        self.assertGreater(
            now,
            watch.WATCHER_CYCLE_DEADLINE_SECONDS,
            "the test must exceed the total budget while each item stays below it",
        )
        writer.assert_not_called()

    def test_every_watcher_refreshes_only_after_a_completed_cycle(self) -> None:
        for name in watch._watcher_liveness.WATCHER_NAMES:
            with self.subTest(name=name), mock.patch.object(
                watch._watcher_liveness, "write_marker"
            ) as writer:
                completed = watch.run_once(
                    name,
                    "project-a",
                    "session-a",
                    lambda: False,
                    emit=lambda _line: None,
                )
                self.assertFalse(
                    completed,
                    f"failed cycles must not refresh the {name} marker",
                )
                writer.assert_not_called()

    def test_every_watcher_writes_through_existing_project_scoped_writer(self) -> None:
        with mock.patch.object(watch._watcher_liveness, "write_marker") as writer:
            for name in watch._watcher_liveness.WATCHER_NAMES:
                self.assertTrue(
                    watch.run_once(
                        name,
                        "project-a",
                        "session-a",
                        lambda: True,
                        emit=lambda _line: None,
                    )
                )
        self.assertEqual(
            [
                mock.call("project-a", "worker-lifecycle", "session-a"),
                mock.call("project-a", "pr-artifacts", "session-a"),
                mock.call("project-a", "heartbeat", "session-a"),
            ],
            writer.call_args_list,
        )


class ProjectConfigTest(unittest.TestCase):
    def test_padded_bb_project_id_refuses_without_refreshing_liveness_marker(
        self,
    ) -> None:
        project = {
            "bb": {
                "project_id": " native-project ",
                "executable": ["configured-bb"],
                "timeout_seconds": 5,
            },
            "github": {"repo": "owner/repo"},
        }
        stderr = io.StringIO()
        with mock.patch.object(watch, "get_project", return_value=project), mock.patch.object(
            watch._watcher_liveness, "write_marker"
        ) as writer, mock.patch.object(
            sys,
            "argv",
            [
                "orchestrator_watch.py",
                "worker-lifecycle",
                "--project",
                "project-a",
                "--session",
                "session-a",
                "--state-dir",
                "/unused",
            ],
        ), contextlib.redirect_stderr(stderr):
            self.assertEqual(
                1,
                watch.main(),
                "a padded bb.project_id must fail the watcher cycle",
            )
        writer.assert_not_called()
        self.assertEqual(
            "REFUSED: bb.project_id ' native-project ' has surrounding whitespace; "
            "refusing (match raw, reject padded)",
            stderr.getvalue().strip(),
        )

    def test_property_c_missing_bb_executable_refuses_without_path_fallback(
        self,
    ) -> None:
        project = {
            "bb": {"project_id": "native-project", "timeout_seconds": 5},
            "github": {"repo": "owner/repo"},
        }
        with mock.patch.object(watch, "get_project", return_value=project):
            with self.assertRaisesRegex(
                watch.ProbeError, "bb.executable must be a non-empty list"
            ):
                watch.project_config("project-a", "worker-lifecycle")


if __name__ == "__main__":
    unittest.main()
