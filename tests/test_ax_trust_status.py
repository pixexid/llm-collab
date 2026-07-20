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
TRUSTED = {"status": "trusted", "reason": None, "remediation": None}
DOWN = {
    "status": "DOWN",
    "reason": "durable mailbox remains authoritative; the AX doorbell is degraded",
    "remediation": (
        "Grant Accessibility access to the controlling process in System Settings, "
        "then rerun tools/axbridge/axsend check."
    ),
}
DIAGNOSE_REMEDIATION = (
    "Run tools/axbridge/axsend check directly to diagnose the optional doorbell."
)


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
        scenario: str,
        activation: dict = CLI_AX_ACTIVATION,
        platform_name: str = "Darwin",
        probe_outcome: int | BaseException = 0,
        expected: dict,
    ) -> list[dict]:
        return [
            {
                "scenario": scenario,
                "project_id": project_id,
                "activation": dict(activation),
                "platform": platform_name,
                "probe_outcome": probe_outcome,
                "expected_serialized_status": json.dumps(expected, sort_keys=True),
            }
            for project_id in PROJECTS
        ]

    def assert_paired_status(self, cases: list[dict]) -> None:
        self.assertEqual({case["project_id"] for case in cases}, set(PROJECTS))
        serialized: dict[str, str] = {}
        for case in cases:
            with self.subTest(
                scenario=case["scenario"], project_id=case["project_id"]
            ):
                result, _ = self.run_case(case)
                expected = json.loads(case["expected_serialized_status"])
                serialized[case["project_id"]] = json.dumps(result, sort_keys=True)
                self.assertEqual(result, expected)
        self.assertEqual(serialized["amiga"], serialized["nuvyr"])

    def test_paired_amiga_nuvyr_exit_mapping_is_project_independent(self) -> None:
        outcomes = (
            (0, TRUSTED),
            (2, DOWN),
            (
                7,
                {
                    "status": "unavailable",
                    "reason": "AX trust probe exited with unexpected status 7",
                    "remediation": DIAGNOSE_REMEDIATION,
                },
            ),
        )
        for returncode, expected in outcomes:
            with self.subTest(returncode=returncode):
                self.assert_paired_status(
                    self.paired_cases(
                        scenario=f"exit_{returncode}",
                        probe_outcome=returncode,
                        expected=expected,
                    )
                )

    def test_probe_uses_prebuilt_binary_and_hard_timeout(self) -> None:
        case = self.paired_cases(scenario="trusted", expected=TRUSTED)[0]
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
        not_built = {
            "status": "unavailable",
            "reason": "prebuilt tools/axbridge/axsend is missing or not executable",
            "remediation": (
                "Build the optional bridge with tools/axbridge/build.sh, "
                "then rerun status."
            ),
        }
        scenarios = (
            ("missing", missing, 0, not_built),
            ("non_executable", nonexec, 0, not_built),
            (
                "timeout",
                self.binary,
                subprocess.TimeoutExpired(["axsend", "check"], 5),
                {
                    "status": "unavailable",
                    "reason": "AX trust probe timed out after 5s",
                    "remediation": DIAGNOSE_REMEDIATION,
                },
            ),
            (
                "oserror",
                self.binary,
                OSError("exec failed"),
                {
                    "status": "unavailable",
                    "reason": "AX trust probe failed with OSError",
                    "remediation": DIAGNOSE_REMEDIATION,
                },
            ),
        )
        for scenario, binary_path, outcome, expected in scenarios:
            cases = self.paired_cases(
                scenario=scenario,
                probe_outcome=outcome,
                expected=expected,
            )
            for case in cases:
                case["binary_path"] = binary_path
            self.assert_paired_status(cases)

    def test_exact_capability_allowlist_reports_na_without_probing(self) -> None:
        variants = (
            (
                "non_darwin",
                "Linux",
                CLI_AX_ACTIVATION,
                {"status": "n/a", "reason": "host platform is not Darwin", "remediation": None},
            ),
            (
                "api_trigger",
                "Darwin",
                {"type": "api_trigger", "ax_app": "Codex"},
                {
                    "status": "n/a",
                    "reason": "agent has no routine AX doorbell capability",
                    "remediation": None,
                },
            ),
            (
                "human_relay",
                "Darwin",
                {"type": "human_relay", "ax_app": "Codex"},
                {
                    "status": "n/a",
                    "reason": "agent has no routine AX doorbell capability",
                    "remediation": None,
                },
            ),
            (
                "empty_ax_app",
                "Darwin",
                {"type": "cli_session", "ax_app": ""},
                {
                    "status": "n/a",
                    "reason": "agent has no routine AX doorbell capability",
                    "remediation": None,
                },
            ),
            (
                "attended_only",
                "Darwin",
                {"type": "cli_session", "ax_app": "Codex", "ax_attended_only": True},
                {
                    "status": "n/a",
                    "reason": "agent has no routine AX doorbell capability",
                    "remediation": None,
                },
            ),
        )
        for scenario, platform_name, activation, expected in variants:
            cases = self.paired_cases(
                scenario=scenario,
                activation=activation,
                platform_name=platform_name,
                expected=expected,
            )
            serialized: dict[str, str] = {}
            for case in cases:
                with self.subTest(
                    scenario=scenario,
                    project_id=case["project_id"],
                    platform=platform_name,
                    activation_type=activation["type"],
                ):
                    result, calls = self.run_case(case)
                    serialized[case["project_id"]] = json.dumps(result, sort_keys=True)
                    self.assertEqual(
                        result, json.loads(case["expected_serialized_status"])
                    )
                    self.assertEqual(calls, [])
            self.assertEqual(serialized["amiga"], serialized["nuvyr"])

    def test_down_human_line_is_honest_and_portable(self) -> None:
        case = self.paired_cases(
            scenario="down", probe_outcome=2, expected=DOWN
        )[0]
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
        identity_content: str | None = None,
        probe_side_effect=None,
    ):
        activation = dict(CLI_AX_ACTIVATION)
        activation["watcher_enabled"] = watcher_result is not None
        agent = {"id": "codex", "activation": activation}
        with tempfile.TemporaryDirectory() as temp_dir:
            identity_path = Path(temp_dir) / "identity.md"
            if identity_content is not None:
                identity_path.write_text(identity_content, encoding="utf-8")
            probe_patch = (
                mock.patch.object(
                    session_bootstrap,
                    "probe_ax_trust",
                    side_effect=probe_side_effect,
                )
                if probe_side_effect is not None
                else mock.patch.object(
                    session_bootstrap, "probe_ax_trust", return_value=status
                )
            )
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
                    session_bootstrap, "agent_identity_path", return_value=identity_path
                ),
                mock.patch.object(session_bootstrap, "get_unread_messages", return_value=[]),
                mock.patch.object(session_bootstrap, "queue_summaries", return_value=[]),
                probe_patch,
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

    def test_human_identity_is_printed_before_probe_invocation(self) -> None:
        output = io.StringIO()
        output_seen_by_probe: list[str] = []

        def timeout_probe(_agent: dict) -> _ax_trust.AxTrustStatus:
            output_seen_by_probe.append(output.getvalue())
            return _ax_trust.AxTrustStatus(
                "unavailable",
                "AX trust probe timed out after 5s",
                DIAGNOSE_REMEDIATION,
            )

        with self.patched_bootstrap(
            status=_ax_trust.AxTrustStatus("unavailable"),
            json_output=False,
            watcher_result={"status": "error", "reason": "pm2 failed"},
            identity_content="# Codex test identity",
            probe_side_effect=timeout_probe,
        ):
            with contextlib.redirect_stdout(output):
                session_bootstrap.main()

        self.assertEqual(len(output_seen_by_probe), 1)
        self.assertIn("IDENTITY", output_seen_by_probe[0])
        self.assertIn("# Codex test identity", output_seen_by_probe[0])
        text = output.getvalue()
        self.assertLess(text.index("IDENTITY"), text.index("[ax] unavailable"))
        self.assertLess(text.index("[ax] unavailable"), text.index("[watcher] error"))
        self.assertEqual(text.count("[ax] unavailable"), 1)

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
