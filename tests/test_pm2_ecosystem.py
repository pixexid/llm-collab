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
import types
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


def load_apps(env_overrides: dict[str, str], *, fake_uid: int | None = None) -> list[dict]:
    env = {**os.environ, **env_overrides}
    script = LOAD if fake_uid is None else f"process.getuid = () => {fake_uid};" + LOAD
    result = subprocess.run(
        [NODE, "-e", script, str(CONFIG)],
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
            token.chmod(0o600)
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
            token.chmod(0o600)
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


    def test_group_or_world_readable_token_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            token = Path(tmp) / "token"
            token.write_text("t0ken\n", encoding="utf-8")
            token.chmod(0o644)  # another local account could read the bearer token
            binary = Path(tmp) / "codex"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            apps = load_apps(
                {
                    "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(token),
                    "LLM_COLLAB_CODEX_BIN": str(binary),
                }
            )
            self.assertEqual([], sidecars(apps))

    def test_token_path_containing_whitespace_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            directory = Path(tmp) / "has space"
            directory.mkdir()
            token = directory / "token"
            token.write_text("t0ken\n", encoding="utf-8")
            token.chmod(0o600)
            binary = Path(tmp) / "codex"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            apps = load_apps(
                {
                    "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(token),
                    "LLM_COLLAB_CODEX_BIN": str(binary),
                }
            )
            # delivery discovery parses flattened `ps` output and would truncate the
            # path, then connect with no token at all — refuse instead.
            self.assertEqual([], sidecars(apps))




    def test_cjs_uid_branch_emits_for_owner_and_refuses_a_foreign_uid(self) -> None:
        """Exercise the real CJS uid check, not only the Python mirror.

        Overriding process.getuid in the isolated node subprocess before require makes
        this directly testable; an earlier revision of this PR wrongly claimed it was
        not, and relied on the Python mirror alone.
        """
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            token = Path(tmp) / "token"
            token.write_text("t0ken\n", encoding="utf-8")
            token.chmod(0o600)
            binary = Path(tmp) / "codex"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            env = {
                "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(token),
                "LLM_COLLAB_CODEX_BIN": str(binary),
            }
            self.assertEqual(
                1, len(sidecars(load_apps(env, fake_uid=os.getuid()))),
                "owner uid must emit the sidecar",
            )
            self.assertEqual(
                [], sidecars(load_apps(env, fake_uid=os.getuid() + 1)),
                "a foreign-owned token must be refused by the CJS gate",
            )

    def test_cjs_reserves_the_sidecar_id_against_a_registered_agent(self) -> None:
        """A registered codex-appserver agent must not yield two apps with one name.

        Loaded against a temp fixture tree: the real agents.json is the authoritative
        registry that live watchers and commands read concurrently, and rewriting it
        in place would expose a truncated file mid-write and could leave the shared
        checkout altered if the process died before restoring.
        """
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            tree = Path(tmp)
            (tree / "pm2").mkdir()
            # copy the real config so the assertion is against production logic
            (tree / "pm2" / "ecosystem.config.cjs").write_text(
                CONFIG.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (tree / "collab.config.json").write_text(
                json.dumps({"workspace_name": "fixture"}), encoding="utf-8"
            )
            (tree / "agents.json").write_text(
                json.dumps(
                    {
                        "agents": [
                            {"id": "codex", "activation": {"type": "cli_session", "watcher_enabled": True}},
                            {"id": "codex-appserver", "activation": {"type": "cli_session", "watcher_enabled": True}},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            token = tree / "token"
            token.write_text("t0ken\n", encoding="utf-8")
            token.chmod(0o600)
            binary = tree / "codex"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            result = subprocess.run(
                [NODE, "-e", LOAD, str(tree / "pm2" / "ecosystem.config.cjs")],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(tree),
                env={
                    **os.environ,
                    "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(token),
                    "LLM_COLLAB_CODEX_BIN": str(binary),
                },
            )
            self.assertEqual(0, result.returncode, result.stderr[:400])
            apps = json.loads(result.stdout)
            names = [a["name"] for a in apps if a["name"].endswith(SIDECAR_SUFFIX)]
            self.assertEqual(len(names), len(set(names)), f"duplicate PM2 app name: {names}")
            self.assertIn(
                "reserved transport",
                result.stderr,
                "the conflict must be reported, not silently skipped",
            )
            # the real registry must be untouched by this test
            self.assertNotIn(
                "codex-appserver",
                [a.get("id") for a in json.loads((ROOT / "agents.json").read_text())["agents"]],
            )


    def test_non_posix_host_disables_the_sidecar_without_crashing_the_config(self) -> None:
        """process.getuid is POSIX-only; on Windows it is undefined.

        Calling it unguarded threw during module evaluation, which would stop the
        ENTIRE ecosystem from loading — unrelated inbox watchers included — rather
        than merely disabling an unsupported sidecar.
        """
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            token = Path(tmp) / "token"
            token.write_text("t0ken\n", encoding="utf-8")
            token.chmod(0o600)
            binary = Path(tmp) / "codex"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            env = {
                "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(token),
                "LLM_COLLAB_CODEX_BIN": str(binary),
            }
            # simulate a host with no getuid, as Node presents on Windows
            script = "delete process.getuid;" + LOAD
            result = subprocess.run(
                [NODE, "-e", script, str(CONFIG)],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, **env}, cwd=str(ROOT),
            )
            self.assertEqual(0, result.returncode,
                             f"config must still load on a non-POSIX host: {result.stderr[:300]}")
            apps = json.loads(result.stdout)
            self.assertEqual([], sidecars(apps), "sidecar must be disabled, not crash")
            self.assertTrue(apps, "unrelated watcher apps must still be emitted")

    def test_reservation_covers_agents_that_are_not_watcher_enabled(self) -> None:
        """A collaborator owns its id regardless of watcher_enabled.

        Filtering only watcherAgents let an agent registered with
        watcher_enabled:false slip through, so the direct ecosystem path would start
        the transport under that collaborator's PM2 identity.
        """
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            tree = Path(tmp)
            (tree / "pm2").mkdir()
            (tree / "pm2" / "ecosystem.config.cjs").write_text(
                CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            (tree / "collab.config.json").write_text(
                json.dumps({"workspace_name": "fixture"}), encoding="utf-8")
            (tree / "agents.json").write_text(json.dumps({"agents": [
                {"id": "codex", "activation": {"type": "cli_session", "watcher_enabled": True}},
                # NOT watcher-enabled, so it never appears in watcherAgents
                {"id": "codex-appserver", "activation": {"type": "cli_session", "watcher_enabled": False}},
            ]}), encoding="utf-8")
            token = tree / "token"; token.write_text("t\n", encoding="utf-8"); token.chmod(0o600)
            binary = tree / "codex"; binary.write_text("#!/bin/sh\n", encoding="utf-8")
            result = subprocess.run(
                [NODE, "-e", LOAD, str(tree / "pm2" / "ecosystem.config.cjs")],
                capture_output=True, text=True, timeout=30, cwd=str(tree),
                env={**os.environ,
                     "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(token),
                     "LLM_COLLAB_CODEX_BIN": str(binary)})
            self.assertEqual(0, result.returncode, result.stderr[:300])
            self.assertEqual([], sidecars(json.loads(result.stdout)),
                             "a reserved id held by any registered agent must block the sidecar")

    def test_relative_token_override_is_resolved_before_validation(self) -> None:
        """Validation and launch must see the same path.

        A relative override was validated against the invoker's cwd while the spawned
        app runs with cwd: root, so the gate could approve one file while Codex opened
        another.
        """
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            tree = Path(tmp)
            (tree / "pm2").mkdir()
            (tree / "pm2" / "ecosystem.config.cjs").write_text(
                CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            (tree / "collab.config.json").write_text(
                json.dumps({"workspace_name": "fixture"}), encoding="utf-8")
            (tree / "agents.json").write_text(json.dumps({"agents": [
                {"id": "codex", "activation": {"type": "cli_session", "watcher_enabled": True}}]}),
                encoding="utf-8")
            secure = tree / "secure-token"
            secure.write_text("t\n", encoding="utf-8"); secure.chmod(0o600)
            binary = tree / "codex"; binary.write_text("#!/bin/sh\n", encoding="utf-8")
            # run from a DIFFERENT cwd so a cwd-relative reading would miss the file
            elsewhere = tree / "elsewhere"; elsewhere.mkdir()
            result = subprocess.run(
                [NODE, "-e", LOAD, str(tree / "pm2" / "ecosystem.config.cjs")],
                capture_output=True, text=True, timeout=30, cwd=str(elsewhere),
                env={**os.environ,
                     "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": "secure-token",
                     "LLM_COLLAB_CODEX_BIN": str(binary)})
            self.assertEqual(0, result.returncode, result.stderr[:300])
            found = sidecars(json.loads(result.stdout))
            self.assertEqual(1, len(found), "the override must resolve against root")
            passed = found[0]["args"][found[0]["args"].index("--ws-token-file") + 1]
            self.assertEqual(os.path.realpath(secure), os.path.realpath(passed),
                             "the launched path must be the validated one")

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
        token.chmod(0o600)
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

    def test_foreign_owned_token_is_refused_by_the_manager_gate(self) -> None:
        # The CJS uid check cannot be exercised without a file owned by another
        # account, which a test cannot create unprivileged. The Python mirror is
        # structurally identical and IS testable, so prove the rule here.
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assertFalse(pm2_watchers.sidecar_token_is_secure(self.token))

    def test_owner_only_token_passes_the_manager_gate(self) -> None:
        self.assertTrue(pm2_watchers.sidecar_token_is_secure(self.token))

    def test_reserved_sidecar_id_never_shadows_a_registered_agent(self) -> None:
        # A real collaborator named codex-appserver must win: otherwise its AX report
        # is suppressed, its watcher flag bypassed, and two PM2 apps share one name.
        with mock.patch.object(pm2_watchers, "agent_ids", return_value=["codex", "codex-appserver"]):
            self.assertFalse(pm2_watchers.is_sidecar("codex-appserver"))
            self.assertEqual(["codex-appserver"], pm2_watchers.sidecar_id_conflicts())
        with mock.patch.object(pm2_watchers, "agent_ids", return_value=["codex"]):
            self.assertTrue(pm2_watchers.is_sidecar("codex-appserver"))
            self.assertEqual([], pm2_watchers.sidecar_id_conflicts())

    def test_cleanup_commands_reach_a_sidecar_whose_token_was_removed(self) -> None:
        """Removing the token must not orphan a running server.

        enabled_sidecar_ids() gates creation, so stop/delete/status/logs/restart have
        to use sidecar_ids_for_command() or `--all` silently omits a live process.
        """
        with mock.patch.dict(
            os.environ,
            {
                "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(Path(self._tmp.name) / "gone"),
                "LLM_COLLAB_CODEX_BIN": str(self.binary),
            },
        ):
            self.assertEqual([], pm2_watchers.enabled_sidecar_ids(),
                             "start must be gated once the token is gone")
            with mock.patch.object(pm2_watchers, "sidecar_is_pm2_registered", return_value=True):
                for command in ("stop", "delete", "status", "logs"):
                    self.assertEqual(
                        ["codex-appserver"], pm2_watchers.sidecar_ids_for_command(command),
                        f"{command} --all must still reach a PM2-registered orphan",
                    )
                # restart relaunches, so it is gated even for a registered orphan
                self.assertEqual([], pm2_watchers.sidecar_ids_for_command("restart"))
            for command in ("start", "ensure"):
                self.assertEqual(
                    [], pm2_watchers.sidecar_ids_for_command(command),
                    f"{command} must stay gated",
                )

    def test_never_enabled_install_gets_no_phantom_sidecar_target(self) -> None:
        """Cleanup must reach an orphan without inventing one.

        Unconditionally targeting the reserved id made `status --all` report a sidecar
        that never existed and exit non-zero on a plain install.
        """
        with mock.patch.dict(
            os.environ,
            {
                "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(Path(self._tmp.name) / "never"),
                "LLM_COLLAB_CODEX_BIN": str(Path(self._tmp.name) / "no-codex"),
            },
        ):
            with mock.patch.object(pm2_watchers, "sidecar_is_pm2_registered", return_value=False):
                for command in ("stop", "delete", "status", "logs", "restart", "start", "ensure"):
                    self.assertEqual(
                        [], pm2_watchers.sidecar_ids_for_command(command),
                        f"{command} must not target a sidecar that never existed",
                    )

    def test_missing_getuid_fails_closed_without_raising(self) -> None:
        """os.getuid is POSIX-only; the manager must not crash where it is absent.

        Uses a stub module rather than deleting the attribute from the real `os`, which
        would mutate global state for every other test in the process.
        """
        posixless = types.SimpleNamespace(stat=os.stat)  # no getuid, as on Windows
        with mock.patch.object(pm2_watchers, "os", posixless):
            self.assertFalse(
                pm2_watchers.sidecar_token_is_secure(self.token),
                "an absent getuid must fail closed, not raise",
            )

    def test_ax_lines_print_before_any_pm2_backed_sidecar_discovery(self) -> None:
        """A PM2 failure must never suppress a collaborator's AX report.

        Resolving sidecar orphans probes PM2, so doing it during target resolution
        meant `status --all` died before printing any AX line. A valid token on the
        developer's own checkout short-circuited the probe and masked this entirely.
        """
        agents = [
            {"id": "codex", "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"}},
            {"id": "operator", "activation": {"type": "human_relay", "watcher_enabled": True}},
        ]
        out = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(Path(self._tmp.name) / "absent")},
        ):
            with contextlib.ExitStack() as stack:
                for patch in (
                    mock.patch.object(sys, "argv", ["pm2_watchers.py", "status", "--all"]),
                    mock.patch.object(pm2_watchers, "watcher_enabled_agents", return_value=agents),
                    mock.patch.object(pm2_watchers, "get_agent",
                                      side_effect=lambda a: next(x for x in agents if x["id"] == a)),
                    mock.patch.object(pm2_watchers, "probe_ax_trust",
                                      return_value=_ax_trust.AxTrustStatus("trusted")),
                    mock.patch.object(pm2_watchers, "config_get", return_value="llm-collab"),
                    mock.patch.object(pm2_watchers, "pm2_run", side_effect=SystemExit(1)),
                ):
                    stack.enter_context(patch)
                with self.assertRaises(SystemExit):
                    with contextlib.redirect_stdout(out):
                        pm2_watchers.main()
        ax = [l for l in out.getvalue().splitlines() if l.startswith("[ax]")]
        self.assertEqual(2, len(ax), f"one AX line per agent must precede PM2: {ax}")

    def test_restart_requires_the_security_gate(self) -> None:
        """restart relaunches, so it must not bypass the token gate.

        Classifying it as non-creating let a sidecar whose token became group- or
        world-readable be restarted with that insecure token still in place.
        """
        self.assertNotIn("restart", pm2_watchers.NON_CREATING_COMMANDS)
        self.token.chmod(0o644)  # now insecure
        with mock.patch.dict(os.environ, {
            "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(self.token),
            "LLM_COLLAB_CODEX_BIN": str(self.binary),
        }):
            self.assertEqual([], pm2_watchers.sidecar_ids_for_command("restart"),
                             "an insecure token must not be restartable")

    def test_direct_sidecar_start_is_refused_when_not_enabled(self) -> None:
        """A direct --agent target bypassed the gate and exited 0 on failure."""
        with mock.patch.dict(os.environ, {
            "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(Path(self._tmp.name) / "absent"),
            "LLM_COLLAB_CODEX_BIN": str(self.binary),
        }):
            for command in ("start", "ensure", "restart"):
                with mock.patch.object(sys, "argv",
                                       ["pm2_watchers.py", command, "--agent", "codex-appserver"]):
                    with mock.patch.object(pm2_watchers, "config_get", return_value="llm-collab"):
                        with mock.patch.object(pm2_watchers, "pm2_run") as ran:
                            with self.assertRaises(SystemExit) as caught:
                                with contextlib.redirect_stdout(io.StringIO()):
                                    pm2_watchers.main()
                            self.assertEqual(2, caught.exception.code, command)
                            ran.assert_not_called()

    def test_absent_token_keeps_the_sidecar_out_of_manager_targets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE": str(Path(self._tmp.name) / "absent"),
                "LLM_COLLAB_CODEX_BIN": str(self.binary),
            },
        ):
            self.assertEqual([], pm2_watchers.enabled_sidecar_ids())


if __name__ == "__main__":
    unittest.main()
