from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _ax_trust
import pm2_watchers
import session_bootstrap


PROJECTS = ("amiga", "nuvyr")
CLI_AX_ACTIVATION = {
    "type": "cli_session",
    "ax_app": "Codex",
    "ax_attended_only": False,
}


class AxTrustProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.binary = Path(self.temp_dir.name) / "axsend"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binary.chmod(0o755)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def agent(project_id: str, activation: dict) -> dict:
        return {
            "id": f"{project_id}-worker",
            "activation": dict(activation),
        }

    def run_case(self, case: dict) -> tuple[dict, list[dict]]:
        calls: list[dict] = []

        def fake_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess:
            calls.append({"command": command, **kwargs})
            outcome = case["probe_outcome"]
            if isinstance(outcome, BaseException):
                raise outcome
            return subprocess.CompletedProcess(command, outcome, stdout="", stderr="")

        result = _ax_trust.probe_ax_trust(
            self.agent(case["project_id"], case["activation"]),
            platform_name=case["platform"],
            binary_path=case.get("binary_path", self.binary),
            runner=fake_runner,
        )
        return result.as_dict(), calls

    def paired_cases(
        self,
        *,
        activation: dict = CLI_AX_ACTIVATION,
        platform_name: str = "Darwin",
        probe_outcome: int | BaseException = 0,
        expected_status: str,
    ) -> list[dict]:
        return [
            {
                "project_id": project_id,
                "activation": dict(activation),
                "platform": platform_name,
                "probe_outcome": probe_outcome,
                "expected_serialized_status": json.dumps({"status": expected_status}),
            }
            for project_id in PROJECTS
        ]

    def assert_paired_status(self, cases: list[dict]) -> None:
        self.assertEqual({case["project_id"] for case in cases}, set(PROJECTS))
        serialized: dict[str, str] = {}
        for case in cases:
            with self.subTest(project_id=case["project_id"]):
                result, _ = self.run_case(case)
                expected = json.loads(case["expected_serialized_status"])
                self.assertEqual(result["status"], expected["status"])
                serialized[case["project_id"]] = json.dumps(result, sort_keys=True)
        self.assertEqual(
            json.loads(serialized["amiga"])["status"],
            json.loads(serialized["nuvyr"])["status"],
        )

    def test_paired_amiga_nuvyr_exit_mapping_is_project_independent(self) -> None:
        for returncode, expected in ((0, "trusted"), (2, "DOWN"), (7, "unavailable")):
            with self.subTest(returncode=returncode):
                self.assert_paired_status(
                    self.paired_cases(probe_outcome=returncode, expected_status=expected)
                )

    def test_probe_uses_prebuilt_binary_and_hard_timeout(self) -> None:
        case = self.paired_cases(expected_status="trusted")[0]
        result, calls = self.run_case(case)
        self.assertEqual(result["status"], "trusted")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["command"], [str(self.binary), "check"])
        self.assertEqual(calls[0]["timeout"], 5)
        self.assertLessEqual(calls[0]["timeout"], 5)
        self.assertFalse(calls[0]["check"])

    def test_missing_nonexecutable_timeout_and_errors_are_unavailable(self) -> None:
        missing = Path(self.temp_dir.name) / "missing"
        nonexec = Path(self.temp_dir.name) / "nonexec"
        nonexec.write_text("stub", encoding="utf-8")
        cases = [
            {
                **self.paired_cases(expected_status="unavailable")[0],
                "binary_path": missing,
            },
            {
                **self.paired_cases(expected_status="unavailable")[1],
                "binary_path": nonexec,
            },
            {
                **self.paired_cases(
                    probe_outcome=subprocess.TimeoutExpired(["axsend", "check"], 5),
                    expected_status="unavailable",
                )[0],
            },
            {
                **self.paired_cases(
                    probe_outcome=OSError("exec failed"),
                    expected_status="unavailable",
                )[1],
            },
        ]
        for case in cases:
            with self.subTest(project_id=case["project_id"], outcome=case["probe_outcome"]):
                result, _ = self.run_case(case)
                self.assertEqual(result["status"], "unavailable")

    def test_exact_capability_allowlist_reports_na_without_probing(self) -> None:
        variants = (
            ("Linux", CLI_AX_ACTIVATION),
            ("Darwin", {"type": "api_trigger", "ax_app": "Codex"}),
            ("Darwin", {"type": "human_relay", "ax_app": "Codex"}),
            ("Darwin", {"type": "cli_session", "ax_app": ""}),
            (
                "Darwin",
                {"type": "cli_session", "ax_app": "Codex", "ax_attended_only": True},
            ),
        )
        for platform_name, activation in variants:
            cases = self.paired_cases(
                activation=activation,
                platform_name=platform_name,
                expected_status="n/a",
            )
            for case in cases:
                with self.subTest(
                    project_id=case["project_id"],
                    platform=platform_name,
                    activation_type=activation["type"],
                ):
                    result, calls = self.run_case(case)
                    self.assertEqual(result["status"], "n/a")
                    self.assertEqual(calls, [])

    def test_down_human_line_is_honest_and_portable(self) -> None:
        case = self.paired_cases(probe_outcome=2, expected_status="DOWN")[0]
        result_dict, _ = self.run_case(case)
        line = _ax_trust.format_ax_status(_ax_trust.AxTrustStatus(**result_dict))
        self.assertTrue(line.startswith("[ax] DOWN"))
        self.assertIn("durable mailbox remains authoritative", line)
        self.assertIn("doorbell is degraded", line)
        self.assertIn("Remediation:", line)
        self.assertNotIn("/Users/", line)

    def test_helper_has_no_project_or_build_coupling(self) -> None:
        source = Path(_ax_trust.__file__).read_text(encoding="utf-8")
        self.assertNotIn("projects.json", source)
        self.assertNotIn("project_id", source)
        self.assertNotIn("axsend-ensure", source)
        self.assertNotIn("subprocess.run([\"bash\"", source)


