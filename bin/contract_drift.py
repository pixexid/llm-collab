#!/usr/bin/env python3
"""Find instruction copies that have drifted from the canonical contract.

Workers cache instructions in several places -- per-agent memory files, skills, project
notes, branches. Those copies go stale SILENTLY: on 2026-07-25 every agent memory file
still taught the `deliver.py` invocation without `--repo-targets`, which is the exact
command that had just silently dropped 27 packets over eleven hours.

A canonical document does not fix that on its own, because nobody re-reads a document
they believe they already know. This turns drift from invisible into reported.

Each rule below is derived from a failure that actually happened, not from a style
preference. Add a rule when a stale copy has cost real time; delete one that turns noisy.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = "AGENTS.md and docs/workflows/collab-thread-quickstart.md"


class Rule:
    def __init__(self, name: str, why: str, matches) -> None:
        self.name = name
        self.why = why
        self.matches = matches


def looks_like_an_invocation(block: str) -> bool:
    """A runnable command, not prose that happens to name the tool.

    The first version of this rule matched any line containing `deliver.py`, so it flagged
    the sentence in a memory file EXPLAINING that deliver.py needs --repo-targets. A
    checker built to catch stale instructions should not be fooled by an accurate one; a
    mention is not an instruction. Requiring the addressing flags is what separates them.
    """
    return "--from" in block and "--to" in block


def deliver_without_repo_targets(text: str) -> list[str]:
    """A documented deliver.py COMMAND that declares no repo scope."""
    hits = []
    for block in re.findall(r"[^\n]*deliver\.py[^\n]*(?:\n\s+[^\n]*)*", text):
        if not looks_like_an_invocation(block):
            continue
        if "--repo-targets" not in block and "--help" not in block:
            hits.append(" ".join(block.split())[:110])
    return hits


def claims_review_is_manual_only(text: str) -> list[str]:
    """Text caching the retired manual-only review policy."""
    stale = re.compile(
        r"review.{0,40}manual only"
        r"|automatic review.{0,20}(?:off|disabled)"
        r"|nothing arrives unless requested",
        re.IGNORECASE,
    )
    normalized = " ".join(text.split())
    return [match.group(0)[:110] for match in stale.finditer(normalized)]


def chat_last_in_a_send_command(text: str) -> list[str]:
    """`--chat last` in a documented send: it resolves to the most recent chat across
    projects, so once a second lane is active it addresses the wrong one."""
    return [
        " ".join(line.split())[:110]
        for line in text.splitlines()
        if "deliver.py" in line and "--chat last" in line and looks_like_an_invocation(line)
    ]


RULES = [
    Rule("deliver-without-repo-targets",
         "a packet with no repo scope is written and then refused at dispatch; this "
         "silently dropped 27 packets",
         deliver_without_repo_targets),
    Rule("manual-only-review-policy",
         "every PR now waits for one automatic bot pass; cached manual-only guidance "
         "can authorize a premature merge",
         claims_review_is_manual_only),
    Rule("chat-last-in-send",
         "`--chat last` resolves across projects and addresses the wrong lane once a "
         "second project is active",
         chat_last_in_a_send_command),
]


def instruction_files(agent: str | None) -> list[Path]:
    """Instruction caches to scan.

    With an agent, ONLY that agent's own files. Session bootstrap runs this on every
    session start, and globbing the whole docs tree there forked a process and walked the
    repository each time -- enough extra load that it perturbed unrelated socket tests.
    Your own drift is also the only drift you can act on. The full sweep is `--all`.
    """
    patterns = ((f"agents/{agent}/*.md",) if agent
                else ("agents/*/*.md", "docs/**/*.md", "*.md", ".claude/skills/**/*.md"))
    found: list[Path] = []
    for pattern in patterns:
        found.extend(sorted(ROOT.glob(pattern)))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", help="Scan only this agent's own instruction files")
    parser.add_argument("--all", action="store_true",
                        help="Sweep every instruction cache in the workspace")
    args = parser.parse_args()

    findings: list[tuple[Path, Rule, str]] = []
    for path in instruction_files(None if args.all else args.agent):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "contract-drift: allow" in text:
            continue
        for rule in RULES:
            for hit in rule.matches(text):
                findings.append((path.relative_to(ROOT), rule, hit))

    if not findings:
        print("[contract-drift] no drifted instruction copies found")
        return 0

    print(f"[contract-drift] {len(findings)} drifted instruction(s); "
          f"canonical source is {CANONICAL}\n")
    for path, rule, hit in findings:
        print(f"  {path}")
        print(f"    rule: {rule.name}")
        print(f"    why : {rule.why}")
        print(f"    text: {hit}\n")
    print("Fix by POINTING at the canonical document instead of restating it. A restated "
          "command is a cached copy that goes stale without telling anyone.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
