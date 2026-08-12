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

from llm_collab.bb_client import BbTransportTimeout

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
        bb_executable=(sys.executable, "-c", "print('accepted')"),
        bb_project_ids=("native-project",),
        github_repo="owner/repo",
        timeout_seconds=5.0,
    )


def multi_project_config() -> watch.WatcherConfig:
    return watch.WatcherConfig(
        bb_executable=(sys.executable, "-c", "print('accepted')"),
        bb_project_ids=("native-app", "native-docs"),
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

    def test_recorded_live_thread_list_completes_heartbeat_probe(self) -> None:
        payload = recorded_thread_list()
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


class MultiProjectAggregationTest(unittest.TestCase):
    def test_heartbeat_aggregates_every_native_project(self) -> None:
        calls = []
        payloads = {
            "native-app": {"threads": [{"id": "app-worker", "status": "active"}]},
            "native-docs": {"threads": [{"id": "docs-worker", "status": "starting"}]},
        }

        def call(_executable, argv, _timeout):
            calls.append(tuple(argv))
            if argv[:2] == ("settings", "version"):
                return {"currentVersion": watch.PINNED_BB_VERSION}
            return payloads[argv[3]]

        messages = []
        self.assertTrue(
            watch.heartbeat_cycle(
                multi_project_config(),
                call=call,
                enumerate_open=lambda *_, **__: [],
                emit=messages.append,
            )
        )
        self.assertIn("liveWorkers=2", messages[-1])
        thread_calls = [argv for argv in calls if argv[:2] == ("thread", "list")]
        self.assertEqual(
            ["native-app", "native-docs"],
            [argv[3] for argv in thread_calls],
        )
        self.assertTrue(all("--include-hidden" not in argv for argv in thread_calls))

    def test_one_native_project_failure_rejects_whole_heartbeat_aggregate(self) -> None:
        calls = []
        messages = []

        def call(_executable, argv, _timeout):
            calls.append(tuple(argv))
            if argv[:2] == ("settings", "version"):
                return {"currentVersion": watch.PINNED_BB_VERSION}
            if argv[3] == "native-app":
                return {"threads": [{"id": "app-worker", "status": "active"}]}
            raise watch.ProbeError("docs project unavailable")

        with mock.patch.object(watch._watcher_liveness, "write_marker") as marker:
            completed = watch.run_once(
                "heartbeat",
                "project-a",
                "session-a",
                lambda: watch.heartbeat_cycle(
                    multi_project_config(),
                    call=call,
                    enumerate_open=lambda *_, **__: [],
                    emit=messages.append,
                ),
                emit=messages.append,
            )
        self.assertFalse(completed)
        self.assertEqual(
            ["native-app", "native-docs"],
            [argv[3] for argv in calls if argv[:2] == ("thread", "list")],
        )
        self.assertTrue(any("liveWorkers=?" in line for line in messages))
        marker.assert_not_called()


class ProbeShapeTest(unittest.TestCase):
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
                "pr-artifacts",
                "project-a",
                "session-a",
                failed,
                emit=lambda _line: None,
            )
        self.assertFalse(completed, "a failed probe must skip marker refresh")
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


