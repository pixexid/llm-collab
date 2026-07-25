"""PM2 ecosystem config contract: the Codex app-server sidecar gate.

The Python suite never loads the CJS config, so the sidecar's token/binary gate
and its exact launch arguments would otherwise be unverified.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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


class Pm2ManagerSidecarTest(unittest.TestCase):
    """The manager's sidecar branch, forced on so a clean checkout still exercises it.

    Without forcing, enabled_sidecar_ids() returns [] wherever the real token or
    Codex binary is absent, so `--all` inclusion and the [sidecar] output would be
    covered only by accident on a machine that happens to have them.
    """

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT / "bin"))
        global pm2_watchers, _ax_trust
        import _ax_trust  # noqa: F401
        import pm2_watchers  # noqa: F811

        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        token = Path(self._tmp.name) / "token"
        token.write_text("t0ken\n", encoding="utf-8")
        binary = Path(self._tmp.name) / "codex"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.token, self.binary = token, binary

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_status_all(self):
        agents = [{"id": "codex", "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"}}]
        output = io.StringIO()
        patches = [
            mock.patch.object(sys, "argv", ["pm2_watchers.py", "status", "--all"]),
            mock.patch.dict(
                os.environ,
                {
                    "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(self.token),
                    "LLM_COLLAB_CODEX_BIN": str(self.binary),
                },
            ),
            mock.patch.object(pm2_watchers, "watcher_enabled_agents", return_value=agents),
            mock.patch.object(pm2_watchers, "get_agent", side_effect=lambda a: agents[0]),
            mock.patch.object(
                pm2_watchers, "probe_ax_trust", return_value=_ax_trust.AxTrustStatus("trusted")
            ),
            mock.patch.object(pm2_watchers, "config_get", return_value="llm-collab"),
            mock.patch.object(pm2_watchers, "pm2_run", side_effect=SystemExit(1)),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stdout(output):
                    pm2_watchers.main()
        return output.getvalue()

    def test_forced_sidecar_is_included_in_all_and_reports_without_an_ax_line(self) -> None:
        text = self._run_status_all()
        ax_lines = [line for line in text.splitlines() if line.startswith("[ax]")]
        sidecar_lines = [line for line in text.splitlines() if line.startswith("[sidecar]")]

        # one [ax] line per real agent, and the sidecar must not add one:
        # [ax] is the per-agent capability contract consumers parse.
        self.assertEqual(1, len(ax_lines), f"expected one [ax] line per agent, got: {ax_lines}")
        self.assertEqual(1, len(sidecar_lines), f"expected one [sidecar] line, got: {sidecar_lines}")
        self.assertIn("codex-appserver", sidecar_lines[0])
        self.assertIn("no AX surface", sidecar_lines[0])

    def test_forced_sidecar_is_a_pm2_target_and_addressable_directly(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(self.token),
                "LLM_COLLAB_CODEX_BIN": str(self.binary),
            },
        ):
            self.assertEqual(["codex-appserver"], pm2_watchers.enabled_sidecar_ids())
            self.assertTrue(pm2_watchers.is_sidecar("codex-appserver"))
            with mock.patch.object(pm2_watchers, "config_get", return_value="llm-collab"):
                self.assertEqual(
                    "llm-collab-codex-appserver", pm2_watchers.app_name("codex-appserver")
                )

    def test_absent_token_keeps_the_sidecar_out_of_manager_targets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(Path(self._tmp.name) / "absent"),
                "LLM_COLLAB_CODEX_BIN": str(self.binary),
            },
        ):
            self.assertEqual([], pm2_watchers.enabled_sidecar_ids())
