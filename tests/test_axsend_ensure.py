from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "bin" / "axsend-ensure"


class AxsendEnsureTest(unittest.TestCase):
    def make_wrapper_root(
        self, ring_output: str, *, ring_exit: int = 0
    ) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp(prefix="llm-collab-axsend-ensure-"))
        self.addCleanup(shutil.rmtree, root)
        wrapper = root / "bin" / "axsend-ensure"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(WRAPPER.read_text())
        wrapper.chmod(0o755)

        bridge = root / "tools" / "axbridge"
        bridge.mkdir(parents=True)
        build = bridge / "build.sh"
        build.write_text("#!/bin/bash\nexit 0\n")
        build.chmod(0o755)

        log = root / "calls.log"
        axsend = bridge / "axsend"
        axsend.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"$*\" >> {str(log)!r}\n"
            "if [[ \"${1:-}\" == \"ring\" ]]; then\n"
            f"  printf '%s\\n' {ring_output!r}\n"
            f"  exit {int(ring_exit)}\n"
            "fi\n"
            "printf '%s\\n' 'delivered: text appears as a sent message'\n"
        )
        axsend.chmod(0o755)
        return wrapper, log

    def run_wrapper(self, wrapper: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(wrapper), "ring", "--app", "ZCode", "--submit", "--verify", "--text", "wake"],
            text=True,
            capture_output=True,
            env={**os.environ, "TMPDIR": tempfile.gettempdir()},
            check=False,
        )

    def test_queued_unconfirmed_is_ambiguous_never_exit_zero(self) -> None:
        # #given — faithful post-#406 axsend: a queued/unconfirmed ring prints the
        # QUEUED line AND its AMBIGUOUS outcome, and exits 3 (not 0).
        wrapper, log = self.make_wrapper_root(
            "QUEUED (UNCONFIRMED): recipient went busy with no visible turn\n"
            "AX_OUTCOME=AMBIGUOUS reason=queued_unconfirmed",
            ring_exit=3,
        )

        # #when
        result = self.run_wrapper(wrapper)

        # #then — ambiguous is never exit-zero success, and the wrapper does not run
        # a standalone confirm or resend; it surfaces the one AMBIGUOUS outcome.
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("AX_OUTCOME=AMBIGUOUS reason=queued_unconfirmed", result.stdout)
        calls = log.read_text().splitlines()
        self.assertEqual(sum(call.startswith("ring ") for call in calls), 1)
        self.assertFalse(any(call.startswith("confirm ") for call in calls))

    def test_confirmed_ring_keeps_standalone_confirmation(self) -> None:
        # #given
        wrapper, log = self.make_wrapper_root(
            "VERIFIED: submitted via button-press — delivered as a conversation turn."
        )

        # #when
        result = self.run_wrapper(wrapper)

        # #then
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            log.read_text().splitlines(),
            [
                "turns --app ZCode --text wake",
                "ring --app ZCode --submit --verify --text wake",
                "confirm --app ZCode --text wake",
            ],
        )


if __name__ == "__main__":
    unittest.main()