class AxTrustCallerTest(unittest.TestCase):
    @staticmethod
    def bootstrap_args(*, json_output: bool, no_watcher: bool = True) -> argparse.Namespace:
        return argparse.Namespace(
            agent="codex",
            limit=5,
            no_watcher=no_watcher,
            json_output=json_output,
        )

    @contextlib.contextmanager
    def patched_bootstrap(
        self,
        *,
        status: _ax_trust.AxTrustStatus,
        json_output: bool,
        watcher_result: dict | None = None,
    ):
        activation = dict(CLI_AX_ACTIVATION)
        activation["watcher_enabled"] = watcher_result is not None
        agent = {"id": "codex", "activation": activation}
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_identity = Path(temp_dir) / "identity.md"
            patches = (
                mock.patch.object(
                    session_bootstrap,
                    "parse_args",
                    return_value=self.bootstrap_args(
                        json_output=json_output,
                        no_watcher=watcher_result is None,
                    ),
                ),
                mock.patch.object(session_bootstrap, "agent_ids", return_value=["codex"]),
                mock.patch.object(session_bootstrap, "get_agent", return_value=agent),
                mock.patch.object(
                    session_bootstrap, "agent_identity_path", return_value=missing_identity
                ),
                mock.patch.object(session_bootstrap, "get_unread_messages", return_value=[]),
                mock.patch.object(session_bootstrap, "queue_summaries", return_value=[]),
                mock.patch.object(session_bootstrap, "probe_ax_trust", return_value=status),
                mock.patch.object(
                    session_bootstrap,
                    "start_watcher",
                    return_value=watcher_result or {"status": "skipped"},
                ),
                mock.patch.object(session_bootstrap, "utc_iso", return_value="2026-07-20T00:00:00Z"),
            )
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                yield

    def test_human_bootstrap_prints_exactly_one_ax_line_when_watcher_skips(self) -> None:
        output = io.StringIO()
        with self.patched_bootstrap(
            status=_ax_trust.AxTrustStatus("trusted"),
            json_output=False,
        ):
            with contextlib.redirect_stdout(output):
                session_bootstrap.main()
        ax_lines = [line for line in output.getvalue().splitlines() if line.startswith("[ax]")]
        self.assertEqual(ax_lines, ["[ax] trusted"])

    def test_down_does_not_block_bootstrap_even_when_watcher_errors(self) -> None:
        output = io.StringIO()
        down = _ax_trust.AxTrustStatus(
            "DOWN",
            "durable mailbox remains authoritative; the AX doorbell is degraded",
            "Grant Accessibility access, then rerun tools/axbridge/axsend check.",
        )
        with self.patched_bootstrap(
            status=down,
            json_output=False,
            watcher_result={"status": "error", "reason": "pm2 failed"},
        ):
            with contextlib.redirect_stdout(output):
                result = session_bootstrap.main()
        self.assertIsNone(result)
        self.assertIn("[watcher] error", output.getvalue())
        self.assertEqual(output.getvalue().count("[ax] DOWN"), 1)

    def test_json_bootstrap_has_ax_object_and_no_human_ax_prose(self) -> None:
        output = io.StringIO()
        with self.patched_bootstrap(
            status=_ax_trust.AxTrustStatus("unavailable", "not built", None),
            json_output=True,
        ):
            with contextlib.redirect_stdout(output):
                session_bootstrap.main()
        text = output.getvalue()
        self.assertNotIn("[ax]", text)
        payload = json.loads(text)
        self.assertEqual(
            payload["ax"],
            {"status": "unavailable", "reason": "not built", "remediation": None},
        )

    def test_pm2_missing_or_timeout_cannot_suppress_ax_and_keeps_exit_code(self) -> None:
        for exit_code in (1, 124):
            output = io.StringIO()
            with self.subTest(exit_code=exit_code):
                with mock.patch.object(
                    sys,
                    "argv",
                    ["pm2_watchers.py", "status", "--agent", "codex"],
                ):
                    with mock.patch.object(pm2_watchers, "agent_ids", return_value=["codex"]):
                        with mock.patch.object(
                            pm2_watchers,
                            "get_agent",
                            return_value={
                                "id": "codex",
                                "activation": dict(CLI_AX_ACTIVATION),
                            },
                        ):
                            with mock.patch.object(
                                pm2_watchers,
                                "probe_ax_trust",
                                return_value=_ax_trust.AxTrustStatus("trusted"),
                            ):
                                with mock.patch.object(
                                    pm2_watchers, "config_get", return_value="llm-collab"
                                ):
                                    with mock.patch.object(
                                        pm2_watchers,
                                        "pm2_run",
                                        side_effect=SystemExit(exit_code),
                                    ):
                                        with self.assertRaises(SystemExit) as context:
                                            with contextlib.redirect_stdout(output):
                                                pm2_watchers.main()
                self.assertEqual(context.exception.code, exit_code)
                self.assertEqual(
                    [
                        line
                        for line in output.getvalue().splitlines()
                        if line.startswith("[ax]")
                    ],
                    ["[ax] trusted agent=codex"],
                )

    def test_pm2_nonzero_result_is_returned_after_ax_status(self) -> None:
        output = io.StringIO()
        failed = subprocess.CompletedProcess(["pm2", "describe"], 9)
        with mock.patch.object(
            sys,
            "argv",
            ["pm2_watchers.py", "status", "--agent", "codex"],
        ):
            with mock.patch.object(pm2_watchers, "agent_ids", return_value=["codex"]):
                with mock.patch.object(
                    pm2_watchers,
                    "get_agent",
                    return_value={"id": "codex", "activation": dict(CLI_AX_ACTIVATION)},
                ):
                    with mock.patch.object(
                        pm2_watchers,
                        "probe_ax_trust",
                        return_value=_ax_trust.AxTrustStatus("trusted"),
                    ):
                        with mock.patch.object(
                            pm2_watchers, "config_get", return_value="llm-collab"
                        ):
                            with mock.patch.object(
                                pm2_watchers, "pm2_run", return_value=failed
                            ):
                                with self.assertRaises(SystemExit) as context:
                                    with contextlib.redirect_stdout(output):
                                        pm2_watchers.main()
        self.assertEqual(context.exception.code, 9)
        self.assertIn("[ax] trusted agent=codex", output.getvalue())

    def test_pm2_all_applies_capability_per_agent_before_pm2_failure(self) -> None:
        agents = [
            {"id": "codex", "activation": dict(CLI_AX_ACTIVATION)},
            {
                "id": "operator",
                "activation": {"type": "human_relay", "watcher_enabled": True},
            },
        ]
        outcomes = {
            "codex": _ax_trust.AxTrustStatus("trusted"),
            "operator": _ax_trust.AxTrustStatus(
                "n/a", "agent has no routine AX doorbell capability"
            ),
        }
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["pm2_watchers.py", "status", "--all"]):
            with mock.patch.object(
                pm2_watchers, "watcher_enabled_agents", return_value=agents
            ):
                with mock.patch.object(
                    pm2_watchers,
                    "get_agent",
                    side_effect=lambda agent_id: next(
                        agent for agent in agents if agent["id"] == agent_id
                    ),
                ):
                    with mock.patch.object(
                        pm2_watchers,
                        "probe_ax_trust",
                        side_effect=lambda agent: outcomes[agent["id"]],
                    ):
                        with mock.patch.object(
                            pm2_watchers, "config_get", return_value="llm-collab"
                        ):
                            with mock.patch.object(
                                pm2_watchers, "pm2_run", side_effect=SystemExit(1)
                            ):
                                with self.assertRaises(SystemExit):
                                    with contextlib.redirect_stdout(output):
                                        pm2_watchers.main()
        ax_lines = [
            line for line in output.getvalue().splitlines() if line.startswith("[ax]")
        ]
        self.assertEqual(len(ax_lines), 2)
        self.assertTrue(any("trusted agent=codex" in line for line in ax_lines))
        self.assertTrue(any("n/a agent=operator" in line for line in ax_lines))


if __name__ == "__main__":
    unittest.main()
