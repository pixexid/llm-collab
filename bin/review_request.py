#!/usr/bin/env python3
"""
review_request.py — post a Codex review request whose SHA can only be real.

The exact-head SHA in a review request is what every terminal signal binds to,
so it must never be hand-typed: PR #347 came to contain a fabricated,
later-retracted SHA precisely because a model typed one. This tool has no
--sha option and rejects SHA-shaped caller text. It reads the PR head from
GitHub, reads the local HEAD, and refuses on mismatch — there is no bypass
flag. It enforces the request budget from docs/workflows/commit-push-prs.md:
one initial request per candidate head, plus the single request-anchored
re-trigger as the only exempted recovery, repeated verbatim from the initial
request it is anchored to. The request history is enumerated exhaustively by
pagination inside a declared bound; reaching the bound with pages outstanding
fails closed rather than treating a truncated history as an empty one.

  bin/review_request.py --pr 356 --project llm-collab --tier A \
      --contract 352 --focus "exact-session authority selection, bounded reads"
  bin/review_request.py --pr 356 --project llm-collab --retrigger
  bin/review_request.py --pr 356 --project llm-collab --tier B \
      --focus "..." --dry-run

Exits 0 after posting (or printing with --dry-run); exits 2 with the
reason on any refusal. Read-only against the workspace; the only write is the
PR comment.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import os
import re
import subprocess

from _helpers import parse_frontmatter

REQUEST_MARKER = "@codex review"
COMMENT_PAGE_SIZE = 100
# Declared bound on the exhaustive enumeration. Hitting it with pages still
# outstanding fails closed: a truncated history must never read as "no prior
# request", because that is exactly how a budget silently resets.
COMMENT_HARD_CAP = 1000
COMMENT_PAGE_HARD_CAP = COMMENT_HARD_CAP // COMMENT_PAGE_SIZE
PROJECTS_MAX_BYTES = 1_000_000
TASK_CONTRACT_ENTRY_HARD_CAP = 5000
TASK_CONTRACT_MAX_BYTES = 1_000_000
SCRIPT_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
TASK_CONTRACT_RE = re.compile(r"TASK-[A-Z0-9]+")
GH_READ_TIMEOUT_SECONDS = 30
GH_POST_TIMEOUT_SECONDS = 45
SHA_SHAPED_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
EXACT_HEAD_WORDING_RE = re.compile(r"exact\s+head", re.IGNORECASE)
RETRIGGER_NOTE = (
    "Re-triggered once as the single exempted recovery: the initial request "
    "for this exact head is repeated verbatim above."
)

COMMENTS_QUERY = f"""query($owner: String!, $name: String!, $pr: Int!, $after: String) {{
  viewer {{ login }}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $pr) {{
      comments(first: {COMMENT_PAGE_SIZE}, after: $after) {{
        nodes {{ body author {{ login }} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
  }}
}}"""


def run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"error: {' '.join(argv[:3])} exceeded its {timeout}s deadline; "
            "failing closed rather than judging on a stalled read"
        )
    except OSError as error:
        raise SystemExit(f"error: cannot run {argv[0]}: {error}")
    if proc.returncode != 0:
        raise SystemExit(f"error: {' '.join(argv[:3])} failed: {proc.stderr.strip()}")
    return proc


def run_json(argv: list[str], timeout: int = GH_READ_TIMEOUT_SECONDS) -> object:
    try:
        return json.loads(run(argv, timeout).stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"error: {' '.join(argv[:3])} returned malformed JSON: {error}"
        )


def coordination_root() -> Path:
    common_dir = Path(
        run(
            [
                "git", "-C", str(SCRIPT_CHECKOUT_ROOT), "rev-parse",
                "--path-format=absolute", "--git-common-dir",
            ],
            GH_READ_TIMEOUT_SECONDS,
        ).stdout.strip()
    )
    return common_dir.parent


def common_checkout_projects() -> list[dict]:
    path = coordination_root() / "projects.json"
    try:
        if path.stat().st_size > PROJECTS_MAX_BYTES:
            raise SystemExit(
                f"error: {path} exceeds the {PROJECTS_MAX_BYTES}-byte registry bound"
            )
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"error: cannot read registered projects from {path}: {error}")
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise SystemExit(f"error: {path} has no valid projects list")
    return projects


def repo_coordinates(project_id: str, projects: list[dict] | None = None) -> tuple[str, str]:
    if projects is None:
        projects = common_checkout_projects()
    project = next((p for p in projects if p.get("id") == project_id), None)
    if project is None:
        raise SystemExit(f"error: unknown project_id: {project_id!r}")
    github = project.get("github")
    if not isinstance(github, dict) or github.get("enabled") is not True:
        raise SystemExit(
            f"error: project {project_id!r} has no enabled GitHub registration"
        )
    repo = github.get("repo", "")
    if "/" not in repo:
        raise SystemExit(
            f"error: project {project_id!r} has no github.repo registration; "
            "refusing to infer the repository from ambient checkout state"
        )
    owner, name = repo.split("/", 1)
    return owner, name


def pr_head(pr: int, owner: str, name: str) -> str:
    data = run_json(
        ["gh", "pr", "view", str(pr), "--repo", f"{owner}/{name}",
         "--json", "headRefOid,state"]
    )
    if data.get("state") != "OPEN":
        raise SystemExit(
            f"error: {owner}/{name}#{pr} is not open; refusing to post a review request"
        )
    return data["headRefOid"]


def require_contract(
    contract: str, project_id: str, owner: str, name: str
) -> None:
    if contract.isdecimal() and int(contract) > 0:
        run_json(
            [
                "gh", "issue", "view", contract, "--repo", f"{owner}/{name}",
                "--json", "number",
            ]
        )
        return
    if TASK_CONTRACT_RE.fullmatch(contract):
        root = coordination_root()
        matches: list[Path] = []
        entries = 0
        try:
            for folder in ("active", "backlog", "done"):
                task_dir = root / "Tasks" / folder
                if not task_dir.is_dir():
                    continue
                with os.scandir(task_dir) as task_entries:
                    for entry in task_entries:
                        entries += 1
                        if entries > TASK_CONTRACT_ENTRY_HARD_CAP:
                            raise SystemExit(
                                "error: task-contract scan exceeds the declared "
                                f"entry bound ({TASK_CONTRACT_ENTRY_HARD_CAP}); "
                                "failing closed"
                            )
                        if (
                            entry.name.endswith(f"__{contract}.md")
                            and entry.is_file()
                        ):
                            matches.append(Path(entry.path))
        except OSError as error:
            raise SystemExit(f"error: cannot scan task contracts: {error}")
        if not matches:
            raise SystemExit(
                f"error: task-hosted lane contract {contract} does not exist"
            )
        if len(matches) != 1:
            raise SystemExit(
                f"error: task-hosted lane contract {contract} is ambiguous"
            )
        path = matches[0]
        try:
            if path.stat().st_size > TASK_CONTRACT_MAX_BYTES:
                raise SystemExit(
                    f"error: {path} exceeds the {TASK_CONTRACT_MAX_BYTES}-byte "
                    "task-contract bound"
                )
            frontmatter, _ = parse_frontmatter(path.read_text())
        except OSError as error:
            raise SystemExit(f"error: cannot read task contract {path}: {error}")
        if (
            frontmatter.get("task_id") != contract
            or frontmatter.get("project_id") != project_id
        ):
            raise SystemExit(
                f"error: task-hosted lane contract {contract} is not bound to "
                f"project {project_id!r}"
            )
        return
    raise SystemExit(
        "error: --contract must be a positive issue number or TASK-id"
    )


def pr_comment_bodies(pr: int, owner: str, name: str) -> list[str]:
    bodies: list[str] = []
    after: str | None = None
    cursors: set[str] = set()
    pages = 0
    while True:
        pages += 1
        if pages > COMMENT_PAGE_HARD_CAP:
            raise SystemExit(
                f"error: comment history exceeds the declared page bound "
                f"({COMMENT_PAGE_HARD_CAP}); failing closed"
            )
        argv = [
            "gh", "api", "graphql",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr}",
            "-f", f"query={COMMENTS_QUERY}",
        ]
        if after is not None:
            argv += ["-f", f"after={after}"]
        data = run_json(argv)
        viewer_login = data["data"]["viewer"]["login"]
        comments = data["data"]["repository"]["pullRequest"]["comments"]
        bodies.extend(
            node["body"]
            for node in comments["nodes"]
            if node.get("author", {}).get("login") == viewer_login
        )
        if len(bodies) > COMMENT_HARD_CAP:
            raise SystemExit(
                f"error: comment history exceeds the declared bound "
                f"({COMMENT_HARD_CAP}); failing closed"
            )
        page = comments["pageInfo"]
        if not page["hasNextPage"]:
            return bodies
        if len(bodies) >= COMMENT_HARD_CAP:
            raise SystemExit(
                f"error: comment history exceeds the declared bound "
                f"({COMMENT_HARD_CAP}) with pages still outstanding; failing "
                "closed rather than treating a truncated history as an empty one"
            )
        cursor = page.get("endCursor")
        if not cursor or cursor == after or cursor in cursors:
            raise SystemExit(
                "error: GitHub comment pagination did not advance; failing closed"
            )
        cursors.add(cursor)
        after = cursor


def local_head() -> str:
    return run(["git", "rev-parse", "HEAD"], GH_READ_TIMEOUT_SECONDS).stdout.strip()


def prior_requests(bodies: list[str], sha: str) -> list[str]:
    return [b for b in bodies if b.startswith(REQUEST_MARKER) and sha in b]


def reject_caller_supplied_shas(fields: dict[str, str]) -> None:
    for label, value in fields.items():
        if SHA_SHAPED_RE.search(value) or EXACT_HEAD_WORDING_RE.search(value):
            raise SystemExit(
                f"error: --{label} contains a SHA-shaped value or exact-head "
                "wording; the head is sourced from GitHub and the checkout, "
                "never from caller text"
            )


def build_request_body(
    focus: str, sha: str, contract: str | int | None = None, note: str | None = None
) -> str:
    if not focus.strip():
        raise SystemExit("error: --focus must name at least one review lens")
    parts = [f"{REQUEST_MARKER} for {focus.strip()} at exact head `{sha}`."]
    if contract is not None:
        contract_ref = str(contract)
        if contract_ref.isdecimal():
            contract_ref = f"#{contract_ref}"
        parts.append(
            f"Review the full diff against the lane contract in {contract_ref} "
            "through those lenses."
        )
    else:
        parts.append("Please review the full diff through those lenses.")
    if note:
        parts.append(note.strip())
    return " ".join(parts)


def post_comment(pr: int, owner: str, name: str, body: str) -> None:
    try:
        proc = subprocess.run(
            ["gh", "pr", "comment", str(pr), "--repo", f"{owner}/{name}",
             "--body", body],
            capture_output=True,
            text=True,
            timeout=GH_POST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            "error: posting the request timed out; the comment may or may not "
            "have landed — inspect the PR before retrying"
        )
    except OSError as error:
        raise SystemExit(
            f"error: cannot run gh: {error}; no review request was posted"
        )
    if proc.returncode != 0:
        raise SystemExit(
            f"error: posting the request failed: {proc.stderr.strip()}; the "
            "comment may or may not have landed — inspect the PR before retrying"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--project", required=True,
        help="registered project_id; the repository is resolved from its "
        "projects.json entry, never from ambient checkout state",
    )
    parser.add_argument(
        "--tier", choices=("A", "B", "C"),
        help="review tier; Tier A requires --contract naming the lane-contract issue",
    )
    parser.add_argument(
        "--focus",
        help="comma-separated review lenses (every Tier A family the diff touches)",
    )
    parser.add_argument(
        "--contract", default=None,
        help="issue number or TASK-id carrying the lane contract (required for --tier A)",
    )
    parser.add_argument("--note", default=None, help="one extra sentence appended verbatim")
    parser.add_argument(
        "--retrigger", action="store_true",
        help="repeat the single prior request for this exact head verbatim, as "
        "the one exempted recovery for a silently dropped request",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not post")
    args = parser.parse_args(argv)

    if args.retrigger:
        if (
            args.tier is not None
            or args.focus is not None
            or args.contract is not None
            or args.note is not None
        ):
            raise SystemExit(
                "error: --retrigger repeats the initial request verbatim; "
                "--tier/--focus/--contract/--note cannot amend its scope"
            )
    else:
        if args.tier is None:
            raise SystemExit("error: --tier is required for an initial request")
        if args.tier == "C":
            raise SystemExit("error: Tier C changes do not request review")
        if args.focus is None:
            raise SystemExit("error: --focus is required for an initial request")
        if args.tier == "A" and args.contract is None:
            raise SystemExit(
                "error: Tier A requires --contract naming the issue that carries "
                "the lane contract; a generic full-diff request does not "
                "satisfy the Tier A gate"
            )
        reject_caller_supplied_shas(
            {k: v for k, v in (("focus", args.focus), ("note", args.note)) if v}
        )

    owner, name = repo_coordinates(args.project)
    if args.tier == "A":
        require_contract(args.contract, args.project, owner, name)
    sha = pr_head(args.pr, owner, name)
    local = local_head()
    if local != sha:
        raise SystemExit(
            f"error: local HEAD {local} != PR head {sha}; push the verified "
            "head first — the request must bind to the head that received the "
            "lane's local verification"
        )

    priors = prior_requests(pr_comment_bodies(args.pr, owner, name), sha)
    if args.retrigger:
        if not priors:
            raise SystemExit(
                "error: no initial request for this exact head exists to "
                "repeat; a re-trigger must anchor to one"
            )
        if len(priors) >= 2:
            raise SystemExit(
                f"error: request budget for this head is spent ({len(priors)} "
                "requests already); there is no further request to issue — "
                "continue to the exact-head release-gate disposition"
            )
        body = priors[0] + "\n\n" + RETRIGGER_NOTE
    else:
        if priors:
            raise SystemExit(
                "error: an initial request for this exact head already exists; "
                "pass --retrigger only if the connector silently dropped it "
                "(the single exempted recovery)"
            )
        body = build_request_body(args.focus, sha, args.contract, args.note)

    if args.dry_run:
        print(body)
        return 0
    if pr_head(args.pr, owner, name) != sha:
        raise SystemExit(
            "error: PR head changed while constructing the request; nothing was posted"
        )
    post_comment(args.pr, owner, name, body)
    if pr_head(args.pr, owner, name) != sha:
        raise SystemExit(
            "error: review request was posted, but the PR head changed during "
            "publication; the posted request is stale and a new-head request is required"
        )
    print(f"posted review request for exact head {sha} on {owner}/{name}#{args.pr}")
    return 0


def cli() -> int:
    try:
        return main()
    except SystemExit as error:
        if isinstance(error.code, str):
            print(error.code, file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(cli())
