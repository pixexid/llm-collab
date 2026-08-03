#!/usr/bin/env python3
"""Canonical local verify gate — the required pre-push/pre-PR check.

This is THE gate: run it locally before pushing a review head or opening a PR.
There is no automatic PR CI; `.github/workflows/verify.yml` is a manual-dispatch
escape hatch only, and a dispatched run is supplementary evidence, never the merge
gate.

Side-effect-free: it fetches nothing, writes nothing to the repo or a remote, and
mutates no runtime state. Because it does not fetch, run `git fetch origin main`
first (fetch-only) so the diff-check merge-base is current. It runs the
unittest suite and fixes the two environment factors that have silently produced
false greens (see the reliability notes):

- **cwd = repo root**, so the top-level `llm_collab/` package imports. Running
  discover from inside `tests/` turns ~345 `import llm_collab.*` modules into
  import errors and silently shrinks the collected suite — a partial run that can
  false-pass.
- **reader identity vars stripped**, so a runner's own session identity
  (CLAUDE_CODE_SESSION_ID and friends) can't leak through `os.environ` into the
  subprocess tests and resolve a runtime family the test meant to leave unset.

It runs two gates and fails if either fails: the unittest suite, and
`git diff --check` (whitespace errors and leftover conflict markers). Exit code
is 0 iff every test passes and the diff check is clean.
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
sys.path.insert(0, str(ROOT / "tests"))  # GH-503: import the runtime-gate testkit

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
    # GH-503: authorize the runtime freshness-gate bypass for the whole suite via
    # the per-run token+sentinel (not a generic switch). Installed here so the
    # canonical gate passes regardless of the unittest argv form (plain discover,
    # a -p pattern that skips tests/__init__, or explicit module names) from a
    # feature-branch worktree the gate would otherwise refuse.
    from _runtime_gate_testkit import gate_bypass_env

    env.update(gate_bypass_env())
    return env


def run_tests(argv: list[str]) -> int:
    return subprocess.run(
        [sys.executable, "-m", "unittest", *argv], cwd=ROOT, env=build_env(),
    ).returncode


def _diff_check_base() -> str | None:
    """Merge-base of HEAD and the integration branch, or None if it can't be
    resolved. In a PR, GITHUB_BASE_REF names the base branch; otherwise default to
    origin/main. `git diff --check <merge-base>` then examines the working tree
    against that base, covering the branch's COMMITTED changes (what a fresh
    checkout of the branch has) as well as any uncommitted local edits — the bare
    form compares only working-tree-vs-index and so checks none of the committed
    diff on a clean checkout."""
    base_ref = os.environ.get("GITHUB_BASE_REF")
    remote_base = f"origin/{base_ref}" if base_ref else "origin/main"
    result = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", remote_base, "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def run_diff_check() -> int:
    """`git diff --check` against the merge-base: whitespace errors and leftover
    conflict markers across the branch's committed changes, not just the working
    tree. Side-effect-free (read-only).

    Fails closed when the merge-base cannot be resolved (origin/main not fetched):
    verify is the sole gate, so silently degrading to the bare working-tree check —
    which misses committed whitespace — would weaken it. Sync origin/main first
    (`git fetch origin main`)."""
    base = _diff_check_base()
    if base is None:
        print("verify: cannot resolve the origin/main merge-base — run "
              "`git fetch origin main` first so the committed diff is checked",
              file=sys.stderr)
        return 1
    return subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--check", base], env=build_env(),
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
