from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import pm2_watchers


def _jlist_entry(name: str, status: str, **env_extra) -> dict:
    """One `pm2 jlist` entry with pm2_env.status set, plus arbitrary env fields."""
    return {
        "name": name,
        "pm2_env": {"status": status, **env_extra},
        "pm_id": 0,
        "pid": 0,
        "monit": {},
    }


def _jlist(*entries: dict) -> str:
    return json.dumps(list(entries))


# Concrete fail-open fixture for the secondary item: an entry whose real
# pm2_env.status is errored, but "online" appears in operator-chosen fields --
# the app name (agent id), script args (--me/--project), cwd and exec/log paths
# (runtime home). A project named "online-store" produces exactly this.
# Text-matching forms reported this dead process as live; the structured read
# takes pm2_env.status, so those fields are irrelevant.
_DIVERGENT_JLIST = _jlist(_jlist_entry(
    "llm-collab-online-store",
    "errored",
    args=["--me", "online-store", "--project", "online-store", "--repo-target", "app"],
    pm_cwd="/srv/online-home",
    pm_exec_path="/srv/online-home/runtime/bin/watch_inbox.py",
    pm_out_log_path="/srv/online-home/.pm2/logs/online-store-out.log",
    pm_err_log_path="/srv/online-home/.pm2/logs/online-store-error.log",
))