class TlsForensicCaptureTest(unittest.TestCase):
    ERROR = watch.ProbeError(
        'Get "https://api.github.com/repos": tls: failed to verify certificate: '
        "x509: certificate signed by unknown authority"
    )
    RAW_SECTIONS = "\n\n".join(
        (
            "=== OPENSSL S_CLIENT ===\npresented certificate bytes",
            "=== SYSTEM DNS (A AND AAAA) ===\nsystem resolver bytes",
            "=== PUBLIC DNS 1.1.1.1 (A AND AAAA) ===\npublic resolver bytes",
            "=== PROXY ENVIRONMENT ===\n(none set)",
        )
    )

    def records_for(self, directory: str) -> list[Path]:
        return list((Path(directory) / "tls-forensics").glob("*.txt"))

    def test_certificate_failure_writes_raw_sections(self) -> None:
        messages = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ) as state_dir, mock.patch.object(
            watch._watcher_liveness, "write_marker"
        ) as marker:
            completed = watch.run_once(
                "heartbeat",
                "project-a",
                "session-a",
                lambda: (_ for _ in ()).throw(self.ERROR),
                emit=messages.append,
                tls_fetcher=lambda _host, _timeout: self.RAW_SECTIONS,
            )
            records = self.records_for(directory)
            self.assertEqual(1, len(records))
            record = records[0].read_text(encoding="utf-8")

        self.assertFalse(completed)
        self.assertIn("UTC timestamp: ", record)
        self.assertIn("Watcher: heartbeat", record)
        self.assertIn(f"Error: {self.ERROR}", record)
        self.assertIn("Endpoint host: api.github.com", record)
        for section in (
            "OPENSSL S_CLIENT",
            "SYSTEM DNS (A AND AAAA)",
            "PUBLIC DNS 1.1.1.1 (A AND AAAA)",
            "PROXY ENVIRONMENT",
        ):
            self.assertIn(f"=== {section} ===", record)
        self.assertEqual([mock.call("project-a")], state_dir.call_args_list)
        marker.assert_not_called()
        self.assertEqual([f"HEARTBEAT CHECK FAILED — {self.ERROR}"], messages)

    def test_persistence_failure_is_emitted_without_changing_cycle_result(self) -> None:
        messages = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ), mock.patch.object(
            watch, "write_file_durably", side_effect=OSError("disk full")
        ), mock.patch.object(watch._watcher_liveness, "write_marker") as marker:
            completed = watch.run_once(
                "pr-artifacts",
                "project-a",
                "session-a",
                lambda: (_ for _ in ()).throw(self.ERROR),
                emit=messages.append,
                tls_fetcher=lambda _endpoint, _timeout: self.RAW_SECTIONS,
            )

        self.assertFalse(completed)
        self.assertEqual(
            [
                "PR-ARTIFACTS TLS FORENSIC CAPTURE FAILED — disk full",
                f"PR-ARTIFACTS CHECK FAILED — {self.ERROR}",
            ],
            messages,
        )
        marker.assert_not_called()

    def test_explicit_endpoint_port_is_retained_and_default_is_443(self) -> None:
        class Result:
            exit_code = 0
            stdout = "raw command output\n"
            stderr = ""

        def transport(_executable, *, max_response_chars):
            self.assertGreater(max_response_chars, 0)
            return lambda _argv, _timeout: Result()

        cases = (
            ('request to "https://bb.example:8443/status" failed: TLS error', 8443),
            ('request to "https://bb.example/status" failed: TLS error', 443),
        )
        with mock.patch.object(
            watch.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"
        ), mock.patch.object(watch, "subprocess_transport", side_effect=transport):
            for error_text, expected_port in cases:
                with self.subTest(port=expected_port):
                    endpoint = watch.endpoint_host_from_error(RuntimeError(error_text))
                    self.assertEqual(("bb.example", expected_port), endpoint)
                    output = watch.fetch_tls_evidence(endpoint, 1.0)
                    self.assertIn(
                        f"openssl s_client -connect bb.example:{expected_port} ",
                        output,
                    )
                    self.assertIn("-servername bb.example", output)

    def test_shared_output_budget_fails_each_remaining_section_visibly(self) -> None:
        class Result:
            exit_code = 0
            stdout = "ab"
            stderr = "cd"

        limits = []

        def transport(_executable, *, max_response_chars):
            limits.append(max_response_chars)
            return lambda _argv, _timeout: Result()

        with mock.patch.object(
            watch, "TLS_CAPTURE_MAX_RESPONSE_CHARS", 4
        ), mock.patch.object(
            watch.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"
        ), mock.patch.object(watch, "subprocess_transport", side_effect=transport):
            output = watch.fetch_tls_evidence(("bb.example", 443), 1.0)

        self.assertEqual([2], limits)
        self.assertIn("=== OPENSSL S_CLIENT ===", output)
        for section in (
            "SYSTEM DNS (A AND AAAA)",
            "PUBLIC DNS 1.1.1.1 (A AND AAAA)",
        ):
            self.assertIn(
                f"=== {section} ===", output
            )
        self.assertEqual(
            2,
            output.count("FAILED: TLS forensic output budget exhausted"),
        )

    def test_overflowing_probe_exhausts_the_shared_budget(self) -> None:
        """GH-757: a probe that trips BbResponseTooLarge spends the whole shared
        budget; later probes must fail visibly, not run against an unshrunk
        remainder."""
        calls = []

        def transport(_executable, *, max_response_chars):
            def run(_argv, _timeout):
                calls.append(max_response_chars)
                raise watch.BbResponseTooLarge(
                    "native stream exceeded 2 chars while reading"
                )

            return run

        with mock.patch.object(
            watch, "TLS_CAPTURE_MAX_RESPONSE_CHARS", 4
        ), mock.patch.object(
            watch.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"
        ), mock.patch.object(watch, "subprocess_transport", side_effect=transport):
            output = watch.fetch_tls_evidence(("bb.example", 443), 1.0)

        self.assertEqual(
            [2],
            calls,
            "the overflowing probe must exhaust the shared budget: a second "
            "transport call means a later probe ran against an unshrunk remainder",
        )
        for section in (
            "OPENSSL S_CLIENT",
            "SYSTEM DNS (A AND AAAA)",
            "PUBLIC DNS 1.1.1.1 (A AND AAAA)",
        ):
            self.assertIn(f"=== {section} ===", output)
        self.assertEqual(
            3,
            output.count("FAILED: TLS forensic output budget exhausted"),
        )

    def test_fetch_timeout_writes_every_section_as_failed(self) -> None:
        def timed_out(_host, _timeout):
            raise TimeoutError("openssl chain fetch timed out")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ):
            watch.capture_tls_failure(
                "pr-artifacts", "project-a", self.ERROR, 1.0, fetcher=timed_out
            )
            records = self.records_for(directory)
            self.assertEqual(1, len(records))
            record = records[0].read_text(encoding="utf-8")

        for section in (
            "OPENSSL S_CLIENT",
            "SYSTEM DNS (A AND AAAA)",
            "PUBLIC DNS 1.1.1.1 (A AND AAAA)",
        ):
            self.assertIn(f"=== {section} ===", record)
        self.assertEqual(3, record.count("FAILED: openssl chain fetch timed out"))
        self.assertIn("=== PROXY ENVIRONMENT ===", record)

    def test_missing_host_writes_visible_absence_without_network(self) -> None:
        error = watch.ProbeError("TLS handshake failed before endpoint selection")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ):
            watch.capture_tls_failure("pr-artifacts", "project-a", error, 1.0)
            records = self.records_for(directory)
            self.assertEqual(1, len(records))
            record = records[0].read_text(encoding="utf-8")

        self.assertIn("Endpoint host: could not be determined", record)
        self.assertEqual(3, record.count("FAILED: endpoint host could not be determined"))
        self.assertIn("=== PROXY ENVIRONMENT ===", record)

    def test_node_style_verification_wording_also_triggers_capture(self) -> None:
        error = watch.ProbeError(
            'request to "https://api.github.com/graphql" failed: '
            "UNABLE_TO_VERIFY_LEAF_SIGNATURE"
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ):
            self.assertFalse(
                watch.run_once(
                    "pr-artifacts",
                    "project-a",
                    "session-a",
                    lambda: (_ for _ in ()).throw(error),
                    emit=lambda _line: None,
                    tls_fetcher=lambda _host, _timeout: self.RAW_SECTIONS,
                )
            )
            records = self.records_for(directory)
            self.assertEqual(1, len(records))
            record = records[0].read_text(encoding="utf-8")

        self.assertIn(f"Error: {error}", record)

    def test_heartbeat_captures_certificate_failure_after_unrelated_failure(self) -> None:
        messages = []
        unrelated = watch.ProbeError("BB worker endpoint timed out")

        def call(_executable, argv, _timeout):
            if argv[:2] == ("settings", "version"):
                return {"currentVersion": watch.PINNED_BB_VERSION}
            raise unrelated

        def enumerate_open(kind, *_, **__):
            if kind == "pr":
                raise self.ERROR
            return []

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ), mock.patch.object(watch._watcher_liveness, "write_marker") as marker:
            completed = watch.run_once(
                "heartbeat",
                "project-a",
                "session-a",
                lambda: watch.heartbeat_cycle(
                    config(),
                    call=call,
                    enumerate_open=enumerate_open,
                    emit=messages.append,
                ),
                emit=messages.append,
                tls_fetcher=lambda _host, _timeout: self.RAW_SECTIONS,
            )
            records = self.records_for(directory)
            self.assertEqual(1, len(records))
            record = records[0].read_text(encoding="utf-8")

        self.assertFalse(completed)
        self.assertIn("Cycle failure count: 2", record)
        self.assertIn("Captured failure: first only", record)
        self.assertIn(f"Error: {self.ERROR}", record)
        self.assertNotIn(f"Error: {unrelated}", record)
        self.assertTrue(messages[0].startswith("HEARTBEAT WORKER PROBE FAILED"))
        self.assertTrue(messages[1].startswith("HEARTBEAT PR ENUMERATION FAILED"))
        self.assertTrue(messages[2].startswith("HEARTBEAT openPRs="))
        self.assertFalse(any("HEARTBEAT CHECK FAILED" in line for line in messages))
        marker.assert_not_called()

    def test_heartbeat_without_certificate_failure_writes_no_record(self) -> None:
        def call(_executable, argv, _timeout):
            if argv[:2] == ("settings", "version"):
                return {"currentVersion": watch.PINNED_BB_VERSION}
            raise watch.ProbeError("BB worker endpoint timed out")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ), mock.patch.object(watch._watcher_liveness, "write_marker") as marker:
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
                tls_fetcher=lambda _host, _timeout: self.RAW_SECTIONS,
            )
            records = self.records_for(directory)

        self.assertFalse(completed)
        self.assertEqual([], records)
        marker.assert_not_called()

    def test_mtime_floor_skips_inside_window_and_allows_after_it(self) -> None:
        now = [100.0]
        fetches = []

        def fetch(_host, _timeout):
            fetches.append(now[0])
            return self.RAW_SECTIONS

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ):
            first = watch.capture_tls_failure(
                "pr-artifacts",
                "project-a",
                self.ERROR,
                1.0,
                fetcher=fetch,
                wall_time=lambda: now[0],
            )
            now[0] += watch.TLS_CAPTURE_MTIME_FLOOR_SECONDS - 1
            skipped = watch.capture_tls_failure(
                "pr-artifacts",
                "project-a",
                self.ERROR,
                1.0,
                fetcher=fetch,
                wall_time=lambda: now[0],
            )
            now[0] += 2
            after = watch.capture_tls_failure(
                "pr-artifacts",
                "project-a",
                self.ERROR,
                1.0,
                fetcher=fetch,
                wall_time=lambda: now[0],
            )
            record_count = len(self.records_for(directory))

        self.assertIsNotNone(first)
        self.assertIsNone(skipped)
        self.assertIsNotNone(after)
        self.assertEqual(2, record_count)
        self.assertEqual([100.0, 131.0], fetches)

    def test_capture_timeout_uses_only_the_cycle_time_remaining(self) -> None:
        now = [100.0]
        seen_timeouts = []

        def monotonic() -> float:
            return now[0]

        def fail_near_deadline() -> bool:
            now[0] += watch.WATCHER_CYCLE_DEADLINE_SECONDS - 0.25
            raise self.ERROR

        def fetch(_host: str | None, timeout: float) -> str:
            seen_timeouts.append(timeout)
            return self.RAW_SECTIONS

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ):
            self.assertFalse(
                watch.run_once(
                    "pr-artifacts",
                    "project-a",
                    "session-a",
                    fail_near_deadline,
                    emit=lambda _line: None,
                    tls_fetcher=fetch,
                    monotonic=monotonic,
                )
            )
            self.assertEqual(1, len(self.records_for(directory)))

        self.assertEqual([0.25], seen_timeouts)

    def test_shutdown_during_capture_emits_cycle_failure_then_propagates(self) -> None:
        messages = []

        def interrupt(_host, _timeout):
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "project_state_dir", return_value=Path(directory)
        ), self.assertRaises(KeyboardInterrupt):
            watch.run_once(
                "heartbeat",
                "project-a",
                "session-a",
                lambda: (_ for _ in ()).throw(self.ERROR),
                emit=messages.append,
                tls_fetcher=interrupt,
            )
        self.assertEqual([f"HEARTBEAT CHECK FAILED — {self.ERROR}"], messages)

    def test_proxy_dump_records_names_only(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"HTTPS_PROXY": "https://user:secret@proxy.example:8443", "NO_PROXY": ""},
            clear=True,
        ):
            section = watch.proxy_environment_section()

        self.assertIn("HTTPS_PROXY: set (value redacted)", section)
        self.assertIn("NO_PROXY: empty", section)
        self.assertNotIn("user", section)
        self.assertNotIn("secret", section)


