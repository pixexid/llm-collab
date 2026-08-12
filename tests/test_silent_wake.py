"""Focused authority and residual-producer checks for GH-805."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import _role_generation
import _watcher_liveness
import resolve_role_wake


def load_watcher():
    spec = importlib.util.spec_from_file_location(
        "silent_wake_orchestrator_watch", ROOT / "bin" / "orchestrator_watch.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


watch = load_watcher()


def role_record(project_id: str, thread_id: str) -> str:
    return "# Role generation\n\n```json\n" + json.dumps(
        {
            "role_id": f"orchestrator:{project_id}",
            "scope": {"kind": "project", "project_id": project_id},
            "epoch": 1,
            "status": "active",
            "thread_id": thread_id,
        }
    ) + "\n```\n"


class RoleGenerationTest(unittest.TestCase):
    def test_valid_record_and_promotion_are_read_at_call_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            _role_generation, "project_state_dir", return_value=Path(directory)
        ):
            path = Path(directory) / "role-generation.md"
            path.write_text(role_record("project-a", "role-old"), encoding="utf-8")
            self.assertEqual(
                "role-old", _role_generation.current_orchestrator_thread_id("project-a")
            )
            path.write_text(role_record("project-a", "role-new"), encoding="utf-8")
            self.assertEqual(
                "role-new", _role_generation.current_orchestrator_thread_id("project-a")
            )

    def test_missing_malformed_and_cross_project_records_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            _role_generation, "project_state_dir", return_value=Path(directory)
        ):
            path = Path(directory) / "role-generation.md"
            for content in (
                None,
                "not a role record",
                role_record("project-b", "role-b"),
                role_record("project-a", " padded "),
            ):
                with self.subTest(content=content):
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(content, encoding="utf-8")
                    with self.assertRaises(_role_generation.RoleGenerationError):
                        _role_generation.current_orchestrator_thread_id("project-a")

    def test_native_project_resolution_emits_only_registered_project_and_role(self) -> None:
        output = io.StringIO()
        with mock.patch.object(
            resolve_role_wake, "_resolve_thread_project", return_value="project-a"
        ), mock.patch.object(
            resolve_role_wake, "current_orchestrator_thread_id", return_value="role-a"
        ), contextlib.redirect_stdout(output):
            self.assertEqual(0, resolve_role_wake.main(["--thread-project", "native-a"]))
        self.assertEqual(
            {"project_id": "project-a", "thread_id": "role-a"},
            json.loads(output.getvalue()),
        )

    def test_unknown_native_project_refuses(self) -> None:
        with mock.patch.object(
            resolve_role_wake, "_resolve_thread_project", return_value=None
        ), self.assertRaisesRegex(SystemExit, "no registered collab owner"):
            resolve_role_wake.main(["--thread-project", "unknown"])


class ResidualProducerTest(unittest.TestCase):
    def config(self):
        return watch.WatcherConfig(
            bb_executable=("configured-bb", "--wrapper"),
            bb_project_ids=("native-a",),
            github_repo="owner/repo",
            timeout_seconds=5.0,
            project_id="project-a",
        )

    def test_host_uses_only_the_plugin_command_with_semantic_digest(self) -> None:
        calls = []

        def transport(executable):
            self.assertEqual(("configured-bb", "--wrapper"), executable)

            def invoke(argv, timeout):
                calls.append((tuple(argv), timeout))
                return mock.Mock(exit_code=0, stdout="accepted\n", stderr="")

            return invoke

        with mock.patch.object(watch, "subprocess_transport", side_effect=transport):
            watch.request_silent_wake(
                self.config(), "heartbeat", "a" * 64, 10.0, monotonic=lambda: 0.0
            )
        self.assertEqual(
            [
                (
                    (
                        "silent-wake", "emit", "--project", "project-a",
                        "--producer", "heartbeat", "--semantic", "a" * 64,
                    ),
                    5.0,
                )
            ],
            calls,
        )

    def test_confirmed_plugin_refusal_does_not_advance_pr_baseline(self) -> None:
        state = {"signatures": {"17": json.dumps({"old": True})}, "terminal_left": {}}
        before = json.loads(json.dumps(state))
        with mock.patch.object(
            watch,
            "request_silent_wake",
            side_effect=watch.ProbeError("confirmed refusal"),
        ):
            with self.assertRaisesRegex(watch.ProbeError, "confirmed refusal"):
                watch.pr_cycle(
                    self.config(),
                    state,
                    enumerate_prs=lambda *_, **__: [17],
                    signature=lambda *_: {
                        "state": "open", "merged": False, "head": "a" * 40, "timeline": []
                    },
                    emit=lambda _line: None,
                )
        self.assertEqual(before, state)

    def test_two_markers_and_retired_python_transport_absent(self) -> None:
        self.assertEqual(("pr-artifacts", "heartbeat"), _watcher_liveness.WATCHER_NAMES)
        source = (ROOT / "bin" / "orchestrator_watch.py").read_text(encoding="utf-8")
        for retired in (
            "worker-lifecycle",
            "RoleWakeCache",
            "role_wake_emitter",
            "ROLE_WAKE_POINTER",
            '"thread", "tell"',
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, source)
        workflow = (ROOT / "docs" / "workflows" / "orchestrator-sessions.md").read_text(
            encoding="utf-8"
        )
        standard = workflow.split("## Standard watcher set", 1)[1].split(
            "## Verification traps", 1
        )[0]
        self.assertIn("exactly two host", standard)
        self.assertIn("orchestrator_watch.py pr-artifacts", standard)
        self.assertIn("orchestrator_watch.py heartbeat", standard)
        self.assertNotIn("orchestrator_watch.py worker", standard)


if __name__ == "__main__":
    unittest.main()
