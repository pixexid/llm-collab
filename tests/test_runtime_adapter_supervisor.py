"""Tests for the Runtime Adapter stdio supervisor boundary."""

from __future__ import annotations

import ast
import inspect
import os
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from llm_collab.runtime_adapter_manifest import (
    ManifestResolutionError,
    TrustedManifestRegistry,
)
from llm_collab.runtime_adapter_supervisor import (
    MAX_STDERR_BYTES_PER_CONNECTION,
    StdioSupervisor,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_supervisor.py"
TEST_PATH = Path(__file__)


def manifest(script: Path, workdir: Path) -> dict:
    return {
        "adapter_a": {
            "adapter_id": "adapter_a",
            "adapter_revision": "rev_1",
            "manifest_id": "manifest_a",
            "manifest_revision": "manifest_rev_1",
            "endpoint": {
                "endpoint_id": "endpoint_a",
                "adapter_name": "adapter_a",
                "adapter_revision": "rev_1",
            },
            "executable": sys.executable,
            "argv": [sys.executable, str(script)],
            "working_directory": str(workdir),
            "environment": {"PYTHONUNBUFFERED": "1"},
            "environment_allowlist": ["PYTHONUNBUFFERED"],
        }
    }


def resolved_adapter(script: Path, workdir: Path):
    return TrustedManifestRegistry(manifest(script, workdir)).resolve("adapter_a")


def write_script(root: Path, source: str) -> Path:
    path = root / "adapter.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RuntimeAdapterSupervisorTests(unittest.TestCase):
    def test_context_manager_spawns_from_resolved_adapter_and_reaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":"r1","result":{}}\\n')
                sys.stdout.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                pid = supervisor.pid
                self.assertIsInstance(pid, int)
                outcome = supervisor.request('{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}')
                self.assertEqual(outcome.response, '{"jsonrpc":"2.0","id":"r1","result":{}}')
                self.assertIsNone(outcome.fault)
            self.assertFalse(process_alive(pid))

    def test_public_constructor_accepts_only_resolved_adapter(self) -> None:
        params = set(inspect.signature(StdioSupervisor.__init__).parameters)
        self.assertEqual(params, {"self", "resolved"})
        self.assertFalse(
            params
            & {
                "executable",
                "path",
                "argv",
                "env",
                "environment",
                "working_directory",
                "workdir",
                "shell",
                "manifest_path",
                "adapter_alias",
            }
        )

    def test_relative_executable_and_workdir_fail_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(root, "pass\n")
            adapter = resolved_adapter(script, root)
            cases = {
                "executable": replace(adapter, executable="adapter-a"),
                "working_directory": replace(adapter, working_directory="relative-work"),
            }
            for name, candidate in cases.items():
                with self.subTest(name=name), patch(
                    "llm_collab.runtime_adapter_supervisor.subprocess.Popen",
                    side_effect=AssertionError("spawn must not run"),
                ):
                    with self.assertRaises(ManifestResolutionError):
                        with StdioSupervisor(candidate):
                            pass

    def test_shell_false_is_explicit_and_shell_mutation_would_be_detected(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        popen_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen"
        ]
        self.assertEqual(len(popen_calls), 1)
        shell_keywords = [kw for kw in popen_calls[0].keywords if kw.arg == "shell"]
        self.assertEqual(len(shell_keywords), 1)
        self.assertIs(shell_keywords[0].value.value, False)

    def test_stderr_is_drained_continuously_past_limit_without_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                f"""
                import sys
                sys.stderr.buffer.write(b"x" * {MAX_STDERR_BYTES_PER_CONNECTION + 4096})
                sys.stderr.buffer.flush()
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b'{{"jsonrpc":"2.0","id":"r1","result":{{}}}}\\n')
                sys.stdout.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                self.assertEqual(outcome.fault, "STDERR_LIMIT_EXCEEDED")
                self.assertTrue(outcome.stderr_truncated)
                self.assertLessEqual(len(outcome.stderr), MAX_STDERR_BYTES_PER_CONNECTION)

    def test_oversized_stdout_frame_is_bounded_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b"x" * 1048578)
                sys.stdout.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                self.assertEqual(outcome.fault, "MESSAGE_TOO_LARGE")
                self.assertTrue(outcome.should_close)

    def test_abnormal_host_exit_reaps_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import time
                time.sleep(30)
                """,
            )
            pid = None
            try:
                with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                    pid = supervisor.pid
                    raise RuntimeError("host failure")
            except RuntimeError:
                pass
            self.assertIsInstance(pid, int)
            self.assertFalse(process_alive(pid))

    def test_no_forbidden_process_or_state_imports(self) -> None:
        forbidden_llm_collab = {
            "canonical",
            "ledger",
            "compatibility",
            "daemon",
            "registry",
            "project_issue_queue",
            "inbox",
        }
        forbidden_os_calls = {"system", "popen"}
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "llm_collab":
                        self.assertFalse(set(parts) & forbidden_llm_collab)
            if isinstance(node, ast.ImportFrom):
                parts = (node.module or "").split(".")
                if parts and parts[0] == "llm_collab":
                    self.assertFalse(set(parts) & forbidden_llm_collab)
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".", 1)[0], forbidden_llm_collab)
                if node.module == "os":
                    self.assertFalse(any(alias.name in forbidden_os_calls for alias in node.names))
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    self.assertFalse(func.value.id == "os" and func.attr in forbidden_os_calls)
                if isinstance(func, ast.Name):
                    self.assertNotIn(func.id, forbidden_os_calls)

    def test_no_bin_consumer_imports_supervisor_module(self) -> None:
        for path in (ROOT / "bin").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(alias.name == "llm_collab.runtime_adapter_supervisor" for alias in node.names),
                        path,
                    )
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "llm_collab.runtime_adapter_supervisor", path)
                    if node.module == "llm_collab":
                        self.assertFalse(
                            any(alias.name == "runtime_adapter_supervisor" for alias in node.names),
                            path,
                        )

    def test_tests_do_not_construct_supervisor_from_raw_execution_inputs(self) -> None:
        params = {"executable", "argv", "environment", "working_directory", "shell"}
        tree = ast.parse(TEST_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "StdioSupervisor":
                    self.assertFalse(any(keyword.arg in params for keyword in node.keywords))


if __name__ == "__main__":
    unittest.main()
