#!/usr/bin/env python3
"""Post-merge local-main synchronization gate for the persistent checkout.

After a merge the persistent llm-collab checkout must return to origin/main so
tooling that refuses a stale HEAD (current_runtime.py) can proceed. This command
fetches origin/main, classifies the checkout, and — with --apply — safely
advances HEAD to origin/main (fast-forwarding `main`, or moving a detached HEAD).

It NEVER discards work. Tracked/staged changes, a branch carrying unique local
commits, or a diverged history fail closed with an explicit classification and a
non-zero exit. Untracked files (the runtime's own Chats/Logs/State) are ignored,
matching current_runtime.py's `--untracked-files=no` contract. The branch name is
irrelevant to the freshness contract (HEAD == origin/main), so a shared checkout
that is routinely detached is synced in place rather than force-switched to a
`main` another worktree holds.

Classifications (always reported with exact local/remote SHAs):
  already_current  exit 0  on main, HEAD == origin/main, clean
  aligned_to_main  exit 0  HEAD == origin/main but detached/feature branch
  fast_forwarded   exit 0  strictly behind, no unique commits -> ff to origin/main
  dirty_tracked    exit 1  tracked or staged changes present
  active_branch    exit 1  HEAD has unique commits (unmerged work) — never discarded
  diverged         exit 1  local and remote both have unique commits
  error            exit 1  not a checkout / git failure

Dry-run (default) reports the classification and the action it WOULD take;
--apply performs the switch/fast-forward for the safe classes. The exit code
reflects the classification in both modes, so `local_main_sync.py --apply` is a
usable post-merge gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

SAFE = {"already_current", "aligned_to_main", "fast_forwarded"}


class SyncError(RuntimeError):
    pass


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SyncError(f"git {' '.join(args)} failed: {error}") from error
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SyncError(f"git {' '.join(args)} failed: {detail}")
    return result


def has_tracked_dirt(root: Path) -> bool:
    """Tracked (staged or unstaged) changes, detected by exit code so a
    pathologically large change set is never buffered into memory. Untracked
    files are ignored, matching current_runtime.py's --untracked-files=no."""
    for cached in ((), ("--cached",)):
        rc = git(root, "diff", *cached, "--quiet", check=False).returncode
        if rc == 1:
            return True
        if rc != 0:
            raise SyncError(f"git diff {' '.join(cached)}--quiet failed with exit {rc}")
    return False


def classify(root: Path) -> dict[str, object]:
    if not (root / ".git").exists():
        raise SyncError(f"{root} is not a git checkout")

    git(root, "fetch", "origin", "main", "--quiet")
    origin_main = git(root, "rev-parse", "origin/main").stdout.strip()
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()

    result: dict[str, object] = {
        "head": head,
        "origin_main": origin_main,
        "branch": branch or "(detached)",
    }

    # Dirt is checked first: a switch/ff must never run over tracked or staged
    # changes, so this outranks every position classification.
    if has_tracked_dirt(root):
        result["classification"] = "dirty_tracked"
        return result

    if head == origin_main:
        result["classification"] = "already_current" if branch == "main" else "aligned_to_main"
        return result

    base = git(root, "merge-base", head, origin_main).stdout.strip()
    if base == head:
        result["classification"] = "fast_forwarded"
    elif base == origin_main:
        result["classification"] = "active_branch"
    else:
        result["classification"] = "diverged"
    return result


def apply_sync(root: Path, info: dict[str, object]) -> str:
    """Perform the safe action for a classification. Returns an action note.

    current_runtime.py's freshness contract is HEAD == origin/main; the branch
    name is irrelevant. A shared canonical checkout is routinely detached (its
    `main` is checked out by another worktree), so this advances HEAD to
    origin/main without ever forcing a `git checkout main` that a worktree lock
    would fail — and without moving a feature branch ref that base==head proved
    carries no unique commits.
    """
    classification = info["classification"]
    branch = info["branch"]
    if classification == "already_current":
        return "no-op (on main at origin/main)"
    if classification == "aligned_to_main":
        return f"no-op (HEAD already at origin/main; branch {branch})"
    if classification == "fast_forwarded":
        if branch == "main":
            git(root, "merge", "--ff-only", "origin/main")
            return "fast-forwarded main to origin/main"
        git(root, "checkout", "--detach", "origin/main")
        note = "advanced detached HEAD to origin/main"
        if branch != "(detached)":
            note += f" (branch {branch} left untouched)"
        return note
    raise SyncError(f"apply refused for non-safe classification: {classification}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="Perform the switch/fast-forward for safe classes. Default is dry-run.")
    parser.add_argument("--repo", type=Path, default=DEFAULT_ROOT,
                        help="Checkout to synchronize (default: the persistent runtime checkout).")
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON only.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    try:
        info = classify(repo)
    except SyncError as error:
        payload = {"classification": "error", "error": str(error)}
        print(json.dumps(payload) if args.json else f"[main-sync] REFUSED: {error}",
              file=sys.stderr)
        return 1

    classification = info["classification"]
    safe = classification in SAFE
    action = None
    if args.apply and safe:
        try:
            action = apply_sync(repo, info)
            info["head"] = git(repo, "rev-parse", "HEAD").stdout.strip()
            # Re-verify post-apply: a concurrent writer between classify() and
            # here could leave the tree dirty or HEAD off origin/main. The
            # promise is an exact, clean origin/main, so confirm it or fail
            # closed rather than returning 0 on the stale classification.
            if info["head"] != info["origin_main"] or has_tracked_dirt(repo):
                raise SyncError(
                    "post-apply verification failed: checkout is not clean at "
                    f"origin/main (HEAD {info['head']}, origin/main {info['origin_main']})"
                )
        except SyncError as error:
            info["classification"] = "error"
            info["error"] = str(error)
            safe = False
            print(json.dumps(info) if args.json else f"[main-sync] REFUSED: {error}",
                  file=sys.stderr)
            return 1
    info["applied"] = bool(action)
    if action:
        info["action"] = action

    if args.json:
        print(json.dumps(info))
    else:
        stream = sys.stdout if safe else sys.stderr
        prefix = "[main-sync]" if safe else "[main-sync] BLOCKED:"
        detail = f" — {action}" if action else ""
        print(f"{prefix} {classification} HEAD {info['head']} "
              f"origin/main {info['origin_main']} branch {info['branch']}{detail}",
              file=stream)
    return 0 if safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
