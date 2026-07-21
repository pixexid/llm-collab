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
    def run_minimal_init(self, root: Path, supplied: list[str]) -> dict:
        agents = [
            {
                "id": "operator",
                "display_name": "Operator",
                "role": "operator",
                "activation": {"type": "human"},
            }
        ]
        with patch.object(init_script, "ROOT", root):
            with patch.object(init_script, "_local_config", {}):
                with patch.object(init_script, "collect_agents", return_value=agents):
                    with patch.object(init_script, "collect_projects", return_value=[]):
                        with redirect_stdout(StringIO()):
                            init_script.main(input_fn=answers(supplied))
        return json.loads((root / "collab.config.json").read_text())

    def test_fresh_init_generates_and_persists_workspace_id(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(init_script, "generate_workspace_id", return_value="ws_fresh123"):
                config = self.run_minimal_init(
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
                config = self.run_minimal_init(
                    root,
                    ["y", "test", str(root / "repos"), str(root / "state"), "15", "n"],
                )
            self.assertEqual(config["workspace_id"], "ws_existing123")

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
