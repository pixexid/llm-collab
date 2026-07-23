import ast
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from llm_collab.codex_runtime_home import RuntimeHomeError, bind_runtime_home


MODULE = Path("llm_collab/codex_runtime_home.py")


class CodexRuntimeHomeTests(unittest.TestCase):
    def test_binds_realpath_and_derived_id_from_held_directory(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            home = Path(tmp) / "codex-home"
            home.mkdir()
            identity = bind_runtime_home(home)
            expected_realpath = os.path.realpath(home)
            self.assertEqual(expected_realpath, identity.runtime_home_realpath)
            self.assertEqual(
                hashlib.sha256(expected_realpath.encode("utf-8")).hexdigest(),
                identity.runtime_home_id,
            )

            self.assertEqual(
                identity,
                bind_runtime_home(
                    home,
                    expected_realpath=identity.runtime_home_realpath,
                    expected_id=identity.runtime_home_id,
                ),
            )

    def test_caller_realpath_and_id_are_checks_not_authority(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            identity = bind_runtime_home(home)
            with self.assertRaisesRegex(RuntimeHomeError, "realpath mismatch"):
                bind_runtime_home(home, expected_realpath=identity.runtime_home_realpath[:-1])
            with self.assertRaisesRegex(RuntimeHomeError, "id mismatch"):
                bind_runtime_home(home, expected_id="0" * 64)

    def test_rejects_missing_relative_null_and_non_directory_inputs(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            with self.assertRaisesRegex(RuntimeHomeError, "absolute"):
                bind_runtime_home("")
            with self.assertRaisesRegex(RuntimeHomeError, "absolute"):
                bind_runtime_home(None)  # type: ignore[arg-type]
            with self.assertRaisesRegex(RuntimeHomeError, "absolute"):
                bind_runtime_home("latest")
            with self.assertRaisesRegex(RuntimeHomeError, "absolute"):
                bind_runtime_home("codex")
            with self.assertRaises(RuntimeHomeError):
                bind_runtime_home(Path(tmp) / "missing")
            file_path = Path(tmp) / "file"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(RuntimeHomeError):
                bind_runtime_home(file_path)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_swap_after_open_binds_held_target_not_re_resolved_string(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            a = root / "a"
            b = root / "b"
            a.mkdir()
            b.mkdir()
            link = root / "CODEX_HOME"
            link.symlink_to(a, target_is_directory=True)
            real_open = os.open
            swapped = False

            def open_then_swap(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                fd = real_open(path, flags, mode, dir_fd=dir_fd)
                if path == os.fspath(link) and not swapped:
                    swapped = True
                    link.unlink()
                    link.symlink_to(b, target_is_directory=True)
                return fd

            with patch("llm_collab.codex_runtime_home.os.open", side_effect=open_then_swap):
                identity = bind_runtime_home(link)

            expected_a = os.path.realpath(a)
            self.assertTrue(swapped)
            self.assertEqual(expected_a, identity.runtime_home_realpath)
            self.assertEqual(hashlib.sha256(expected_a.encode("utf-8")).hexdigest(), identity.runtime_home_id)
            self.assertEqual(os.path.realpath(link), os.path.realpath(b))

    def test_module_has_no_live_connection_or_sessionref_surface(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        banned_imports = {"socket", "subprocess", "urllib", "requests", "websocket"}
        literals = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], banned_imports)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
        self.assertFalse(any(value.startswith("turn/") for value in literals))
        self.assertNotIn("thread/resume", literals)
        self.assertNotIn("SessionRefV1", literals)


if __name__ == "__main__":
    unittest.main()
