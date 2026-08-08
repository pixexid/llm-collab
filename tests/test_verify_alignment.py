"""Alignment: bin/verify.py is the required local gate for "verified", and
.github/workflows/verify.yml stays a correctly-configured manual
(workflow_dispatch) escape hatch — not an automatic PR gate."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
VERIFY_PY = ROOT / "bin" / "verify.py"
VERIFY_YML = ROOT / ".github" / "workflows" / "verify.yml"


def load_verify():
    spec = importlib.util.spec_from_file_location("verify_mod", VERIFY_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifyCommandTest(unittest.TestCase):
    def setUp(self):
        self.verify = load_verify()

    def test_root_is_repo_root(self):
        # cwd for the suite must be the repo root, where the top-level packages
        # import; otherwise ~345 `import llm_collab.*` modules become import
        # errors and the suite silently shrinks.
        self.assertEqual(self.verify.ROOT, ROOT)
        self.assertTrue((self.verify.ROOT / "llm_collab").is_dir())
        self.assertTrue((self.verify.ROOT / "tests").is_dir())

    def test_strips_runner_identity_env(self):
        for var in (
            "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "GEMINI_SESSION_ID",
            "LLM_COLLAB_READER_RUNTIME_FAMILY", "LLM_COLLAB_READER_RUNTIME_ID",
        ):
            self.assertIn(var, self.verify.STRIP_ENV)

    def test_build_env_removes_leaked_session_and_disables_bytecode(self):
        import os
        os.environ["CLAUDE_CODE_SESSION_ID"] = "leak-should-be-stripped"
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        env = self.verify.build_env()
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertEqual("1", env["PYTHONDONTWRITEBYTECODE"])

    def test_diff_check_clean_tree_passes(self):
        # This branch's committed diff (vs merge-base) has no whitespace errors.
        self.assertEqual(0, self.verify.run_diff_check())

    def test_diff_check_fails_closed_without_merge_base(self):
        # Sole gate: when origin/main can't resolve, fail closed rather than
        # silently degrade to the bare working-tree check (misses committed dirt).
        import unittest.mock as mock
        with mock.patch.object(self.verify, "_diff_check_base", return_value=None):
            self.assertNotEqual(0, self.verify.run_diff_check())

    def test_diff_check_base_resolves_committed_range(self):
        import os
        # Default: merge-base against origin/main resolves in a real checkout, so
        # the committed range is examined (not just the working tree).
        self.assertIsNotNone(self.verify._diff_check_base())
        # A non-existent base ref fails closed to None (bare working-tree check).
        os.environ["GITHUB_BASE_REF"] = "no-such-branch-xyz"
        self.addCleanup(os.environ.pop, "GITHUB_BASE_REF", None)
        self.assertIsNone(self.verify._diff_check_base())

    def test_main_fails_if_either_gate_fails(self):
        # GH-472 requires one command to run BOTH the suite and git diff --check;
        # a failure in either must surface, unmasked by the other passing.
        import sys as _sys
        v = self.verify
        self.addCleanup(setattr, v, "run_tests", v.run_tests)
        self.addCleanup(setattr, v, "run_diff_check", v.run_diff_check)
        self.addCleanup(setattr, _sys, "argv", _sys.argv)
        _sys.argv = ["verify.py"]

        v.run_tests = lambda argv: 0
        v.run_diff_check = lambda: 0
        self.assertEqual(0, v.main())

        v.run_tests = lambda argv: 0
        v.run_diff_check = lambda: 1
        self.assertNotEqual(0, v.main(), "diff-check failure must not be masked")

        v.run_tests = lambda argv: 1
        v.run_diff_check = lambda: 0
        self.assertNotEqual(0, v.main(), "test failure must not be masked")


class VerifyWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = VERIFY_YML.read_text()

    def test_read_only_permissions(self):
        self.assertIn("permissions:", self.text)
        self.assertIn("contents: read", self.text)

    def test_manual_dispatch_only(self):
        # Verify is a LOCAL gate; the workflow is a manual escape hatch.
        self.assertIn("workflow_dispatch:", self.text)

    def test_no_automatic_triggers(self):
        # No per-PR Actions minutes: must not run on pull_request or push.
        self.assertNotIn("pull_request", self.text)
        self.assertNotIn("push:", self.text)

    def test_pins_python_311(self):
        self.assertIn("python-version: '3.11'", self.text)

    def test_fetches_full_history_for_diff_check(self):
        # verify.py's diff-check resolves a merge-base; the manual workflow
        # checkout must fetch full history.
        self.assertIn("fetch-depth: 0", self.text)

    def test_installs_runtime_and_dev_requirements(self):
        self.assertIn("requirements-runtime.txt", self.text)
        self.assertIn("requirements-dev.txt", self.text)

    def test_invokes_the_canonical_verify_command(self):
        self.assertIn("python bin/verify.py", self.text)


class SuiteInterpreterDependencyGateTest(unittest.TestCase):
    """The pin check must describe the interpreter this module will actually
    launch the suite on (`sys.executable`), not the declared TEST_INTERPRETER.

    A wrong interpreter does not skip what it cannot import — it collects fewer
    tests than exist and fails the rest, so the run reads as a broken main. Both
    directions are asserted: an unsatisfied interpreter must refuse, and a
    satisfied one must not, or the gate cannot be told from its over-application.
    """

    def setUp(self):
        self.verify = load_verify()
        self.clean = {
            "test_interpreter": sys.executable, "interpreter_unprobeable": False,
            "critical_missing": [], "critical_mismatched": [],
            "runtime_missing": [], "runtime_mismatched": [], "read_failures": [],
        }

    def run_gate(self, report):
        import session_bootstrap

        probed = {}

        def fake_report(interpreter=session_bootstrap.TEST_INTERPRETER):
            probed["interpreter"] = interpreter
            return report

        with patch.object(session_bootstrap, "dependency_report", fake_report):
            with patch.object(session_bootstrap, "announce_dependencies", lambda r: None):
                return self.verify.check_suite_interpreter(), probed

    def test_probes_the_interpreter_that_will_run_the_suite(self):
        # Discriminating: passing TEST_INTERPRETER instead would still refuse
        # below, so the refusal alone cannot prove which environment was checked.
        _, probed = self.run_gate(self.clean)
        self.assertEqual(sys.executable, probed["interpreter"])

    def test_refuses_when_the_suites_interpreter_lacks_a_test_critical_pin(self):
        rc, _ = self.run_gate({**self.clean, "critical_missing": ["jsonschema"]})
        self.assertEqual(1, rc)

    def test_refuses_when_the_suites_interpreter_cannot_be_probed(self):
        rc, _ = self.run_gate({**self.clean, "interpreter_unprobeable": True})
        self.assertEqual(1, rc)

    def test_allows_a_satisfied_interpreter(self):
        rc, _ = self.run_gate(self.clean)
        self.assertEqual(0, rc)

    def test_a_runtime_only_gap_does_not_refuse(self):
        # Degradable pins never falsify a test result (GH-357/#362 ruling).
        rc, _ = self.run_gate({**self.clean, "runtime_missing": ["watchdog"]})
        self.assertEqual(0, rc)

    def test_main_does_not_run_the_suite_on_a_refused_interpreter(self):
        with patch.object(self.verify, "check_suite_interpreter", return_value=1):
            with patch.object(self.verify, "run_tests") as run_tests:
                self.assertEqual(1, self.verify.main())
        run_tests.assert_not_called()


if __name__ == "__main__":
    unittest.main()
