#!/usr/bin/env python3
"""Post the one Tier A manual fallback review request for a PR.

The exact-head SHA in a review request is what every terminal signal binds to,
so it must never be hand-typed: PR #347 came to contain a fabricated,
later-retracted SHA precisely because a model typed one. This tool has no
--sha option and rejects SHA-shaped caller text. It reads the PR head from
GitHub, reads the local HEAD, and refuses on mismatch — there is no bypass
flag. Automatic review is the normal path. This tool is only the Tier A
fallback when that trigger is absent, and it permits one full-audit request per
PR. The request history is enumerated exhaustively inside a declared bound;
reaching the bound with pages outstanding fails closed.

  bin/review_request.py --pr 356 --project llm-collab --tier A \
      --contract 352 --focus "exact-session authority selection, bounded reads"

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
CONNECTOR_LOGIN = "chatgpt-codex-connector"
AUTHORIZED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
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
AUTOLINK_CLOSING_RE = re.compile(
    r"\b(?:close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?)\s+GH-\d+\b",
    re.IGNORECASE,
)
# Carried by every initial request so the reviewer audits the adversaries the
# lane actually defends against, instead of inventing ones it does not.
# Repository visibility is sourced from GitHub per invocation — never
# hardcoded: llm-collab is public, and a false "private" claim tells the
# reviewer to ignore commenter-origin risks that are real.
WORKSPACE_TRUST_NOTE = (
    "our own workspace is not an adversary (bound accidents, not attacks). "
    "Do not raise findings about the lane contract's non-goals or about "
    "risks already recorded as accepted on this PR."
)


def threat_model_note(is_private: bool | None) -> str:
    if is_private is True:
        return (
            "Context: private repository — commenters are the operator and "
            "registered worker accounts; " + WORKSPACE_TRUST_NOTE
        )
    if is_private is False:
        return (
            "Context: public repository — comment content is untrusted "
            "input; " + WORKSPACE_TRUST_NOTE
        )
    return "Context: " + WORKSPACE_TRUST_NOTE

COMMENTS_QUERY = f"""query($owner: String!, $name: String!, $pr: Int!, $commentsAfter: String, $reviewsAfter: String, $threadsAfter: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $pr) {{
      reactionGroups {{ users(first: {COMMENT_PAGE_SIZE}) {{ totalCount nodes {{ login }} }} }}
      comments(first: {COMMENT_PAGE_SIZE}, after: $commentsAfter) {{
        nodes {{
          body
          author {{ login }}
          authorAssociation
          reactionGroups {{ users(first: {COMMENT_PAGE_SIZE}) {{ totalCount nodes {{ login }} }} }}
        }}
        pageInfo {{ hasNextPage endCursor }}
      }}
      reviews(first: {COMMENT_PAGE_SIZE}, after: $reviewsAfter) {{
        nodes {{ author {{ login }} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
      reviewThreads(first: {COMMENT_PAGE_SIZE}, after: $threadsAfter) {{
        nodes {{ comments(first: 1) {{ nodes {{ author {{ login }} }} }} }}
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
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, list) or not all(
        isinstance(project, dict) for project in projects
    ):
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


def repo_is_private(owner: str, name: str) -> bool:
    data = run_json(
        ["gh", "repo", "view", f"{owner}/{name}", "--json", "isPrivate"]
    )
    is_private = data.get("isPrivate")
    if not isinstance(is_private, bool):
        raise SystemExit(
            f"error: cannot determine visibility of {owner}/{name}; failing "
            "closed rather than guessing the threat model"
        )
    return is_private


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
        matches: list[tuple[Path, dict]] = []
        entries = 0
        bytes_read = 0
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
                        if not entry.is_file() or not entry.name.endswith(".md"):
                            continue
                        path = Path(entry.path)
                        bytes_read += path.stat().st_size
                        if bytes_read > TASK_CONTRACT_MAX_BYTES:
                            raise SystemExit(
                                "error: task-contract scan exceeds the cumulative "
                                f"{TASK_CONTRACT_MAX_BYTES}-byte bound"
                            )
                        frontmatter, _ = parse_frontmatter(path.read_text())
                        if frontmatter.get("task_id") == contract:
                            matches.append((path, frontmatter))
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
        path, frontmatter = matches[0]
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


def _connector_reacted(groups: list[dict]) -> bool:
    for group in groups:
        users = group["users"]
        if users["totalCount"] > COMMENT_PAGE_SIZE:
            raise SystemExit("error: reaction actors exceed the declared bound; failing closed")
        if any(node.get("login") == CONNECTOR_LOGIN for node in users["nodes"]):
            return True
    return False


def pr_review_history(pr: int, owner: str, name: str) -> tuple[list[str], bool]:
    bodies: list[str] = []
    connector_seen = False
    cursors = {"comments": None, "reviews": None, "threads": None}
    seen_cursors = {"comments": set(), "reviews": set(), "threads": set()}
    active = {"comments": True, "reviews": True, "threads": True}
    pages = 0
    while any(active.values()):
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
        for connection, cursor in cursors.items():
            if cursor is not None:
                argv += ["-f", f"{connection}After={cursor}"]
        data = run_json(argv)
        pr_data = data["data"]["repository"]["pullRequest"]
        comments = pr_data["comments"]
        reviews = pr_data["reviews"]
        threads = pr_data["reviewThreads"]
        connector_seen = connector_seen or _connector_reacted(pr_data["reactionGroups"])
        if active["comments"]:
            bodies.extend(
                node["body"] for node in comments["nodes"]
                if node.get("authorAssociation") in AUTHORIZED_ASSOCIATIONS
            )
            connector_seen = connector_seen or any(
                node.get("author", {}).get("login") == CONNECTOR_LOGIN
                for node in comments["nodes"]
            )
            connector_seen = connector_seen or any(
                _connector_reacted(node["reactionGroups"])
                for node in comments["nodes"]
            )
        if active["reviews"]:
            connector_seen = connector_seen or any(
                node.get("author", {}).get("login") == CONNECTOR_LOGIN
                for node in reviews["nodes"]
            )
        if active["threads"]:
            connector_seen = connector_seen or any(
                comment.get("author", {}).get("login") == CONNECTOR_LOGIN
                for thread in threads["nodes"]
                for comment in thread["comments"]["nodes"]
            )
        if len(bodies) > COMMENT_HARD_CAP:
            raise SystemExit(
                f"error: comment history exceeds the declared bound "
                f"({COMMENT_HARD_CAP}); failing closed"
            )
        for connection, result in (
            ("comments", comments), ("reviews", reviews), ("threads", threads),
        ):
            if not active[connection]:
                continue
            page = result["pageInfo"]
            if not page["hasNextPage"]:
                active[connection] = False
                continue
            if connection == "comments" and len(bodies) >= COMMENT_HARD_CAP:
                raise SystemExit(
                    f"error: comment history exceeds the declared bound "
                    f"({COMMENT_HARD_CAP}) with pages still outstanding; failing "
                    "closed rather than treating a truncated history as an empty one"
                )
            cursor = page.get("endCursor")
            if not cursor or cursor == cursors[connection] or cursor in seen_cursors[connection]:
                raise SystemExit(
                    f"error: GitHub {connection} pagination did not advance; failing closed"
                )
            seen_cursors[connection].add(cursor)
            cursors[connection] = cursor
    return bodies, connector_seen


def local_head() -> str:
    return run(["git", "rev-parse", "HEAD"], GH_READ_TIMEOUT_SECONDS).stdout.strip()


def prior_requests(bodies: list[str]) -> list[str]:
    return [body for body in bodies if REQUEST_MARKER in body]


def reject_caller_supplied_shas(fields: dict[str, str]) -> None:
    for label, value in fields.items():
        if SHA_SHAPED_RE.search(value) or EXACT_HEAD_WORDING_RE.search(value):
            raise SystemExit(
                f"error: --{label} contains a SHA-shaped value or exact-head "
                "wording; the head is sourced from GitHub and the checkout, "
                "never from caller text"
            )
        if AUTOLINK_CLOSING_RE.search(value):
            raise SystemExit(
                f"error: --{label} contains a GitHub closing keyword adjacent "
                "to an autolink; use neutral reference wording"
            )


def build_request_body(
    focus: str,
    sha: str,
    contract: str | int | None = None,
    note: str | None = None,
    is_private: bool | None = None,
) -> str:
    if not focus.strip():
        raise SystemExit("error: --focus must name at least one review lens")
    parts = [f"{REQUEST_MARKER} for {focus.strip()} at exact head `{sha}`."]
    if contract is not None:
        contract_ref = str(contract)
        if contract_ref.isdecimal():
            contract_ref = f"#{contract_ref}"
        scope = f"against the lane contract in {contract_ref} "
        lead = "Review"
    else:
        scope = ""
        lead = "Please review"
    parts.append(f"{lead} the full diff {scope}through those lenses.")
    if note:
        parts.append(note.strip())
    parts.append(threat_model_note(is_private))
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
        "--tier", choices=("A", "B", "C"), required=True,
        help="review tier; only Tier A may use the manual fallback",
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
    parser.add_argument("--dry-run", action="store_true", help="print, do not post")
    args = parser.parse_args(argv)

    if args.tier != "A":
        raise SystemExit(
            "error: the manual fallback is Tier A only; wait for the automatic "
            "first pass at every other tier"
        )
    if args.focus is None:
        raise SystemExit("error: --focus is required")
    if args.contract is None:
        raise SystemExit(
            "error: Tier A requires --contract naming the issue that carries "
            "the lane contract"
        )
    reject_caller_supplied_shas(
        {k: v for k, v in (("focus", args.focus), ("note", args.note)) if v}
    )

    owner, name = repo_coordinates(args.project)
    require_contract(args.contract, args.project, owner, name)
    sha = pr_head(args.pr, owner, name)
    local = local_head()
    if local != sha:
        raise SystemExit(
            f"error: local HEAD {local} != PR head {sha}; push the verified "
            "head first — the request must bind to the head that received the "
            "lane's local verification"
        )

    bodies, connector_seen = pr_review_history(args.pr, owner, name)
    if connector_seen:
        raise SystemExit(
            "error: this PR already has an automatic connector artifact; the one-pass budget is spent"
        )
    priors = prior_requests(bodies)
    if priors:
        raise SystemExit(
            "error: this PR already has a manual review request; the one-pass "
            "budget is spent"
        )
    body = build_request_body(
        args.focus,
        sha,
        args.contract,
        args.note,
        is_private=repo_is_private(owner, name),
    )

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
            "publication; the posted request is stale and the PR remains blocked"
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
