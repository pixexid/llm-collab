"""A missing pin makes the suite lie rather than fail (GH-362).

The runtime-adapter conformance validators raise `ConformanceFailure` on
ImportError instead of skipping, so an interpreter without the schema-validator
pins reports 131 failures and 31 errors *in conformance* and silently collects
156 fewer tests than exist. On 2026-07-28 that was read as a broken `main` and
reported to a collaborator as one; `main` was 1856 and green.

The connector findings pinned here: reads are bounded at the read (not
stat-then-EOF), invalid UTF-8 becomes UNKNOWN not a crash, read-failure
criticality is preserved so a degradable file is not shouted, versions are
enforced not just presence, and the interpreter that actually runs the suite is
the one probed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import session_bootstrap


class RequirementsParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="llm-collab-deps-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(session_bootstrap, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name: str, body: str) -> None:
        (self.root / name).write_text(body, encoding="utf-8")

    def test_pins_carry_their_version_and_criticality(self) -> None:
        self.write("requirements-dev.txt", "somevalidator==4.26.0\n")
        self.write("requirements-runtime.txt", "fileevents==6.0.0\n")
        pins, failures = session_bootstrap.parse_requirements()
        by_name = {p["name"]: p for p in pins}
        self.assertEqual([], failures)
        self.assertEqual("4.26.0", by_name["somevalidator"]["pinned_version"])
        self.assertTrue(by_name["somevalidator"]["test_critical"])
        self.assertFalse(by_name["fileevents"]["test_critical"])

    def test_comments_blanks_flags_and_extras_are_not_pins(self) -> None:
        self.write("requirements-dev.txt",
                   "# dev\n\nvalidator==4.26.0  # note\n-r other.txt\nrich[jupyter]==13.0\n")
        self.write("requirements-runtime.txt", "")
        names = [p["name"] for p in session_bootstrap.parse_requirements()[0]]
        self.assertEqual(["validator", "rich"], names)

    def test_an_absent_file_is_skipped_but_an_unreadable_one_is_a_failure(self) -> None:
        """Finding 3: an unknown pin set must not silently become an empty one."""
        self.write("requirements-dev.txt", "validator==4.26.0\n")
        pins, failures = session_bootstrap.parse_requirements()
        self.assertEqual([], failures)  # runtime file absent -> skipped, no failure

        self.write("requirements-runtime.txt", "fileevents==6.0.0\n")
        with patch.object(session_bootstrap, "_read_requirements_bounded",
                          side_effect=session_bootstrap.RequirementsUnreadable("EIO")):
            _pins, failures = session_bootstrap.parse_requirements()
        self.assertTrue(failures, "an unreadable file must surface, not vanish")

    def test_a_read_failure_carries_its_files_criticality(self) -> None:
        """Finding 3: an unreadable degradable file must not be shouted as
        test-critical, and an unreadable dev file must be."""
        self.write("requirements-dev.txt", "validator==1\n")
        self.write("requirements-runtime.txt", "fileevents==1\n")

        def only_runtime_fails(path, remaining):
            if path.name == "requirements-runtime.txt":
                raise session_bootstrap.RequirementsUnreadable("runtime EIO")
            return "validator==1\n"

        with patch.object(session_bootstrap, "_read_requirements_bounded",
                          side_effect=only_runtime_fails):
            _pins, failures = session_bootstrap.parse_requirements()
        self.assertEqual(1, len(failures))
        self.assertFalse(failures[0]["test_critical"])

    def test_the_read_asks_for_a_bounded_number_of_bytes(self) -> None:
        """Finding 1, the mechanism not just the outcome: the read must request at
        most remaining+1 bytes, so a file that grows after the open still cannot be
        read to an unbounded EOF. Records the size actually passed to read()."""
        requested = []

        class FakeHandle:
            def read(self, n=-1):
                requested.append(n)
                return b"x" * min(n if n and n > 0 else 10_000, 10_000)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("builtins.open", return_value=FakeHandle()):
            with self.assertRaises(session_bootstrap.RequirementsUnreadable):
                session_bootstrap._read_requirements_bounded(self.root / "big.txt", 10)
        self.assertEqual([11], requested, "read must be bounded to remaining+1, never read-to-EOF")

    def test_an_oversized_file_still_fails_closed(self) -> None:
        self.write("requirements-dev.txt", "x" * 5000)
        with self.assertRaises(session_bootstrap.RequirementsUnreadable):
            session_bootstrap._read_requirements_bounded(
                self.root / "requirements-dev.txt", 10)

    def test_invalid_utf8_is_a_read_failure_not_a_crash(self) -> None:
        """Finding 2: a corrupt file must become UNKNOWN, not an uncaught
        UnicodeDecodeError that crashes bootstrap."""
        (self.root / "requirements-dev.txt").write_bytes(b"validator==1\n\xff\xfe")
        with self.assertRaises(session_bootstrap.RequirementsUnreadable):
            session_bootstrap._read_requirements_bounded(
                self.root / "requirements-dev.txt", session_bootstrap.MAX_REQUIREMENTS_BYTES)


class DependencyReportTest(unittest.TestCase):
    CRIT = {"name": "validator", "pinned_version": "4.26.0", "test_critical": True}
    RUNTIME = {"name": "fileevents", "pinned_version": "6.0.0", "test_critical": False}

    def report(self, pins, installed, read_failures=None):
        with patch.object(session_bootstrap, "parse_requirements",
                          return_value=(pins, read_failures or [])):
            with patch.object(session_bootstrap, "_installed_versions", return_value=installed):
                return session_bootstrap.dependency_report()

    def test_a_missing_test_critical_pin_is_critical(self) -> None:
        r = self.report([self.CRIT], installed={"validator": None})
        self.assertEqual(["validator"], r["critical_missing"])
        self.assertEqual([], r["runtime_missing"])

    def test_a_missing_runtime_pin_is_only_runtime(self) -> None:
        r = self.report([self.RUNTIME], installed={"fileevents": None})
        self.assertEqual(["fileevents"], r["runtime_missing"])
        self.assertEqual([], r["critical_missing"])

    def test_a_wrong_version_is_enforced_not_ignored(self) -> None:
        """Finding 2 (round 1): the pin says which version, not just that one exists."""
        r = self.report([self.CRIT], installed={"validator": "3.0.0"})
        self.assertEqual([], r["critical_missing"])
        self.assertTrue(any("3.0.0" in m and "4.26.0" in m for m in r["critical_mismatched"]))

    def test_the_exact_pinned_version_is_accepted(self) -> None:
        r = self.report([self.CRIT], installed={"validator": "4.26.0"})
        self.assertEqual([], r["critical_missing"])
        self.assertEqual([], r["critical_mismatched"])

    def test_metadata_error_is_not_reported_as_missing(self) -> None:
        """#370's error-is-not-an-answer: '?' (metadata unreadable) is neither
        missing nor mismatched — installing what is already installed helps nobody."""
        r = self.report([self.CRIT], installed={"validator": "?"})
        self.assertEqual([], r["critical_missing"])
        self.assertEqual([], r["critical_mismatched"])

    def test_an_unprobeable_test_interpreter_is_flagged_not_passed(self) -> None:
        """Finding 4: if python3.11 cannot be run, the suite's environment is
        UNKNOWN. Proceeding as complete is the silent pass this gate stops."""
        r = self.report([self.CRIT], installed=None)
        self.assertTrue(r["interpreter_unprobeable"])


class AnnouncementTest(unittest.TestCase):
    def announce(self, report: dict) -> str:
        base = {"test_interpreter": "python3.11", "interpreter_unprobeable": False,
                "critical_missing": [], "critical_mismatched": [],
                "runtime_missing": [], "runtime_mismatched": [], "read_failures": []}
        out: list[str] = []
        with patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(str(x) for x in a))):
            session_bootstrap.announce_dependencies({**base, **report})
        return "\n".join(out)

    def test_a_complete_environment_is_silent(self) -> None:
        self.assertEqual("", self.announce({}))

    def test_a_test_critical_gap_earns_the_loud_banner(self) -> None:
        text = self.announce({"critical_missing": ["validator"]})
        self.assertIn("test results here are not real", text)
        self.assertIn("validator", text)

    def test_the_banner_names_the_test_interpreter_not_this_process(self) -> None:
        """Finding 4: the report is about python3.11, so the banner must say so."""
        text = self.announce({"critical_missing": ["validator"]})
        self.assertIn("python3.11", text)

    def test_a_degradable_gap_does_not_earn_the_banner(self) -> None:
        text = self.announce({"runtime_missing": ["fileevents"]})
        self.assertNotIn("test results here are not real", text)
        self.assertIn("fileevents", text)
        self.assertIn("degradable", text)

    def test_a_degradable_read_failure_is_reported_softly(self) -> None:
        """Finding 3: an unreadable runtime file is degradable, not test-critical."""
        text = self.announce({"read_failures": [{"detail": "runtime EIO", "test_critical": False}]})
        self.assertNotIn("test results here are not real", text)
        self.assertIn("runtime EIO", text)

    def test_a_test_critical_read_failure_earns_the_banner_and_says_unknown(self) -> None:
        text = self.announce({"read_failures": [{"detail": "dev EIO", "test_critical": True}]})
        self.assertIn("test results here are not real", text)
        self.assertIn("UNKNOWN", text)

    def test_an_unprobeable_interpreter_earns_its_own_banner(self) -> None:
        text = self.announce({"interpreter_unprobeable": True})
        self.assertIn("CANNOT VERIFY", text)
        self.assertIn("python3.11", text)
        self.assertIn("not real", text)


if __name__ == "__main__":
    unittest.main()