class PM2WatchersTest(unittest.TestCase):
    def test_ecosystem_comes_from_runtime_root(self) -> None:
        with patch.object(pm2_watchers, "RUNTIME_ROOT", Path("/deployed/runtime")):
            self.assertEqual(
                Path("/deployed/runtime/pm2/ecosystem.config.cjs"),
                pm2_watchers.ecosystem_path(),
            )

    def test_pm2_run_exits_when_pm2_times_out(self) -> None:
        with patch.object(pm2_watchers, "resolve_pm2", return_value="/usr/local/bin/pm2"):
            with patch.object(
                pm2_watchers.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["pm2", "describe", "llm-collab-codex"], 15),
            ):
                with self.assertRaises(SystemExit) as context:
                    pm2_watchers.pm2_run(["describe", "llm-collab-codex"])

        self.assertEqual(context.exception.code, 124)

    def test_logs_command_requests_non_streaming_pm2_output(self) -> None:
        calls: list[list[str]] = []

        def fake_pm2_run(args_list: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess:
            calls.append(args_list)
            return subprocess.CompletedProcess(args=args_list, returncode=0)

        with patch.object(sys, "argv", ["pm2_watchers.py", "logs", "--agent", "codex", "--lines", "7"]):
            with patch.object(pm2_watchers, "agent_ids", return_value=["codex"]):
                with patch.object(pm2_watchers, "config_get", return_value="llm-collab"):
                    with patch.object(pm2_watchers, "pm2_run", side_effect=fake_pm2_run):
                        pm2_watchers.main()

        self.assertEqual(calls, [["logs", "llm-collab-codex", "--lines", "7", "--nostream"]])

    def test_process_status_preserves_failure_and_requires_online(self) -> None:
        # status is read structurally from `pm2 jlist` (pm2_env.status), not
        # matched out of rendered text. A missing app reads as not-online (1),
        # not as a pm2 error code, because jlist succeeds and simply omits it.
        cases = (
            ("online", _jlist(_jlist_entry("llm-collab-codex", "online")), 0),
            ("stopped", _jlist(_jlist_entry("llm-collab-codex", "stopped")), 1),
            ("errored", _jlist(_jlist_entry("llm-collab-codex", "errored")), 1),
            ("missing", _jlist(_jlist_entry("llm-collab-someone-else", "online")), 1),
            ("pm2_error", None, 9),
        )
        for label, stdout, expected in cases:
            with self.subTest(label):
                result = subprocess.CompletedProcess(
                    args=[],
                    returncode=(9 if stdout is None else 0),
                    stdout=(stdout or ""),
                    stderr=("boom" if stdout is None else ""),
                )
                with patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                     patch.object(pm2_watchers, "pm2_run", return_value=result):
                    self.assertEqual(expected, pm2_watchers.process_status_exit_code("codex"))

    def _restart_env(self, start_or_restart_returncode: int, jlist_stdout: str):
        """pm2_run that answers startOrRestart then jlist for `restart --agent`."""
        def fake(args_list, *, capture_output=False, max_output_bytes=None):
            if args_list[0] == "startOrRestart":
                return subprocess.CompletedProcess(args=args_list, returncode=start_or_restart_returncode)
            if args_list[0] == "jlist":
                return subprocess.CompletedProcess(args=args_list, returncode=0, stdout=jlist_stdout, stderr="")
            return subprocess.CompletedProcess(args=args_list, returncode=0)
        return fake

    def test_restart_propagates_pm2_exit_code_when_start_or_restart_fails(self) -> None:
        # pm2_run returns a CompletedProcess on a non-zero PM2 exit; the restart
        # branch used to discard it and fall through to exit 0.
        online = _jlist(_jlist_entry("llm-collab-codex", "online"))
        with patch.object(sys, "argv", ["pm2_watchers.py", "restart", "--agent", "codex"]):
            with patch.object(pm2_watchers, "agent_ids", return_value=["codex"]), \
                 patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                 patch.object(pm2_watchers, "pm2_run", side_effect=self._restart_env(7, online)):
                with self.assertRaises(SystemExit) as ctx:
                    pm2_watchers.main()
        self.assertEqual(7, ctx.exception.code)

    def test_restart_verifies_online_after_pm2_accepts(self) -> None:
        # startOrRestart exits 0 but the process lands errored. The discarded
        # return value plus no post-check let restart exit 0, so start_watcher
        # reported ok for a watcher that was not running (GH-678).
        errored = _jlist(_jlist_entry("llm-collab-codex", "errored"))
        with patch.object(sys, "argv", ["pm2_watchers.py", "restart", "--agent", "codex"]):
            with patch.object(pm2_watchers, "agent_ids", return_value=["codex"]), \
                 patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                 patch.object(pm2_watchers, "pm2_run", side_effect=self._restart_env(0, errored)):
                with self.assertRaises(SystemExit) as ctx:
                    pm2_watchers.main()
        self.assertNotEqual(0, ctx.exception.code)

    def test_restart_returns_zero_when_online(self) -> None:
        # A healthy restart must still fall through to exit 0.
        online = _jlist(_jlist_entry("llm-collab-codex", "online"))
        with patch.object(sys, "argv", ["pm2_watchers.py", "restart", "--agent", "codex"]):
            with patch.object(pm2_watchers, "agent_ids", return_value=["codex"]), \
                 patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                 patch.object(pm2_watchers, "pm2_run", side_effect=self._restart_env(0, online)):
                pm2_watchers.main()  # must not raise SystemExit

    def test_watcher_status_reads_pm2_env_status_not_any_text(self) -> None:
        # The authority. Status is errored; "online" appears in the app name,
        # script args, cwd and log paths -- all operator-chosen. The read takes
        # pm2_env.status, so it is not online. This is what stops any text match
        # being reintroduced.
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=_DIVERGENT_JLIST, stderr="")
        with patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
             patch.object(pm2_watchers, "pm2_run", return_value=result):
            _, status = pm2_watchers.watcher_status("online-store")
        self.assertEqual("errored", status)

    def test_watcher_status_ignores_literal_status_online_in_other_fields(self) -> None:
        # The review finding (PR #680): the row/field regex had a colon
        # alternative, so the literal "status: online" appearing in ANY echoed
        # field satisfied it while the real status row said errored. The
        # structured read takes pm2_env.status, so a field value of
        # "status: online" cannot satisfy it.
        jlist = _jlist(_jlist_entry("llm-collab-codex", "errored", args=["--project", "status: online"]))
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=jlist, stderr="")
        with patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
             patch.object(pm2_watchers, "pm2_run", return_value=result):
            _, status = pm2_watchers.watcher_status("codex")
        self.assertEqual("errored", status)

    def test_start_agent_treats_online_in_other_fields_as_not_online(self) -> None:
        # The named consumer (was start_agent:376). pm2 start succeeds (exit 0),
        # but the post-start status is errored with "online" in other fields.
        # start_agent must still exit non-zero -- never report a started watcher.
        # The authority being correct does not prove a caller routes through it;
        # that gap is how the original defect survived.
        def fake(args_list, *, capture_output=False, max_output_bytes=None):
            if args_list[0] == "jlist":
                return subprocess.CompletedProcess(args=args_list, returncode=0, stdout=_DIVERGENT_JLIST, stderr="")
            return subprocess.CompletedProcess(args=args_list, returncode=0)
        with patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
             patch.object(pm2_watchers, "is_sidecar", return_value=False), \
             patch.object(pm2_watchers, "get_agent", return_value={"activation": {"watcher_enabled": True}}), \
             patch.object(pm2_watchers, "pm2_run", side_effect=fake):
            with self.assertRaises(SystemExit) as ctx:
                pm2_watchers.start_agent("online-store")
        self.assertNotEqual(0, ctx.exception.code)

    def test_watcher_status_bounds_the_jlist_read(self) -> None:
        # The call site must request the bound: a status read that silently fell
        # back to the unbounded path would reintroduce the finding. Pinning the
        # kwarg is what stops that, complementing the enforcement test below.
        with patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
             patch.object(
                 pm2_watchers,
                 "pm2_run",
                 return_value=subprocess.CompletedProcess([], 0, "[]", ""),
             ) as run:
            pm2_watchers.watcher_status("codex")
        self.assertIsNotNone(run.call_args.kwargs.get("max_output_bytes"))

    def test_pm2_run_bounded_fails_closed_on_oversized_output(self) -> None:
        # A real subprocess producing more than the bound (mirrors deploy_runtime's
        # _pm2_run_bounded test). Must abort, never return a buffer to parse -- a
        # truncated jlist that still parsed would report a real watcher as absent.
        with patch.object(pm2_watchers, "resolve_pm2", return_value=sys.executable):
            with self.assertRaises(SystemExit):
                pm2_watchers.pm2_run(
                    ["-c", "import sys; sys.stdout.write('x' * 128)"],
                    max_output_bytes=16,
                )

    def test_pm2_run_bounded_post_eof_wait_timeout_routes_through_timeout_exit(self) -> None:
        # Item 1 (GH-682). PM2 closes stdout after emitting output then hangs
        # before exiting: EOF breaks the read loop, then process.wait() raises
        # subprocess.TimeoutExpired. `except OSError` does not cover it
        # (TimeoutExpired is a subprocess.SubprocessError, not an OSError), so
        # without the dedicated branch this is a bare traceback + exit 1. It must
        # route through the timeout diagnostic + exit 124, restoring parity with
        # deploy_runtime.py's _pm2_run_bounded (which catches SubprocessError).
        #
        # MUTATION PROOF: delete the `except subprocess.TimeoutExpired` branch and
        # process.wait()'s TimeoutExpired propagates out of _pm2_run_bounded
        # instead of raising SystemExit; `assertRaises(SystemExit)` then sees a
        # TimeoutExpired (not SystemExit) and the test ERRORS. exit code 124 and
        # the "timed out" diagnostic both fail to appear.
        buf = io.StringIO()
        # Emit, close the stdout fd at the OS level (EOF to the parent), then
        # outlive the 1s deadline without exiting.
        script = (
            "import sys,time,os; sys.stdout.write('ok'); sys.stdout.flush();"
            " os.close(1); time.sleep(30)"
        )
        with patch.dict(pm2_watchers.os.environ, {"LLM_COLLAB_PM2_TIMEOUT_SECONDS": "1"}), \
             patch.object(pm2_watchers, "resolve_pm2", return_value=sys.executable), \
             contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                pm2_watchers.pm2_run(["-c", script], max_output_bytes=64)
        self.assertEqual(124, ctx.exception.code)
        self.assertIn("timed out", buf.getvalue())

    def test_status_all_reads_one_bounded_jlist_snapshot_for_the_batch(self) -> None:
        # Item 2 (GH-682). `status --all` must read `pm2 jlist` ONCE for the whole
        # batch under a single cumulative bound, not once per target. Per-call
        # bounding is not cumulative across one run (AGENTS.md "Bounded work fails
        # closed").
        #
        # MUTATION PROOF (answers: could this pass if each agent got a fresh 16
        # MiB?): NO. In that world `watcher_status` is called once per target and
        # each call reads jlist, so jlist is invoked N times; this asserts exactly
        # one call, so it fails for N>=2. The bound on that one call is also
        # pinned, so a truncating/unbounded batch read cannot slip in here.
        jlist_calls: list[int | None] = []

        def fake_pm2_run(args_list, *, capture_output=False, max_output_bytes=None):
            if args_list[0] == "jlist":
                jlist_calls.append(max_output_bytes)
                return subprocess.CompletedProcess(args=args_list, returncode=0, stdout="[]", stderr="")
            return subprocess.CompletedProcess(args=args_list, returncode=0)

        agents = [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]
        with patch.object(sys, "argv", ["pm2_watchers.py", "status", "--all"]), \
             contextlib.redirect_stdout(io.StringIO()):
            with patch.object(pm2_watchers, "watcher_enabled_agents", return_value=agents), \
                 patch.object(pm2_watchers, "agent_ids", return_value=["alpha", "beta", "gamma"]), \
                 patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                 patch.object(pm2_watchers, "is_sidecar", return_value=False), \
                 patch.object(pm2_watchers, "sidecar_ids_for_command", return_value=[]), \
                 patch.object(pm2_watchers, "format_ax_status", return_value=""), \
                 patch.object(pm2_watchers, "probe_ax_trust", return_value=None), \
                 patch.object(pm2_watchers, "get_agent", return_value={}), \
                 patch.object(pm2_watchers, "pm2_run", side_effect=fake_pm2_run):
                with self.assertRaises(SystemExit):
                    pm2_watchers.main()
        self.assertEqual(1, len(jlist_calls))
        self.assertEqual(pm2_watchers.PM2_JLIST_MAX_BYTES, jlist_calls[0])

    def test_status_all_oversized_snapshot_raises_rather_than_truncating(self) -> None:
        # Item 2 (GH-682). The batch's single bounded read must ABORT on exceed,
        # never hand a truncated buffer to json.loads -- a truncated jlist that
        # still parses reports a real watcher as absent (fail-open, GH-678).
        #
        # The jlist call is routed through the REAL _pm2_run_bounded on a
        # subprocess that emits past the (tiny, patched) bound, so the abort is
        # genuine, not a stub. Discriminator vs a truncating batch: the abort
        # prints "exceeds" to stderr and prints NO status line (it raises before
        # the target loop); a truncating impl would silently parse a partial
        # table and print "not found".
        def fake_pm2_run(args_list, *, capture_output=False, max_output_bytes=None):
            if args_list[0] == "jlist":
                return pm2_watchers._pm2_run_bounded(
                    sys.executable, ["-c", "import sys; sys.stdout.write('x' * 128)"], 16
                )
            return subprocess.CompletedProcess(args=args_list, returncode=0)

        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["pm2_watchers.py", "status", "--agent", "codex"]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with patch.object(pm2_watchers, "agent_ids", return_value=["codex"]), \
                 patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                 patch.object(pm2_watchers, "is_sidecar", return_value=False), \
                 patch.object(pm2_watchers, "format_ax_status", return_value=""), \
                 patch.object(pm2_watchers, "probe_ax_trust", return_value=None), \
                 patch.object(pm2_watchers, "get_agent", return_value={}), \
                 patch.object(pm2_watchers, "pm2_run", side_effect=fake_pm2_run):
                with self.assertRaises(SystemExit) as ctx:
                    pm2_watchers.main()
        self.assertNotEqual(0, ctx.exception.code)
        self.assertIn("exceeds", err.getvalue())
        self.assertNotIn("not found", out.getvalue())

    def test_status_all_discovers_sidecar_from_snapshot_without_unbounded_describe(self) -> None:
        # Head 2 (GH-682). Without a usable sidecar token, sidecar discovery fell
        # back to sidecar_is_pm2_registered -> `pm2 describe` (UNBOUNDED), which
        # ran BEFORE the bounded jlist snapshot. A large describe response could
        # exhaust memory before the jlist bound applied, so the PR's "one source,
        # one bound, one parse" was false while an unbounded read ran earlier.
        # Registration must now be answered from the shared snapshot.
        #
        # MUTATION PROOF: restore the unbounded describe (sidecar_is_pm2_registered
        # ignores the snapshot) and `describe` appears in pm2_calls before/after
        # jlist; assertEqual(["jlist"], pm2_calls) fails. Could this pass in a
        # world where a PM2 read in this command is still unbounded? NO -- the
        # describe IS that read, and this asserts the only PM2 read is jlist.
        pm2_calls: list[str] = []

        def fake_pm2_run(args_list, *, capture_output=False, max_output_bytes=None):
            pm2_calls.append(args_list[0])
            if args_list[0] == "jlist":
                # Sidecar is registered in the table; discovery must find it here.
                payload = json.dumps([
                    {"name": "llm-collab-alpha", "pm2_env": {"status": "online"}},
                    {"name": "llm-collab-codex-appserver", "pm2_env": {"status": "online"}},
                ])
                return subprocess.CompletedProcess(args=args_list, returncode=0, stdout=payload, stderr="")
            return subprocess.CompletedProcess(args=args_list, returncode=0)

        out = io.StringIO()
        with patch.object(sys, "argv", ["pm2_watchers.py", "status", "--all"]), \
             contextlib.redirect_stdout(out):
            with patch.object(pm2_watchers, "watcher_enabled_agents", return_value=[{"id": "alpha"}]), \
                 patch.object(pm2_watchers, "agent_ids", return_value=["alpha"]), \
                 patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                 patch.object(pm2_watchers, "enabled_sidecar_ids", return_value=[]), \
                 patch.object(pm2_watchers, "is_sidecar", side_effect=lambda aid: aid == "codex-appserver"), \
                 patch.object(pm2_watchers, "format_ax_status", return_value=""), \
                 patch.object(pm2_watchers, "probe_ax_trust", return_value=None), \
                 patch.object(pm2_watchers, "get_agent", return_value={}), \
                 patch.object(pm2_watchers, "pm2_run", side_effect=fake_pm2_run):
                pm2_watchers.main()
        # The ONLY PM2 read is the bounded jlist snapshot; no describe precedes it.
        self.assertEqual(["jlist"], pm2_calls)
        # Discovery still works: the registered sidecar is reported from the snapshot.
        self.assertIn("llm-collab-codex-appserver: online", out.getvalue())

    def test_status_all_unregistered_sidecar_not_discovered_and_no_describe(self) -> None:
        # Head 2 (GH-682), other direction. With no token and a sidecar absent
        # from the jlist snapshot, discovery must NOT invent it (no phantom
        # target) and still must not fall back to an unbounded describe.
        pm2_calls: list[str] = []

        def fake_pm2_run(args_list, *, capture_output=False, max_output_bytes=None):
            pm2_calls.append(args_list[0])
            if args_list[0] == "jlist":
                return subprocess.CompletedProcess(args=args_list, returncode=0, stdout="[]", stderr="")
            return subprocess.CompletedProcess(args=args_list, returncode=0)

        out = io.StringIO()
        with patch.object(sys, "argv", ["pm2_watchers.py", "status", "--all"]), \
             contextlib.redirect_stdout(out):
            with patch.object(pm2_watchers, "watcher_enabled_agents", return_value=[{"id": "alpha"}]), \
                 patch.object(pm2_watchers, "agent_ids", return_value=["alpha"]), \
                 patch.object(pm2_watchers, "config_get", return_value="llm-collab"), \
                 patch.object(pm2_watchers, "enabled_sidecar_ids", return_value=[]), \
                 patch.object(pm2_watchers, "is_sidecar", side_effect=lambda aid: aid == "codex-appserver"), \
                 patch.object(pm2_watchers, "format_ax_status", return_value=""), \
                 patch.object(pm2_watchers, "probe_ax_trust", return_value=None), \
                 patch.object(pm2_watchers, "get_agent", return_value={}), \
                 patch.object(pm2_watchers, "pm2_run", side_effect=fake_pm2_run):
                with self.assertRaises(SystemExit):
                    pm2_watchers.main()
        self.assertEqual(["jlist"], pm2_calls)
        self.assertNotIn("codex-appserver", out.getvalue())


if __name__ == "__main__":
    unittest.main()
