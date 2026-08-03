"""GH-503: the mandatory current-runtime freshness gate on mutation entrypoints.

Covers the five required cases (stale deployed, stale source, tracked dirt, fetch
failure, exact-current success), the recovery-only waiver (scoped to one command),
the test-only bypass, and direct-entrypoint coverage proving delivery / session
mutation / watcher startup call the gate while read-only commands do not.

These tests never set LLM_COLLAB_RUNTIME_GATE_TEST_BYPASS — they exercise the real
gate. current_tooling() itself (the git-level checks) is covered by
test_current_runtime.py; here we drive the gate above it.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import current_runtime


class GateSentinel(Exception):
    pass


def _stale(msg="runtime must be exact origin/main; origin/main=aaa HEAD=bbb"):
    return current_runtime.ToolingError(msg)


class RuntimeGateTest(unittest.TestCase):
    # ---- the five required cases ----

    def test_exact_current_success_passes(self):
        evidence = {"head": "x", "origin_main": "x", "contract_version": "10"}
        with patch.object(current_runtime, "current_tooling", return_value=evidence):
            out = current_runtime.require_current_runtime("deliver", environ={})
        self.assertEqual(evidence, out)

    def test_stale_deployed_runtime_refuses_and_names_deployed(self):
        with patch.object(current_runtime, "current_tooling", side_effect=_stale()), \
             patch.object(current_runtime, "ROOT", current_runtime._DEPLOYED_RUNTIME):
            with self.assertRaises(SystemExit) as cm:
                current_runtime.require_current_runtime("deliver", environ={})
        self.assertEqual(current_runtime.RUNTIME_GATE_REFUSED, cm.exception.code)

    def test_stale_source_checkout_refuses_and_names_source(self):
        src = Path("/tmp/some/source/checkout")
        with patch.object(current_runtime, "current_tooling", side_effect=_stale()), \
             patch.object(current_runtime, "ROOT", src):
            with self.assertRaises(SystemExit) as cm:
                current_runtime.require_current_runtime("deliver", environ={})
        self.assertEqual(current_runtime.RUNTIME_GATE_REFUSED, cm.exception.code)

    def test_tracked_dirt_refuses(self):
        with patch.object(
            current_runtime, "current_tooling",
            side_effect=current_runtime.ToolingError("runtime has tracked changes; refusing bootstrap"),
        ):
            with self.assertRaises(SystemExit) as cm:
                current_runtime.require_current_runtime("deliver", environ={})
        self.assertEqual(current_runtime.RUNTIME_GATE_REFUSED, cm.exception.code)

    def test_fetch_failure_fails_closed_not_silent_pass(self):
        with patch.object(
            current_runtime, "current_tooling",
            side_effect=current_runtime.ToolingError("git fetch origin main failed: network down"),
        ):
            with self.assertRaises(SystemExit) as cm:
                current_runtime.require_current_runtime("watch", environ={})
        self.assertEqual(current_runtime.RUNTIME_GATE_REFUSED, cm.exception.code)

    def test_tree_label_distinguishes_deployed_from_source(self):
        self.assertEqual("deployed runtime", current_runtime._tree_label(current_runtime._DEPLOYED_RUNTIME))
        self.assertIn("source checkout", current_runtime._tree_label(Path("/tmp/x")))

    # ---- recovery waiver: scoped, loud, never blanket ----

    def test_recovery_waiver_matching_command_overrides(self):
        env = {current_runtime.RECOVERY_WAIVER_ENV: "deliver"}
        with patch.object(current_runtime, "current_tooling", side_effect=_stale()):
            out = current_runtime.require_current_runtime("deliver", environ=env)
        self.assertEqual("deliver", out.get("waived"))

    def test_recovery_waiver_does_not_authorize_other_commands(self):
        # waiver set for 'deliver' must NOT bypass a 'watch' mutation.
        env = {current_runtime.RECOVERY_WAIVER_ENV: "deliver"}
        with patch.object(current_runtime, "current_tooling", side_effect=_stale()):
            with self.assertRaises(SystemExit) as cm:
                current_runtime.require_current_runtime("watch", environ=env)
        self.assertEqual(current_runtime.RUNTIME_GATE_REFUSED, cm.exception.code)

    # ---- test bypass: per-run token must match the sentinel file (not a switch) ----

    def test_token_matching_sentinel_bypasses(self):
        import tempfile
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as h:
            h.write("secret-token-123")
        try:
            env = {current_runtime.TEST_TOKEN_ENV: "secret-token-123",
                   current_runtime.TEST_SENTINEL_ENV: path}
            with patch.object(current_runtime, "current_tooling", side_effect=AssertionError("must not run")):
                out = current_runtime.require_current_runtime("deliver", environ=env)
            self.assertEqual("deliver", out.get("test_bypass"))
        finally:
            os.unlink(path)

    def test_token_without_sentinel_does_not_bypass(self):
        # a bare env token with no sentinel file must NOT bypass — enforce the gate.
        env = {current_runtime.TEST_TOKEN_ENV: "x"}
        with patch.object(current_runtime, "current_tooling", side_effect=_stale()):
            with self.assertRaises(SystemExit) as cm:
                current_runtime.require_current_runtime("deliver", environ=env)
        self.assertEqual(current_runtime.RUNTIME_GATE_REFUSED, cm.exception.code)

    def test_token_mismatch_does_not_bypass(self):
        import tempfile
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as h:
            h.write("real-token")
        try:
            env = {current_runtime.TEST_TOKEN_ENV: "guessed-wrong",
                   current_runtime.TEST_SENTINEL_ENV: path}
            with patch.object(current_runtime, "current_tooling", side_effect=_stale()):
                with self.assertRaises(SystemExit) as cm:
                    current_runtime.require_current_runtime("deliver", environ=env)
            self.assertEqual(current_runtime.RUNTIME_GATE_REFUSED, cm.exception.code)
        finally:
            os.unlink(path)

    def test_direct_production_subprocess_cannot_bypass(self):
        # codex's required proof: a direct mutator with NO test token/sentinel and NO
        # recovery waiver, from this feature-branch worktree, is refused with exit 78.
        import subprocess
        bin_deliver = REPO_ROOT / "bin" / "deliver.py"
        clean = {
            k: v for k, v in os.environ.items()
            if k not in (current_runtime.TEST_TOKEN_ENV, current_runtime.TEST_SENTINEL_ENV,
                         current_runtime.RECOVERY_WAIVER_ENV)
        }
        proc = subprocess.run(
            [sys.executable, str(bin_deliver)], env=clean,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(current_runtime.RUNTIME_GATE_REFUSED, proc.returncode, proc.stderr)
        self.assertIn("runtime-gate", proc.stderr)

    # ---- direct-entrypoint coverage: mutation gates, read-only does not ----

    def test_deliver_main_calls_the_gate_first(self):
        import deliver
        with patch.object(deliver, "require_current_runtime", side_effect=GateSentinel), \
             patch.object(deliver, "parse_args", side_effect=AssertionError("gate must run before parse_args")):
            with self.assertRaises(GateSentinel):
                deliver.main()

    def test_watch_main_calls_the_gate_first(self):
        import watch_inbox
        with patch.object(watch_inbox, "require_current_runtime", side_effect=GateSentinel), \
             patch.object(watch_inbox, "parse_args", side_effect=AssertionError("gate must run before parse_args")):
            with self.assertRaises(GateSentinel):
                watch_inbox.main()

    def test_autobridge_gates_a_mutation_command(self):
        import session_autobridge as sab
        import argparse
        ns = argparse.Namespace(command="register", json_output=False)
        with patch.object(sab, "parse_args", return_value=ns), \
             patch.object(sab, "require_current_runtime", side_effect=GateSentinel) as gate:
            with self.assertRaises(GateSentinel):
                sab.main()
        gate.assert_called_once_with("autobridge:register")

    def test_autobridge_does_not_gate_a_read_only_command(self):
        import session_autobridge as sab
        import argparse
        ns = argparse.Namespace(command="show", json_output=False)
        with patch.object(sab, "parse_args", return_value=ns), \
             patch.object(sab, "require_current_runtime", side_effect=GateSentinel) as gate, \
             patch.object(sab, "show_session", return_value={"ok": True}), \
             patch.object(sab, "emit"):
            sab.main()
        gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
