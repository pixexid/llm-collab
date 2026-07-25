"""PM2 ecosystem config contract: the Codex app-server sidecar gate.

The Python suite never loads the CJS config, so the sidecar's token/binary gate
and its exact launch arguments would otherwise be unverified.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "pm2" / "ecosystem.config.cjs"
NODE = shutil.which("node")

LOAD = (
    "const c = require(process.argv[1]);"
    "process.stdout.write(JSON.stringify(c.apps.map("
    "a => ({name: a.name, script: a.script, args: a.args, env: a.env}))));"
)


def load_apps(env_overrides: dict[str, str]) -> list[dict]:
    env = {**os.environ, **env_overrides}
    result = subprocess.run(
        [NODE, "-e", LOAD, str(CONFIG)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise AssertionError(f"config failed to load: {result.stderr[:400]}")
    return json.loads(result.stdout)


SIDECAR_SUFFIX = "-codex-appserver"


def sidecars(apps: list[dict]) -> list[dict]:
    return [a for a in apps if a["name"].endswith(SIDECAR_SUFFIX)]


@unittest.skipIf(NODE is None, "node is required to load the CJS ecosystem config")
class Pm2EcosystemTest(unittest.TestCase):
    def test_missing_token_file_leaves_the_app_list_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            binary = Path(tmp) / "codex"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            apps = load_apps(
                {
                    "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(Path(tmp) / "absent-token"),
                    "LLM_COLLAB_CODEX_BIN": str(binary),
                }
            )
            self.assertEqual([], sidecars(apps))
            self.assertTrue(apps, "watcher apps must still be emitted")

    def test_missing_binary_leaves_the_app_list_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            token = Path(tmp) / "token"
            token.write_text("t0ken\n", encoding="utf-8")
            apps = load_apps(
                {
                    "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(token),
                    "LLM_COLLAB_CODEX_BIN": str(Path(tmp) / "absent-codex"),
                }
            )
            self.assertEqual([], sidecars(apps))

    def test_token_and_binary_emit_the_exact_sidecar_configuration(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            token = Path(tmp) / "token"
            token.write_text("t0ken\n", encoding="utf-8")
            binary = Path(tmp) / "codex"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            apps = load_apps(
                {
                    "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(token),
                    "LLM_COLLAB_CODEX_BIN": str(binary),
                    "LLM_COLLAB_CODEX_HOME": str(codex_home),
                    "LLM_COLLAB_CODEX_APP_SERVER_PORT": "8791",
                }
            )
            found = sidecars(apps)
            self.assertEqual(1, len(found), "exactly one sidecar app expected")
            app = found[0]
            self.assertEqual(str(binary), app["script"])
            self.assertEqual(
                [
                    "app-server",
                    "--listen", "ws://127.0.0.1:8791",
                    "--ws-auth", "capability-token",
                    "--ws-token-file", str(token),
                ],
                app["args"],
            )
            # localhost-only: a remote bind would expose an unauthenticated-by-default
            # control surface for the operator's real Codex account.
            self.assertIn("ws://127.0.0.1:", app["args"][app["args"].index("--listen") + 1])
            self.assertEqual(str(codex_home), app["env"]["CODEX_HOME"])


if __name__ == "__main__":
    unittest.main()
