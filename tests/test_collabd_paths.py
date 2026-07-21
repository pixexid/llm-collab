from __future__ import annotations

import inspect
import os
import re
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.ledger.paths import (
    LedgerPaths,
    MAX_UNIX_SOCKET_PATH_BYTES,
    generate_workspace_id,
    validate_project_id,
    validate_workspace_id,
)


class LedgerPathsTest(unittest.TestCase):
    def test_exact_workspace_paths_are_fixed_under_state_root(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "state"
            paths = LedgerPaths.derive(root, "ws_alpha")
            root = root.resolve()
            workspace = root / "llm-collabd" / "ws_alpha"
            self.assertEqual(
                {
                    "workspace_root": paths.workspace_root,
                    "ledger": paths.ledger,
                    "backups": paths.backups,
                    "socket": paths.socket,
                    "lock": paths.lock,
                    "log": paths.log,
                },
                {
                    "workspace_root": workspace,
                    "ledger": workspace / "ledger.sqlite3",
                    "backups": workspace / "backups",
                    "socket": workspace / "daemon.sock",
                    "lock": workspace / "daemon.lock",
                    "log": workspace / "logs" / "llm-collabd.jsonl",
                },
            )

            paths.ensure_directories()
            for directory in (root / "llm-collabd", workspace, paths.backups, paths.logs):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_workspace_and_project_id_validation_rejects_escape_and_reserved_names(self) -> None:
        for invalid in ("../escape", "ws_..", "ws_a/b", "alpha", "ws_a"):
            with self.subTest(workspace_id=invalid):
                with self.assertRaisesRegex(ValueError, "workspace_id"):
                    validate_workspace_id(invalid)

        self.assertEqual(validate_project_id("amiga"), "amiga")
        self.assertEqual(validate_project_id("nuvyr.app"), "nuvyr.app")
        for invalid in ("../amiga", "amiga/other", "llm-collabd", "backups", "daemon.lock"):
            with self.subTest(project_id=invalid):
                with self.assertRaises(ValueError):
                    validate_project_id(invalid)
        with self.assertRaises(ValueError):
            validate_project_id("Backups")

    def test_direct_construction_cannot_override_an_artifact_path(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            derived = LedgerPaths.derive(tmp, "ws_alpha")
            values = dict(derived.__dict__)
            values["ledger"] = Path(tmp) / "caller-selected.sqlite3"
            with self.assertRaisesRegex(ValueError, "may not override"):
                LedgerPaths(**values)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_existing_symlink_escape_is_rejected_before_creation(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            base = Path(tmp)
            state = base / "state"
            outside = base / "outside"
            state.mkdir()
            outside.mkdir()
            (state / "llm-collabd").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "escapes"):
                LedgerPaths.derive(state, "ws_alpha")
            self.assertEqual(list(outside.iterdir()), [])

    def test_non_directory_incumbent_is_rejected_without_chmod_or_replacement(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            incumbent = state / "llm-collabd"
            incumbent.write_bytes(b"operator-owned")
            incumbent.chmod(0o644)
            paths = LedgerPaths.derive(state, "ws_alpha")

            with self.assertRaisesRegex(ValueError, "no-follow directory"):
                paths.ensure_directories()
            self.assertEqual(incumbent.read_bytes(), b"operator-owned")
            self.assertEqual(incumbent.stat().st_mode & 0o777, 0o644)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_create_open_symlink_swap_fails_closed_without_touching_outside(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            state = root / "state"
            outside = root / "outside"
            outside.mkdir(mode=0o755)
            paths = LedgerPaths.derive(state, "ws_alpha")
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == "llm-collabd" and dir_fd is not None and not swapped:
                    swapped = True
                    os.rmdir(path, dir_fd=dir_fd)
                    os.symlink(outside, path, dir_fd=dir_fd)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("llm_collab.ledger.paths.os.open", side_effect=racing_open):
                with self.assertRaises(ValueError):
                    paths.ensure_directories()

            self.assertTrue(swapped)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertTrue((state / "llm-collabd").is_symlink())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_post_open_edge_swap_is_detected_by_final_inode_revalidation(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir(mode=0o755)
            paths = LedgerPaths.derive(root / "state", "ws_alpha")
            real_stat = os.stat
            swapped = False

            def racing_stat(path, *, dir_fd=None, follow_symlinks=True):
                nonlocal swapped
                if path == "logs" and dir_fd is not None and not follow_symlinks and not swapped:
                    swapped = True
                    os.rmdir(path, dir_fd=dir_fd)
                    os.symlink(outside, path, dir_fd=dir_fd)
                return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

            with patch("llm_collab.ledger.paths.os.stat", side_effect=racing_stat):
                with self.assertRaisesRegex(ValueError, "changed during directory walk"):
                    paths.ensure_directories()

            self.assertTrue(swapped)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertTrue(paths.logs.is_symlink())

    def test_child_directory_creation_is_dirfd_anchored_and_no_follow(self) -> None:
        source = (
            inspect.getsource(LedgerPaths.ensure_directories)
            + inspect.getsource(LedgerPaths._ensure_child_directory)
            + inspect.getsource(LedgerPaths._revalidate_child)
        )
        self.assertIn("dir_fd=", source)
        self.assertIn("os.O_NOFOLLOW", source)
        self.assertIn("os.fchmod", source)
        self.assertIn("st_dev", source)
        self.assertIn("st_ino", source)
        self.assertNotIn(".chmod(", source)

    def test_generated_ids_and_backup_names_are_valid_and_collision_specific(self) -> None:
        with patch.object(uuid, "uuid4", return_value=uuid.UUID(int=0)):
            first = generate_workspace_id()
        self.assertEqual(first, "ws_wAAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(len(first), 26)
        self.assertIsNotNone(re.fullmatch(r"ws_w[A-Za-z0-9_-]{22}", first))
        second = generate_workspace_id()
        self.assertEqual(validate_workspace_id(first), first)
        self.assertNotEqual(first, second)
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, first)
            self.assertEqual(
                paths.backup_path(1, "20260721T075500000000Z").name,
                "ledger-1-20260721T075500000000Z.sqlite3",
            )

    def test_portable_socket_path_byte_boundary_is_checked_before_filesystem_use(self) -> None:
        workspace_id = "ws_wAAAAAAAAAAAAAAAAAAAAAA"
        fixed_suffix = os.fsencode(f"/llm-collabd/{workspace_id}/daemon.sock")
        accepted_root = Path("/" + "p" * (MAX_UNIX_SOCKET_PATH_BYTES - len(fixed_suffix) - 1))
        rejected_root = Path(str(accepted_root) + "p")
        self.assertFalse(accepted_root.exists())
        self.assertFalse(rejected_root.exists())

        accepted = LedgerPaths.derive(accepted_root, workspace_id)
        self.assertEqual(len(os.fsencode(accepted.socket)), 103)
        with self.assertRaisesRegex(ValueError, "AF_UNIX socket path is 104 encoded bytes"):
            LedgerPaths.derive(rejected_root, workspace_id)
        self.assertFalse(accepted_root.exists())
        self.assertFalse(rejected_root.exists())


if __name__ == "__main__":
    unittest.main()
