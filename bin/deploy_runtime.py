#!/usr/bin/env python3
"""Refresh the isolated deployed runtime without touching a source checkout."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = Path.home() / ".local" / "share" / "llm-collab" / "runtime" / "main"
CONTRACT_MARKER = re.compile(r"CONTRACT_VERSION:\s*(\S+)")


class DeployError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise DeployError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def contract_version(text: str) -> str:
    match = CONTRACT_MARKER.search(text[:200])
    if not match:
        raise DeployError("AGENTS.md has no CONTRACT_VERSION marker")
    return match.group(1)


def source_head(source: Path) -> tuple[str, str]:
    git(source, "fetch", "origin", "main", "--quiet")
    origin_main = git(source, "rev-parse", "origin/main")
    head = git(source, "rev-parse", "HEAD")
    if head != origin_main:
        raise DeployError(
            f"source must be exact origin/main: origin/main={origin_main} HEAD={head}"
        )
    ancestry = subprocess.run(
        ["git", "-C", str(source), "merge-base", "--is-ancestor", "origin/main", "HEAD"],
        timeout=30,
    )
    if ancestry.returncode:
        raise DeployError(f"source is stale or unrelated: origin/main={origin_main} HEAD={head}")
    local_contract = contract_version((source / "AGENTS.md").read_text(encoding="utf-8"))
    origin_contract = contract_version(git(source, "show", "origin/main:AGENTS.md"))
    if local_contract != origin_contract:
        raise DeployError(
            f"contract mismatch: source={local_contract} origin/main={origin_contract}"
        )
    return head, local_contract


def deploy(source: Path, target: Path | None = None) -> dict[str, str]:
    source = source.resolve()
    target = (DEFAULT_TARGET if target is None else target).resolve()
    if source == target:
        raise DeployError("source and deployed runtime must be different paths")
    if not (source / ".git").exists() or not (target / ".git").exists():
        raise DeployError("source and target must both be git worktrees")
    head, contract = source_head(source)
    git(target, "reset", "--hard", head)
    return {"source": str(source), "target": str(target), "head": head, "contract_version": contract}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        evidence = deploy(args.source)
    except (OSError, subprocess.SubprocessError, DeployError) as error:
        print(f"[runtime] REFUSED: {error}")
        return 1
    print(
        f"[runtime] deployed contract v{evidence['contract_version']} "
        f"HEAD {evidence['head']} target {evidence['target']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
