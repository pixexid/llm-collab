"""Bridge: run the axbridge Swift fixture suite when swiftc is available (GH-98).

The Swift resolver/outcome fixtures in tools/axbridge/test.sh are the
regression corpus for the AX doorbell; the configured Python suite previously
never executed them. Skips cleanly on hosts without a Swift toolchain.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class AxbridgeSwiftBridgeTest(unittest.TestCase):
    def test_axbridge_swift_fixtures_pass(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("AX doorbell fixtures are macOS-only")
        if shutil.which("swiftc") is None:
            self.skipTest("swiftc not available")
        result = subprocess.run(
            ["bash", str(ROOT / "tools" / "axbridge" / "test.sh")],
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = (result.stdout + result.stderr)[-2000:]
        self.assertEqual(result.returncode, 0, output)


if __name__ == "__main__":
    unittest.main()
