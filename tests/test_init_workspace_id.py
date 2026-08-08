from __future__ import annotations

import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import init as init_script


def answers(values: list[str]):
    remaining = iter(values)

    def read(_prompt: str) -> str:
        try:
            return next(remaining)
        except StopIteration as exc:
            raise AssertionError("initializer requested an unexpected answer") from exc

    return read


class InitWorkspaceIdTest(unittest.TestCase):
    def run_minimal_init(self, root: Path, supplied: list[str]) -> tuple[dict, str]:
        agents = [
            {
                "id": "operator",
                "display_name": "Operator",
                "role": "operator",
                "activation": {"type": "human"},
            }
        ]
        return self.run_init_with_agents(root, agents, supplied)

    def run_init_with_agents(
        self, root: Path, agents: list[dict], supplied: list[str]
    ) -> tuple[dict, str]:
        with patch.object(init_script, "ROOT", root):
            with patch.object(init_script, "_local_config", {}):
                with patch.object(init_script, "collect_agents", return_value=agents):
                    with patch.object(init_script, "collect_projects", return_value=[]):
                        output = StringIO()
                        with redirect_stdout(output):
                            init_script.main(input_fn=answers(supplied))
        return json.loads((root / "collab.config.json").read_text()), output.getvalue()

    def test_fresh_init_generates_and_persists_workspace_id(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(init_script, "generate_workspace_id", return_value="ws_fresh123"):
                config, _ = self.run_minimal_init(
                    root,
                    ["test", str(root / "repos"), str(root / "state"), "15", "n"],
                )
            self.assertEqual(config["workspace_id"], "ws_fresh123")

    def test_reinitialize_preserves_existing_identity_instead_of_rotating(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "collab.config.json").write_text(
                json.dumps({"workspace_id": "ws_existing123", "operator_value": 7})
            )
            with patch.object(
                init_script,
                "generate_workspace_id",
                side_effect=AssertionError("existing workspace identity must not rotate"),
            ):
                config, _ = self.run_minimal_init(
                    root,
                    ["y", "test", str(root / "repos"), str(root / "state"), "15", "n"],
                )
            self.assertEqual(config["workspace_id"], "ws_existing123")

    def test_fresh_init_points_to_complete_canonical_pm2_rotation_workflow(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, output = self.run_minimal_init(
                root,
                ["test", str(root / "repos"), str(root / "state"), "15", "n"],
        )

        workflow_path = "docs/workflows/pm2-log-rotation.md"
        self.assertIn(
            "1. PM2 log rotation, required before any watcher runs:",
            output.splitlines(),
        )
        self.assertIn(f"   {workflow_path}", output.splitlines())
        self.assertLess(output.index(workflow_path), output.index("2. Bootstrap each agent session:"))
        self.assertNotIn("pm2 install", output)
        self.assertNotIn("pm2 set", output)
        self.assertNotIn("pm2 trigger", output)

        workflow = (REPO_ROOT / workflow_path).read_text()
        required_steps = (
            "operator-owned disposition",
            "Installing `pm2-logrotate` starts the module and rotation immediately",
            "Archive any history needed for diagnosis",
            "Do not blindly",
            "Inspect orphaned files",
            "install pm2-" + "logrotate",
            "max_size 10M",
            "retain 7",
            "compress true",
            "rotateInterval '0 0 * * *'",
            "pm2_watchers.py start --all",
            "sleep 35",
            "list watched logs",
            "PASS only if",
            "Logs/watchers/",
            "~/.pm2/logs/",
            "If either path class is",
            "missing, treat rotation coverage as failed",
            "current file plus seven gzip-compressed generations",
        )
        cursor = -1
        for step in required_steps:
            cursor = workflow.find(step, cursor + 1)
            self.assertNotEqual(-1, cursor, f"missing or out-of-order PM2 step: {step}")

    def test_setup_flow_never_pairs_skippable_rotation_with_a_watcher_command(self) -> None:
        # GH-673 head 1: the setup flow used to print rotation as "(optional)" and
        # then a bare bootstrap command that starts an unrotated watcher. Rotation
        # is no longer labelled skippable where a watcher command is printed, and
        # an agent the operator did not enable a watcher for bootstraps with
        # --no-watcher. (What --no-watcher actually does -- skip STARTING, not stop
        # an existing watcher -- is pinned by the head-2 test below.)
        agents = [
            {
                "id": "watchdog",
                "display_name": "Watchdog",
                "role": "implementation",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
            {
                "id": "dry",
                "display_name": "Dry",
                "role": "implementation",
                "activation": {"type": "cli_session", "watcher_enabled": False},
            },
            {
                "id": "operator",
                "display_name": "Operator",
                "role": "operator",
                "activation": {"type": "human"},
            },
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, output = self.run_init_with_agents(
                root,
                agents,
                ["test", str(root / "repos"), str(root / "state"), "15", "n"],
            )

        lines = output.splitlines()

        # Direction 1 — the optional-labelled path can no longer reach watcher
        # creation: rotation is not presented as skippable, and the agent not
        # enabled for a watcher is printed with --no-watcher (not a bare command).
        rotation_lines = [ln for ln in lines if "PM2 log rotation" in ln]
        self.assertTrue(rotation_lines, "rotation step must be printed")
        for ln in rotation_lines:
            self.assertNotIn("(optional", ln)
            self.assertNotIn("optional,", ln)

        dry_lines = [ln for ln in lines if "current_runtime.py --agent dry" in ln]
        self.assertEqual(1, len(dry_lines), "the non-watcher agent has one bootstrap line")
        self.assertIn("--no-watcher", dry_lines[0])

        # The watcher-enabled agent still gets the bare command (rotation required
        # above it), so the genuinely-wanted watcher path is not broken either.
        watchdog_lines = [ln for ln in lines if "current_runtime.py --agent watchdog" in ln]
        self.assertEqual(1, len(watchdog_lines), "the watcher agent has one bootstrap line")
        self.assertNotIn("--no-watcher", watchdog_lines[0])

    def test_decline_path_says_skip_starting_and_warns_existing_watcher_runs(self) -> None:
        # GH-673 head 2: --no-watcher only skips STARTING a watcher this session;
        # it never stops an existing agent-wide PM2 watcher (session_bootstrap just
        # bypasses start_watcher). The printed decline path must say exactly that --
        # matching the flag's help text "Skip starting the inbox watcher" -- and must
        # NOT overclaim that --no-watcher means no watcher runs. A test that only
        # checks the flag is present cannot see this defect; that is how it survived
        # head 1, so this test pins the MEANING, not the flag.
        agents = [
            {
                "id": "dry",
                "display_name": "Dry",
                "role": "implementation",
                "activation": {"type": "cli_session", "watcher_enabled": False},
            },
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, output = self.run_init_with_agents(
                root,
                agents,
                ["test", str(root / "repos"), str(root / "state"), "15", "n"],
            )

        self._assert_accurate_decline_wording(output.lower(), "init [Next steps]")

        getting_started = (REPO_ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
        self._assert_accurate_decline_wording(getting_started.lower(), "docs/getting-started.md")

    def _assert_accurate_decline_wording(self, text: str, label: str) -> None:
        # The accurate action, matching the --no-watcher help text.
        self.assertIn(
            "skips starting", text, f"{label}: must say --no-watcher skips starting a watcher"
        )
        # The existing-watcher caveat: the flag does not stop one already running.
        self.assertIn(
            "already running", text, f"{label}: must warn an existing watcher is unaffected"
        )
        self.assertIn(
            "does not stop", text, f"{label}: must state it does not stop a watcher"
        )
        # The proxy-for-invariant overclaims that survived head 1.
        for overclaim in (
            "decline the watcher",
            "declines the watcher",
            "declining the watcher",
            "without a watcher",
            "start without one",
        ):
            self.assertNotIn(overclaim, text, f"{label}: overclaim present: {overclaim!r}")

    def test_add_workspace_id_is_backup_protected_atomic_and_non_destructive(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "collab.config.json"
            original = b'{"workspace_name":"existing","operator_value":7}\n'
            config_path.write_bytes(original)
            with patch.object(init_script, "ROOT", root):
                with patch.object(init_script, "generate_workspace_id", return_value="ws_added123"):
                    with patch.object(
                        init_script,
                        "write_json",
                        side_effect=AssertionError("add path must not use destructive write_json"),
                    ):
                        with redirect_stdout(StringIO()):
                            result = init_script.add_workspace_id()

            self.assertEqual(result, "ws_added123")
            self.assertEqual(
                json.loads(config_path.read_text()),
                {"workspace_name": "existing", "operator_value": 7, "workspace_id": "ws_added123"},
            )
            backup = root / "collab.config.json.pre-workspace-id.bak"
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_add_workspace_id_refuses_overwrite_and_backup_collision(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "collab.config.json"
            existing = b'{"workspace_id":"ws_existing123","operator_value":7}\n'
            config_path.write_bytes(existing)
            with patch.object(init_script, "ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                    init_script.add_workspace_id()
            self.assertEqual(config_path.read_bytes(), existing)
            self.assertFalse((root / "collab.config.json.pre-workspace-id.bak").exists())

            original = b'{"workspace_name":"legacy"}\n'
            config_path.write_bytes(original)
            backup = root / "collab.config.json.pre-workspace-id.bak"
            backup.write_bytes(b"operator backup")
            with patch.object(init_script, "ROOT", root):
                with self.assertRaises(FileExistsError):
                    init_script.add_workspace_id()
            self.assertEqual(config_path.read_bytes(), original)
            self.assertEqual(backup.read_bytes(), b"operator backup")

    def test_atomic_replace_failure_leaves_original_and_verified_backup(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "collab.config.json"
            original = b'{"workspace_name":"legacy"}\n'
            config_path.write_bytes(original)
            with patch.object(init_script, "ROOT", root):
                with patch.object(init_script.os, "replace", side_effect=OSError("injected replace failure")):
                    with self.assertRaisesRegex(OSError, "injected replace failure"):
                        init_script.add_workspace_id()
            self.assertEqual(config_path.read_bytes(), original)
            self.assertEqual(
                (root / "collab.config.json.pre-workspace-id.bak").read_bytes(),
                original,
            )
            self.assertEqual(list(root.glob(".collab.config.json.*.tmp")), [])

    def test_cli_flag_selects_only_the_non_destructive_add_path(self) -> None:
        with patch.object(init_script, "add_workspace_id", return_value="ws_added123") as add:
            with patch.object(
                init_script,
                "main",
                side_effect=AssertionError("flag must not enter full initialization"),
            ):
                init_script.cli(["--add-workspace-id"])
        add.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
