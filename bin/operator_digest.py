#!/usr/bin/env python3
"""
operator_digest.py — render one readable page answering the operator's two questions:

  1. What is blocked on me?
  2. What is each worker doing, and is any idle with work outstanding?

Reads the mailboxes, session records and (optionally) GitHub. Writes one markdown file.
Read-only apart from that file: it creates no bindings, answers no approvals, and
changes no session state.

  python bin/operator_digest.py            # writes State/operator-digest.md
  python bin/operator_digest.py --stdout   # print instead
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone

from _helpers import ROOT, agent_ids

DECISION_MARKERS = ("decision", "required", "approve", "ratify", "recommend", "blocked")
# Bounded so one packet citing many PRs cannot stall the digest on network calls.
# Exceeding it downgrades the hint to PARTIAL rather than claiming full coverage.
PR_CHECK_LIMIT = 6
_repo_targets_cache: dict[str, str] | None = None


def git_origin_slug() -> str | None:
    """The owner/name of this checkout's origin, or None when it cannot be read.

    Extracted so tests can fixture it instead of stubbing subprocess wholesale, which
    blocked the legitimate git call and made an unrelated test fail.
    """
    try:
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        # Narrow deliberately. A bare `except Exception` here swallowed a NameError from a
        # missing import and silently dropped THIS workspace's own repo from the operator's
        # PR view, reporting other projects' PRs while hiding the five in front of them.
        return None
    origin = re.sub(r"^(?:https://github\.com/|git@github\.com:)", "", origin)
    slug = origin.removesuffix(".git")
    return slug if "/" in slug else None


def known_repo_targets() -> dict[str, str]:
    """Map every name a packet may use for a repo onto its owner/name slug.

    Derived from the git origin and projects.json rather than hardcoded, because the
    previous constant asserted that every `#123` in this workspace belonged to
    pixexid/llm-collab while project amiga's REGISTERED repo is pixexid/amiga.

    The workspace's own name is taken from the origin's basename, not from
    collab.config.json: config_get exits the process when that ignored file is absent, so
    reading it here made a reporting helper fatal in a detached checkout for a value the
    origin already carries.
    """
    global _repo_targets_cache
    if _repo_targets_cache is not None:
        return _repo_targets_cache

    targets: dict[str, str] = {}

    def register(slug: str | None) -> None:
        if not slug or "/" not in slug:
            return
        targets[slug] = slug
        targets[slug.split("/")[-1]] = slug

    register(git_origin_slug())

    try:
        payload = json.loads((ROOT / "projects.json").read_text(encoding="utf-8"))
        for project in payload.get("projects", []):
            slug = (project.get("github") or {}).get("repo")
            register(slug)
            if slug and project.get("id"):
                targets[str(project["id"])] = slug
    except (OSError, json.JSONDecodeError):
        pass

    _repo_targets_cache = targets
    return targets


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_stamp(name: str) -> datetime | None:
    """Packet filenames start with an ISO-ish UTC stamp: 2026-07-25T05-40-10_..."""
    head = name.split("_", 1)[0]
    try:
        return datetime.strptime(head, "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_of(stamp: datetime | None) -> str:
    if stamp is None:
        return "unknown age"
    delta = now() - stamp
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def read_inbox(agent: str) -> dict:
    path = ROOT / "agents" / agent / "inbox.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def packet_title(relpath: str) -> str:
    name = Path(relpath).name
    body = name.split("_", 2)[-1] if "_" in name else name
    return body.removesuffix(".md").replace("-", " ")


def sender_of(relpath: str) -> str:
    parts = Path(relpath).name.split("_")
    for part in parts:
        if part.startswith("to-"):
            return "→ " + part[3:]
    return "?"


def declared_repo(text: str) -> str | None:
    """The single repo a packet's frontmatter attributes its bare PR numbers to.

    `repo_targets: ["llm-collab"]` IS an explicit attribution -- it is the field the
    delivery contract uses for exactly this. Ignoring it left every real packet's hint
    inert. Two or more targets stay ambiguous, and an unknown name is not guessed.
    """
    declared = re.search(r"^repo_targets:\s*\[(.*?)\]", text, re.MULTILINE)
    if not declared or not declared.group(1).strip():
        return None
    named = [t.strip().strip('"\'') for t in declared.group(1).split(",") if t.strip()]
    if len(named) != 1:
        return None
    return known_repo_targets().get(named[0])


def qualified_pr_refs(text: str) -> tuple[list[tuple[str, int]], int]:
    """Return (repo-qualified PR refs, count of bare unattributable ones).

    A bare `#170` names no repository. This workspace's packets are scoped to project
    `amiga`, whose REGISTERED repo is pixexid/amiga -- not pixexid/llm-collab -- so
    guessing this repo could report a same-numbered PR's state from the wrong project and
    declare a live request settled on it. Only an explicitly qualified reference is
    checkable; bare ones stay unresolved and count against settlement.
    """
    qualified: list[tuple[str, int]] = []
    for owner, repo, number in re.findall(
        r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", text
    ):
        qualified.append((f"{owner}/{repo}", int(number)))
    for owner, repo, number in re.findall(r"\b([\w.-]+)/([\w.-]+)#(\d{1,5})\b", text):
        qualified.append((f"{owner}/{repo}", int(number)))

    attributed = {number for _, number in qualified}
    bare = {
        int(n) for n in re.findall(r"(?<![\w/])#(\d{2,4})\b", text)
    } - attributed

    # Frontmatter naming exactly one known repo attributes the bare numbers to it.
    owner = declared_repo(text)
    if owner and bare:
        qualified.extend((owner, number) for number in sorted(bare))
        bare = set()

    unique = sorted(set(qualified))
    return unique, len(bare)


def pr_state(repo: str, number: int) -> str | None:
    try:
        raw = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", repo, "--json", "state"],
            capture_output=True, text=True, timeout=20, check=True).stdout
        return json.loads(raw).get("state")
    except Exception:
        return None


def resolution_hint(relpath: str) -> tuple[bool, str]:
    """Return (fully_settled, note) for one packet.

    A packet listing unread forever is not the same as a decision still needed. The oldest
    item in this queue asked to ratify an option and said two PRs were held until then;
    both merged and the task moved to Tasks/done, so it presented as an urgent blocker for
    four days after ceasing to block anything.

    Authority is RETURNED, never inferred from this note's wording. An earlier version made
    the caller test the note's prefix, so a packet whose note began with a completed-task
    clause -- or with "PR reference(s) not checked" -- was rendered moot although live work
    remained inside it. Anything unresolved, including an unreachable PR or a bare
    unattributable reference, counts against settlement.
    """
    try:
        text = (ROOT / relpath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, ""

    notes: list[str] = []
    saw_reference = False
    settled_everywhere = True

    tasks = sorted(set(re.findall(r"TASK-[0-9A-F]{6}", text)))
    if tasks:
        saw_reference = True
        done = [t for t in tasks if any((ROOT / "Tasks" / "done").glob(f"*{t}*"))]
        if len(done) == len(tasks):
            notes.append(f"task(s) completed: {', '.join(done)}")
        else:
            settled_everywhere = False
            if done:
                notes.append(f"task(s) completed: {', '.join(done)}; "
                             f"still open: {', '.join(t for t in tasks if t not in done)}")

    qualified, bare_count = qualified_pr_refs(text)
    if bare_count:
        saw_reference = True
        settled_everywhere = False
        notes.append(f"{bare_count} bare PR reference(s) not checked: no repository named, "
                     f"and this project's registered repo is not assumed")

    if qualified:
        saw_reference = True
        checked = qualified[:PR_CHECK_LIMIT]
        if len(qualified) > len(checked):
            settled_everywhere = False
            notes.append(f"{len(qualified) - len(checked)} further PR ref(s) not checked")
        settled, open_or_unknown = [], []
        for repo, number in checked:
            state = pr_state(repo, number)
            label = f"{repo}#{number}"
            (settled if state in {"MERGED", "CLOSED"} else open_or_unknown).append(label)
        if open_or_unknown:
            settled_everywhere = False
        if settled and not open_or_unknown:
            notes.append(f"PR(s) already settled: {', '.join(settled)}")
        elif settled:
            notes.append(f"settled: {', '.join(settled)}; still open or unknown: "
                         f"{', '.join(open_or_unknown)}")

    if not saw_reference:
        return False, ""
    return settled_everywhere, "; ".join(n for n in notes if n)


def decision_status(relpath: str) -> str:
    """The operator-facing verdict for one row.

    Only a hint with nothing left open may read as moot. An earlier version prefixed
    every hint with "likely moot", so a PARTIAL hint rendered as
    "**likely moot** -- PARTIAL -- settled: #299; still open: #302" -- a row that tells
    the reader to skip an item while naming the live work inside it.
    """
    fully_settled, note = resolution_hint(relpath)
    if not note:
        return "awaiting you"
    if fully_settled:
        return f"**likely moot** — {note}"
    return f"**awaiting you** — {note}"


def pending_for_operator() -> list[tuple[datetime | None, str, str]]:
    rows = []
    for relpath in read_inbox("operator").get("unread", []):
        stamp = parse_stamp(Path(relpath).name)
        rows.append((stamp, packet_title(relpath), relpath))
    rows.sort(key=lambda r: (r[0] is None, r[0] or now()), reverse=True)
    return rows


def worker_sessions() -> tuple[list[dict], int]:
    """Return (dispatchable sessions, count of stale ones).

    Status alone is misleading: a record can sit at status=active with an expired lease
    forever. Listing those as live produced 32 rows where 2 mattered, and made a dozen
    dead records look like a dozen chats sharing one native thread. Only genuinely
    dispatchable sessions are shown; the rest are counted.
    """
    sys.path.insert(0, str(ROOT / "bin"))
    from _session_autobridge import session_is_dispatchable

    sessions_dir = ROOT / "State" / "session_autobridge" / "sessions"
    live: list[dict] = []
    stale = 0
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("status") not in {"active", "parked"}:
            continue
        dispatchable, _ = session_is_dispatchable(record)
        if dispatchable:
            live.append(record)
        else:
            stale += 1
    return live, stale


def duplicate_native_ids(sessions: list[dict]) -> dict[str, list[str]]:
    """Live sessions sharing one native session id — a real routing hazard."""
    seen: dict[str, list[str]] = {}
    for record in sessions:
        native = (record.get("runtime") or {}).get("session_id")
        if not native:
            continue
        seen.setdefault(str(native), []).append(str(record.get("chat_id") or "-"))
    return {k: v for k, v in seen.items() if len(v) > 1}


def open_prs() -> tuple[list[dict], list[str]]:
    """Open PRs across every registered repo, plus the repos that could not be read.

    Querying one repo under the heading "Open pull requests" let the digest report None
    while another registered project repo had open work. Each row now carries its repo, and
    a repo we failed to reach is named rather than silently rendered as empty.
    """
    rows: list[dict] = []
    unreachable: list[str] = []
    for slug in sorted(set(known_repo_targets().values())):
        try:
            raw = subprocess.run(
                ["gh", "pr", "list", "--repo", slug, "--state", "open",
                 "--json", "number,title,isDraft,headRefName,headRefOid"],
                capture_output=True, text=True, timeout=30, check=True).stdout
            for row in json.loads(raw):
                row["repo"] = slug
                rows.append(row)
        except Exception:
            unreachable.append(slug)
    rows.sort(key=lambda r: (r["repo"], -int(r["number"])))
    return rows, unreachable


def unread_counts() -> list[tuple[str, int]]:
    rows = []
    for agent in agent_ids():
        count = len(read_inbox(agent).get("unread", []))
        if count:
            rows.append((agent, count))
    rows.sort(key=lambda r: -r[1])
    return rows


def render() -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# Operator digest — {now().strftime('%Y-%m-%d %H:%M')} UTC")
    add("")

    pending = pending_for_operator()
    decisions = [r for r in pending if any(m in r[1].lower() for m in DECISION_MARKERS)]
    add("## 1. Blocked on you")
    add("")
    if not decisions:
        add("Nothing addressed to you is awaiting a decision.")
    else:
        add("| age | what | status |")
        add("|---|---|---|")
        for stamp, title, relpath in decisions[:12]:
            add(f"| {age_of(stamp)} | {title} | {decision_status(relpath)} |")
        add("")
        add("Only *likely moot* means every referenced task and PR is settled; verify "
            "before dropping it. Everything else is awaiting you, including rows whose "
            "note lists what could not be checked. A bare `#123` names no repository, so "
            "it is reported rather than guessed -- reference PRs as `owner/repo#123` to "
            "make them checkable.")
    add("")
    other = [r for r in pending if r not in decisions]
    if other:
        add(f"Plus {len(other)} other unread packet(s) addressed to you, oldest "
            f"{age_of(min((r[0] for r in other if r[0]), default=None))}.")
        add("")

    add("## 2. Workers")
    add("")
    sessions, stale = worker_sessions()
    if not sessions:
        add("No dispatchable sessions.")
    else:
        add("| agent | project / chat | mode | wake | native session |")
        add("|---|---|---|---|---|")
        for s in sessions:
            runtime = s.get("runtime") or {}
            native = str(runtime.get("session_id") or "-")
            add(f"| {s.get('agent_id')} | {s.get('project_id') or '-'} / "
                f"{s.get('chat_id') or '-'} | {s.get('mode')} | {s.get('wake_strategy')} | "
                f"`{native[:18]}` |")
    add("")
    if stale:
        add(f"{stale} further session record(s) sit at active/parked with an expired "
            f"lease and cannot receive delivery. They are excluded above.")
        add("")
    clashes = duplicate_native_ids(sessions)
    if clashes:
        add("> **Routing hazard:** live sessions sharing one native session id — "
            "messages for different tasks would land in the same thread.")
        add(">")
        for native, chats in clashes.items():
            add(f"> - `{native}` ← {', '.join(chats)}")
        add("")

    prs, unreachable_repos = open_prs()
    queried = sorted(set(known_repo_targets().values()))
    add("## 3. Open pull requests")
    add("")
    add(f"Across every registered repo: {', '.join(queried) or 'none registered'}.")
    add("")
    if not prs:
        add("None open in any registered repo.")
    else:
        add("| pr | repo | state | head | title |")
        add("|---|---|---|---|---|")
        for pr in prs:
            # Draft does NOT mean "with the implementer" here: this workspace reviews
            # drafts and only marks ready to open the merge settle window. Asserting a
            # court the flag does not carry would misdirect the one reader of this file.
            state = "draft" if pr["isDraft"] else "ready — settling for merge"
            add(f"| #{pr['number']} | {pr['repo']} | {state} | "
                f"`{pr['headRefOid'][:10]}` | {pr['title'][:55]} |")
    if unreachable_repos:
        add("")
        add(f"Could not read: {', '.join(unreachable_repos)} — treat as unknown, not empty.")
    add("")

    counts = unread_counts()
    if counts:
        add("## 4. Unread mail per agent")
        add("")
        add(", ".join(f"**{agent}**: {count}" for agent, count in counts))
        add("")

    add("---")
    add("")
    add("Regenerate with `python bin/operator_digest.py`. Read-only: creates no "
        "bindings, answers no approvals, changes no session state.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the operator digest.")
    parser.add_argument("--out", default=str(ROOT / "State" / "operator-digest.md"))
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing")
    args = parser.parse_args()

    text = render()
    if args.stdout:
        print(text)
        return
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"[digest] {target}")


if __name__ == "__main__":
    main()
