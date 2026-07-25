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


def resolution_hint(relpath: str) -> str:
    """Flag a request whose subject has already been resolved.

    A packet listing unread forever is not the same as a decision still needed. The
    oldest item in this queue asked to ratify an option and said two PRs were held until
    then; both merged six hours later and the task moved to Tasks/done, so it presented
    itself as an urgent blocker for four days after ceasing to block anything. Checking
    the subject, not just the read flag, is what stops that class of ghost.
    """
    try:
        text = (ROOT / relpath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    notes: list[str] = []

    tasks = sorted(set(re.findall(r"TASK-[0-9A-F]{6}", text)))
    done = []
    for task in tasks:
        if any((ROOT / "Tasks" / "done").glob(f"*{task}*")):
            done.append(task)
    if done and len(done) == len(tasks):
        notes.append(f"task(s) completed: {', '.join(done)}")

    prs = sorted({int(n) for n in re.findall(r"(?:PR\s*)?#(\d{2,4})\b", text)})
    if prs:
        # All referenced PRs must be settled, exactly as tasks require all done. An
        # earlier version claimed moot when ANY single PR had merged, which mislabelled
        # a packet carrying three decisions because one of the three had shipped. A
        # wrong "moot" on a live request is worse than no hint at all.
        checked = prs[:PR_CHECK_LIMIT]
        settled, open_or_unknown = [], []
        for number in checked:
            try:
                raw = subprocess.run(
                    ["gh", "pr", "view", str(number), "--repo", "pixexid/llm-collab",
                     "--json", "state"],
                    capture_output=True, text=True, timeout=20, check=True).stdout
                state = json.loads(raw).get("state")
            except Exception:
                open_or_unknown.append(f"#{number}")
                continue
            (settled if state in {"MERGED", "CLOSED"} else open_or_unknown).append(f"#{number}")

        if settled and not open_or_unknown and len(checked) == len(prs):
            notes.append(f"referenced PR(s) already settled: {', '.join(settled)}")
        elif settled:
            still = ", ".join(open_or_unknown) or "unchecked references"
            dropped = f" ({len(prs) - len(checked)} more not checked)" if len(prs) > len(checked) else ""
            notes.append(
                f"PARTIAL — settled: {', '.join(settled)}; still open: {still}{dropped}"
            )

    return "; ".join(notes)

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


def open_prs() -> list[dict]:
    try:
        raw = subprocess.run(
            ["gh", "pr", "list", "--repo", "pixexid/llm-collab", "--state", "open",
             "--json", "number,title,isDraft,headRefName,headRefOid"],
            capture_output=True, text=True, timeout=30, check=True).stdout
        return json.loads(raw)
    except Exception:
        return []


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
            hint = resolution_hint(relpath)
            status = f"**likely moot** — {hint}" if hint else "awaiting you"
            add(f"| {age_of(stamp)} | {title} | {status} |")
        add("")
        add("Items marked *likely moot* reference work that has since merged or "
            "completed; verify before spending attention on them.")
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

    prs = open_prs()
    add("## 3. Open pull requests")
    add("")
    if not prs:
        add("None open, or GitHub is unreachable.")
    else:
        add("| pr | state | head | title |")
        add("|---|---|---|---|")
        for pr in sorted(prs, key=lambda p: -p["number"]):
            # Draft does NOT mean "with the implementer" here: this workspace reviews
            # drafts and only marks ready to open the merge settle window. Asserting a
            # court the flag does not carry would misdirect the one reader of this file.
            state = "draft" if pr["isDraft"] else "ready — settling for merge"
            add(f"| #{pr['number']} | {state} | `{pr['headRefOid'][:10]}` | {pr['title'][:60]} |")
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
