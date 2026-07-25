"""The sidecar path invariant must be identical in Python and CJS.

Five separate defects came from normalising one side of a two-sided comparison:
a token path validated differently from how the spawned app reads it, a runtime home
canonicalised at registration but launched verbatim, and relative overrides resolved
against different bases in the manager and the config. Testing each side alone could
not catch any of them; only an equivalence test can.
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

import pm2_watchers  # noqa: E402

NODE = shutil.which("node")

# exercise the shapes that actually broke us: trailing separator, relative override,
# redundant segments, and an already-absolute path
CASES = [
    "/tmp/codex-home/",
    "/tmp/codex-home",
    ".secrets/codex_app_server_ws_token",
    "/tmp/./a/../codex-home",
    "/tmp/codex-home//",
]

CJS_PROBE = """
const path = require("path");
const root = process.argv[1];
function canonicalPath(value, base) {
  if (!value) return value;
  const resolved = path.resolve(base || root, String(value).trim());
  return resolved.length > 1 ? resolved.replace(/\\/+$/, "") : resolved;
}
process.stdout.write(JSON.stringify(JSON.parse(process.argv[2]).map((v) => canonicalPath(v))));
"""


@unittest.skipIf(NODE is None, "node is required to compare the CJS invariant")
class PathInvariantParityTest(unittest.TestCase):
    def test_python_and_cjs_agree_on_every_shape(self) -> None:
        result = subprocess.run(
            [NODE, "-e", CJS_PROBE, str(ROOT), json.dumps(CASES)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr[:300])
        cjs = json.loads(result.stdout)
        py = [str(pm2_watchers.canonical_path(case)) for case in CASES]
        for case, expected, actual in zip(CASES, cjs, py):
            self.assertEqual(
                expected, actual,
                f"invariant diverged for {case!r}: CJS={expected!r} Python={actual!r}",
            )

    def test_trailing_separator_is_stripped_on_both_sides(self) -> None:
        self.assertEqual(
            str(pm2_watchers.canonical_path("/tmp/codex-home/")),
            str(pm2_watchers.canonical_path("/tmp/codex-home")),
            "a trailing separator must not produce a different literal",
        )

    def test_relative_override_resolves_against_the_repository_root(self) -> None:
        # not the caller's cwd: the manager and the config must pick the same file
        self.assertEqual(
            ROOT / ".secrets" / "tok",
            pm2_watchers.canonical_path(".secrets/tok"),
        )

    def test_symlinks_are_not_resolved(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)
            self.assertEqual(str(link), str(pm2_watchers.canonical_path(str(link))))


if __name__ == "__main__":
    unittest.main()
