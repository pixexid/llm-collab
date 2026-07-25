"""The path invariant must be identical in Python and CJS, and used everywhere.

Six separate defects came from normalising one side of a two-sided comparison: a token
path validated differently from how the spawned app reads it, a runtime home
canonicalised at registration but launched verbatim, relative overrides resolved against
different bases in the manager and the config, and a restart that validated current
overrides while relaunching a stored definition. Testing each side alone caught none of
them; only an equivalence test can.

This loads the PRODUCTION implementations on both sides. An earlier version copied the
CJS function into the test, which cannot detect drift -- the exact failure mode the test
exists to prevent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import _helpers  # noqa: E402
import pm2_watchers  # noqa: E402
import session_autobridge  # noqa: E402

NODE = shutil.which("node")
CONFIG = ROOT / "pm2" / "ecosystem.config.cjs"

# the shapes that actually broke us
# Every shape must cross the language boundary. An earlier matrix omitted "~/.codex"
# from the CJS side and compared it Python-to-Python only, which is exactly where the
# implementations diverged: Python expanded it while Node resolved it to <repo>/~/.codex.
CASES = [
    "/tmp/codex-home/",
    "/tmp/codex-home",
    ".secrets/codex_app_server_ws_token",
    ".codex",
    "~",
    "~/.codex",
    "~/.codex/",
    "/tmp/./a/../codex-home",
    "/tmp/codex-home//",
]

# load the REAL exported function, never a reimplementation
# An explicit shared base is passed to BOTH sides. Relying on each side's implicit root
# made this environment-sensitive: Python bound the configured checkout while CJS bound
# whichever worktree held the config, so relative cases diverged in a merge worktree
# while passing locally. The test must compare the RULE, not the root.
SHARED_BASE = "/tmp/llm-collab-parity-base"

CJS_PROBE = (
    "const c = require(process.argv[1]);"
    "if (typeof c.canonicalPath !== 'function') { throw new Error('canonicalPath not exported'); }"
    "const base = process.argv[3];"
    "process.stdout.write(JSON.stringify(JSON.parse(process.argv[2]).map((v) => c.canonicalPath(v, base))));"
)


def cjs_canonical(cases: list[str], base: str = SHARED_BASE) -> list[str]:
    result = subprocess.run(
        [NODE, "-e", CJS_PROBE, str(CONFIG), json.dumps(cases), base],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise AssertionError(f"CJS probe failed: {result.stderr[:400]}")
    return json.loads(result.stdout)


@unittest.skipIf(NODE is None, "node is required to compare the CJS invariant")
class PathInvariantParityTest(unittest.TestCase):
    def test_production_implementations_agree_on_every_shape(self) -> None:
        cjs = cjs_canonical(CASES)
        for case, expected in zip(CASES, cjs):
            self.assertEqual(
                expected, str(_helpers.canonical_path(case, base=SHARED_BASE)),
                f"invariant diverged for {case!r}",
            )

    def test_manager_and_shared_helper_are_the_same_function(self) -> None:
        # pm2_watchers must not carry its own copy
        self.assertIs(pm2_watchers.canonical_path, _helpers.canonical_path)

    def test_registration_uses_the_same_invariant_as_launch(self) -> None:
        """A relative runtime home must resolve identically on both paths.

        Registration previously stored `.codex` verbatim while the ecosystem launched
        `<repo>/.codex`, so discovery -- which matches CODEX_HOME literally -- never
        found an endpoint and delivery failed with no diagnostic.
        """
        # cross the boundary for each case, including the tilde that used to diverge
        for case in ("/tmp/codex-home/", "~/.codex", "~"):
            with self.subTest(case=case):
                registered = session_autobridge.canonical_runtime_home(case)
                launched = cjs_canonical([case], base=str(ROOT))[0]
                self.assertEqual(
                    launched, registered,
                    f"registration and launch disagree for {case!r}",
                )

    def test_trailing_separator_never_changes_the_literal(self) -> None:
        self.assertEqual(
            str(_helpers.canonical_path("/tmp/codex-home/")),
            str(_helpers.canonical_path("/tmp/codex-home")),
        )

    def test_symlinks_are_not_resolved(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)
            self.assertEqual(str(link), str(_helpers.canonical_path(str(link))))


class SidecarRestartArgvTest(unittest.TestCase):
    """A sidecar restart must re-read the ecosystem, never a stored definition."""

    def test_restart_uses_start_or_restart_with_the_ecosystem_file(self) -> None:
        from unittest import mock

        with mock.patch.object(pm2_watchers, "config_get", return_value="llm-collab"):
            with mock.patch.object(pm2_watchers, "pm2_run") as ran:
                with mock.patch.object(pm2_watchers, "is_sidecar", return_value=True):
                    pm2_watchers.start_agent  # ensure module loaded
                    # drive the restart branch directly through main()
                    with mock.patch.object(sys, "argv",
                                           ["pm2_watchers.py", "restart", "--agent", "codex-appserver"]):
                        with mock.patch.object(pm2_watchers, "enabled_sidecar_ids",
                                               return_value=["codex-appserver"]):
                            with mock.patch.object(pm2_watchers, "sidecar_id_conflicts", return_value=[]):
                                pm2_watchers.main()
        self.assertTrue(ran.called, "pm2 must be invoked")
        argv = ran.call_args[0][0]
        self.assertEqual("startOrRestart", argv[0],
                         f"plain restart would relaunch a stored definition: {argv}")
        self.assertIn(str(pm2_watchers.ecosystem_path()), argv)
        self.assertIn("--only", argv)
        self.assertIn("llm-collab-codex-appserver", argv)
        self.assertNotIn("restart", argv[:1], "must not be a plain `pm2 restart`")


if __name__ == "__main__":
    unittest.main()
