"""GH-728 guard: no bare BB invocation outside the resolver seam.

Every call site that invokes BB obtains its command from
``llm_collab.bb_client.bb_executable_from_project()``; nothing else constructs
a BB invocation. This test scans the production source tree and fails on any
bare BB invocation, so the seventh instance of the bare-``bb`` class arrives
as a red test in the authoring worker's own suite, seconds after it is
written, instead of as a review finding hours later.

Scope and exemptions — deliberately narrow:

- Scanned: ``bin/``, ``llm_collab/``, ``scripts/`` (Python) and ``bb-plugins/``,
  ``pi-extensions/`` (TS/JS). Those are the trees whose code can spawn a
  process; a BB invocation outside them cannot execute.
- Not scanned: ``tests/`` and ``docs/``. Test fixtures carry ``["bb"]`` as
  registry *data* — the configured value the seam validates — never as an
  invocation; docs describe BB's native CLI surface in prose and are governed
  by review, not by this guard. No production file is exempt: after GH-728 the
  resolver itself holds no bb argv literal, so a clean tree needs zero
  exemptions and any hit is a real violation.

The patterns name bb as the *command*: an argv list literal whose first
element is the bb binary (with a lookbehind excluding subscripts such as
``entry["bb"]``), a bb literal passed straight to a process-spawning call, or
a ``which("bb")`` PATH lookup, which is how a PATH fallback gets built.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PY_DIRS = ("bin", "llm_collab", "scripts")
TS_DIRS = ("bb-plugins", "pi-extensions")

PATTERNS = (
    # argv list literal starting with the bb binary: ["bb"], [ "bb", ...].
    # The lookbehind excludes subscripts (entry["bb"]) and indexing after
    # a call or string, which are config reads, not invocations.
    re.compile(r"(?<![\w\)\]\"'`])\[\s*[\"']bb[\"'\s,\]]"),
    # bb literal as the command argument of a process-spawning call.
    re.compile(
        r"(?:Popen|run|exec|spawn|check_output|check_call|call)\(\s*[\"']bb[\"'\s,\)]"
    ),
    # A PATH lookup for bb: how a silent PATH fallback gets constructed.
    re.compile(r"which\(\s*[\"']bb[\"']"),
)


def bare_bb_invocations() -> list[str]:
    hits: list[str] = []
    files: list[Path] = []
    for directory in PY_DIRS:
        files.extend(sorted((ROOT / directory).rglob("*.py")))
    for directory in TS_DIRS:
        for suffix in ("*.ts", "*.js"):
            files.extend(sorted((ROOT / directory).rglob(suffix)))
    for path in files:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if any(pattern.search(line) for pattern in PATTERNS):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    return hits


class BbInvocationGuardTest(unittest.TestCase):
    def test_no_bare_bb_invocation_outside_the_resolver_seam(self) -> None:
        hits = bare_bb_invocations()
        self.assertEqual(
            [],
            hits,
            "bare BB invocation(s) outside bb_executable_from_project() "
            "(GH-728); resolve the command through the seam instead:\n"
            + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
