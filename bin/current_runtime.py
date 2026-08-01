#!/usr/bin/env python3
"""Validate this checkout against origin/main before starting bootstrap."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_MARKER = re.compile(r"CONTRACT_VERSION:\s*(\S+)")


class ToolingError(RuntimeError):
    pass


def git(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ToolingError(f"git {' '.join(args)} failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ToolingError(f"git {' '.join(args)} failed: {detail}")
    return result


def contract_version(text: str) -> str:
    match = CONTRACT_MARKER.search(text[:200])
    if not match:
        raise ToolingError("AGENTS.md has no CONTRACT_VERSION marker")
    return match.group(1)


def current_tooling() -> dict[str, str]:
    if not (ROOT / ".git").exists():
        raise ToolingError(f"{ROOT} is not a git checkout")

    git("fetch", "origin", "main", "--quiet")
    origin_main = git("rev-parse", "origin/main").stdout.strip()
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != origin_main:
        raise ToolingError(
            "runtime must be exact origin/main; "
            f"origin/main={origin_main} HEAD={head}"
        )
    tracked_changes = git(
        "status", "--porcelain=v1", "--untracked-files=no"
    ).stdout.strip()
    if tracked_changes:
        raise ToolingError("runtime has tracked changes; refusing bootstrap")

    local_contract = contract_version((ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    origin_contract = contract_version(
        git("show", "origin/main:AGENTS.md").stdout
    )
    if local_contract != origin_contract:
        raise ToolingError(
            f"contract mismatch: checkout={local_contract} origin/main={origin_contract}"
        )
    return {
        "head": head,
        "origin_main": origin_main,
        "contract_version": local_contract,
    }


def parse_args() -> tuple[bool, list[str]]:
    parser = argparse.ArgumentParser(
        description="Validate current llm-collab tooling before session bootstrap."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and report current tooling without starting bootstrap.",
    )
    args, bootstrap_args = parser.parse_known_args()
    if not args.check and not bootstrap_args:
        parser.error("pass session_bootstrap.py arguments, or use --check")
    if args.check and bootstrap_args:
        parser.error("--check cannot be combined with bootstrap arguments")
    return args.check, bootstrap_args


def main() -> int:
    check_only, bootstrap_args = parse_args()
    try:
        evidence = current_tooling()
    except (OSError, ToolingError) as error:
        print(f"[tooling] REFUSED: {error}", file=sys.stderr)
        return 1

    print(
        f"[tooling] current: contract v{evidence['contract_version']} "
        f"HEAD {evidence['head']} origin/main {evidence['origin_main']}",
        file=sys.stdout if check_only else sys.stderr,
        flush=True,
    )
    if check_only:
        return 0

    bootstrap = ROOT / "bin" / "session_bootstrap.py"
    return subprocess.run(
        [sys.executable, str(bootstrap), *bootstrap_args], cwd=ROOT
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
