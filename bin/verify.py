#!/usr/bin/env python3
"""Canonical local verify gate — run the test suite the way CI does.

Side-effect-free: it fetches nothing, writes nothing to the repo or a remote, and
mutates no runtime state. It only runs the unittest suite, and it fixes the two
environment factors that have silently produced false greens
(see the reliability notes):

- **cwd = repo root**, so the top-level `llm_collab/` package imports. Running
  discover from inside `tests/` turns ~345 `import llm_collab.*` modules into
  import errors and silently shrinks the collected suite — a partial run that can
  false-pass.
- **reader identity vars stripped**, so a runner's own session identity
  (CLAUDE_CODE_SESSION_ID and friends) can't leak through `os.environ` into the
  subprocess tests and resolve a runtime family the test meant to leave unset.

It runs two gates and fails if either fails: the unittest suite, and
`git diff --check` (whitespace errors and leftover conflict markers). CI
(.github/workflows/verify.yml) pins the interpreter (python3.11) and invokes this
same command, so `bin/verify.py` is the single source of truth for "verified"
locally and in CI. Exit code is 0 iff every test passes and the diff check is
clean.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _python_runtime import require_python

require_python()

ROOT = Path(__file__).resolve().parent.parent

# Runner-session identity that must never leak into subprocess tests: each is
# read by runtime_family_from_env() and would resolve a family a test left unset.
STRIP_ENV = (
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_SESSION_ID",
    "GEMINI_SESSION_ID",
    "LLM_COLLAB_READER_RUNTIME_FAMILY",
    "LLM_COLLAB_READER_RUNTIME_ID",
)


def build_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_tests(argv: list[str]) -> int:
    return subprocess.run(
        [sys.executable, "-m", "unittest", *argv], cwd=ROOT, env=build_env(),
    ).returncode


def _diff_check_base() -> str | None:
    """Merge-base of HEAD and the integration branch, or None if it can't be
    resolved. In a PR, GITHUB_BASE_REF names the base branch; otherwise default to
    origin/main. `git diff --check <merge-base>` then examines the working tree
    against that base, covering the branch's COMMITTED changes (what a clean CI
    checkout has) as well as any uncommitted local edits — the bare form compares
    only working-tree-vs-index and so checks none of the committed diff in CI."""
    base_ref = os.environ.get("GITHUB_BASE_REF")
    remote_base = f"origin/{base_ref}" if base_ref else "origin/main"
    result = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", remote_base, "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def run_diff_check() -> int:
    """`git diff --check`: whitespace errors and leftover conflict markers.
    Side-effect-free (read-only). Checked against the merge-base when it resolves,
    so the branch's committed changes are examined (not just uncommitted ones);
    falls back to the bare working-tree check when no base is available."""
    base = _diff_check_base()
    target = [base] if base else []
    return subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--check", *target], env=build_env(),
    ).returncode


def main() -> int:
    argv = sys.argv[1:] or ["discover", "-s", "tests"]
    # Run both, report both, fail if either fails — a diff-check violation must
    # not be masked by passing tests and vice versa.
    test_rc = run_tests(argv)
    diff_rc = run_diff_check()
    if test_rc:
        print("verify: unittest failed", file=sys.stderr)
    if diff_rc:
        print("verify: git diff --check failed (whitespace or conflict markers)",
              file=sys.stderr)
    return test_rc or diff_rc


if __name__ == "__main__":
    raise SystemExit(main())