class StatePersistenceTest(unittest.TestCase):
    def test_state_within_bound_round_trips_through_save_and_load(self) -> None:
        state = {"statuses": {"thread-a": "idle", "thread-b": "active"}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            watch, "MAX_STATE_BYTES", 128
        ):
            path = Path(directory) / "pr-artifacts.json"
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
            bb_project_ids=("native-project",),
            github_repo="owner/repo",
            timeout_seconds=5.0,
        )

        messages = []
        with mock.patch.object(
            watch, "THREAD_ENUM_MAX_RESPONSE_CHARS", limit
        ), mock.patch.object(
            watch._watcher_liveness, "write_marker"
        ) as writer:
            completed = watch.run_once(
                "heartbeat",
                "project-a",
                "session-a",
                lambda: watch.heartbeat_cycle(
                    bounded_config,
                    enumerate_open=lambda *_, **__: [],
                    emit=messages.append,
                ),
                emit=messages.append,
            )
        self.assertFalse(completed)
        self.assertTrue(
            any("FAILED" in message and str(limit) in message for message in messages)
        )
        writer.assert_not_called()

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
    def test_watcher_is_the_only_marker_writing_entrypoint(self) -> None:
        writers = sorted(
            path.name
            for path in (ROOT / "bin").glob("*.py")
            if path.name != "_watcher_liveness.py"
            and "write_marker(" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(["orchestrator_watch.py"], writers)

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

    def test_heartbeat_wait_reports_marker_failures_and_continues(self) -> None:
        sleeps = []
        messages = []
        marker_calls = 0

        def write_marker(_project, _name, _session):
            nonlocal marker_calls
            marker_calls += 1
            if marker_calls == 1:
                raise OSError("first mount hiccup")
            if marker_calls == 2:
                raise OSError("second mount hiccup")

        with mock.patch.object(
            watch._watcher_liveness,
            "write_marker",
            side_effect=write_marker,
        ) as writer:
            watch.heartbeat_wait(
                "project-a",
                "session-a",
                True,
                sleep=sleeps.append,
                emit=messages.append,
            )
        self.assertEqual(
            watch.HEARTBEAT_REPORT_SECONDS,
            sum(sleeps),
            "the report wait must continue after an intermediate marker failure",
        )
        self.assertEqual(9, writer.call_count, "later marker refreshes must still run")
        self.assertEqual(
            [
                "HEARTBEAT MARKER WRITE FAILED — first mount hiccup",
                "HEARTBEAT MARKER WRITE FAILED — second mount hiccup",
            ],
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
            "repos": {"app": "app"},
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
                "pr-artifacts",
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
            "repos": {"app": "app"},
            "bb": {"project_id": "native-project", "timeout_seconds": 5},
            "github": {"repo": "owner/repo"},
        }
        with mock.patch.object(watch, "get_project", return_value=project):
            with self.assertRaisesRegex(
                watch.ProbeError, "bb.executable must be a non-empty list"
            ):
                watch.project_config("project-a", "pr-artifacts")

    def test_all_repo_placements_are_resolved_and_deduplicated(self) -> None:
        project = {
            "repos": {"app": "app", "docs": "docs", "shared": "shared"},
            "bb": {
                "project_id": "legacy-project",
                "project_ids": {
                    "app": "native-app",
                    "docs": "native-docs",
                    "shared": "native-app",
                },
                "executable": ["configured-bb"],
                "timeout_seconds": 5,
            },
            "github": {"repo": "owner/repo"},
        }
        with mock.patch.object(watch, "get_project", return_value=project):
            resolved = watch.project_config("project-a", "heartbeat")
        self.assertEqual(("native-app", "native-docs"), resolved.bb_project_ids)


if __name__ == "__main__":
    unittest.main()
