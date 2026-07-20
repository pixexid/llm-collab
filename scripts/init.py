#!/usr/bin/env python3
"""
scripts/init.py — Interactive workspace initialization.

Creates collab.config.json, agents.json, projects.json, and
generates agents/{id}/identity.md + agents/{id}/memory.md for each agent.

Run from the workspace root:
  python scripts/init.py
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def prompt(
    question: str,
    default: str = "",
    input_fn: Callable[[str], str] | None = None,
) -> str:
    hint = f" [{default}]" if default else ""
    reader = input if input_fn is None else input_fn
    val = reader(f"{question}{hint}: ").strip()
    return val if val else default


def prompt_list(
    question: str,
    input_fn: Callable[[str], str] | None = None,
) -> list[str]:
    print(f"{question} (comma-separated, e.g. app,api or leave blank):")
    reader = input if input_fn is None else input_fn
    val = reader("> ").strip()
    return [v.strip() for v in val.split(",") if v.strip()]


def yn(
    question: str,
    default: bool = True,
    input_fn: Callable[[str], str] | None = None,
) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    reader = input if input_fn is None else input_fn
    val = reader(f"{question} {hint}: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def divider():
    print("\n" + "─" * 50)


def write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    print(f"  ✓ {path.relative_to(ROOT)}")


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  ✓ {path.relative_to(ROOT)}")


def build_identity_md(agent: dict, workspace_name: str, all_agent_ids: list[str], projects: list[dict]) -> str:
    aid = agent["id"]
    display = agent.get("display_name", aid)
    role = agent.get("role", "agent")
    activation = agent.get("activation", {})
    identity_note = activation.get("identity_note", "")
    base_model = activation.get("base_model", "")
    other_agents = [a for a in all_agent_ids if a != aid]
    project_list = ", ".join(p["id"] for p in projects) if projects else "(none)"

    lines = [
        f"# Identity: {display}",
        "",
    ]

    if identity_note:
        lines += [
            f"> **IMPORTANT**: {identity_note}",
            "",
        ]

    lines += [
        f"## Role",
        f"",
        f"**Agent ID**: `{aid}`  ",
        f"**Role**: {role}  ",
        f"**Workspace**: {workspace_name}  ",
        "",
        "## Workspace",
        "",
        f"- Root: `{ROOT}`",
        f"- Your inbox: `{ROOT}/agents/{aid}/inbox.json`",
        f"- Your memory: `{ROOT}/agents/{aid}/memory.md`",
        "",
        "## Session Bootstrap",
        "",
        f"At the start of every session, run:",
        f"```",
        f"{ROOT}/bin/llm-collab session_bootstrap.py --agent {aid}",
        f"```",
        "",
        "## Key Commands",
        "",
        f"```bash",
        f"# Check inbox",
        f"{ROOT}/bin/llm-collab inbox.py --me {aid}",
        "",
        f"# Send message",
        f'{ROOT}/bin/llm-collab deliver.py --chat last --from {aid} --to <agent> --project <project_id> --title "..."',
        "",
        f"# Create task",
        f'{ROOT}/bin/llm-collab new_task.py --title "..." --created-by {aid} --project <project_id>',
        "",
        f"# Task board",
        f"{ROOT}/bin/llm-collab task_board.py",
        f"```",
        "",
        f"## Active Projects",
        "",
        f"{project_list}",
        "",
        "## Project Boundary",
        "",
        "- Project-scoped is the default; universal behavior is the exception.",
        "- Use one registered `project_id` for every chat, message, task, queue, and report.",
        "- Never reuse another project's paths, design docs, database refs, tools, or policy.",
        "- Read the target repository instructions and the workspace `AGENTS.md` before acting.",
        "",
        f"## Other Agents",
        "",
    ]

    for oid in other_agents:
        lines.append(f"- `{oid}`")

    lines += [
        "",
        "## Instructions",
        "",
        "- Always bootstrap your session before starting work.",
        "- Always check your inbox before starting new work.",
        "- When implementation is ready for independent review, update its status: `claim_task.py --status review`",
        "- When you send a message to a human_relay agent, the system will print",
        "  a handoff prompt for the operator to relay to them.",
    ]

    return "\n".join(lines) + "\n"


def build_memory_md(agent: dict) -> str:
    aid = agent["id"]
    display = agent.get("display_name", aid)
    return f"# Memory: {display}\n\n_This file is maintained by `{aid}`. Add persistent notes here._\n\n## Notes\n\n- \n"


def collect_agents() -> list[dict]:
    print("\n[Agents]\n")
    print("Define the agents (LLM instances) that will collaborate.")
    print("You need at least: one human operator + one LLM agent.")
    print()

    agents = []

    while True:
        divider()
        print(f"Agent #{len(agents) + 1}")
        aid = prompt("  Agent ID (lowercase, e.g. orchestrator, claude, codex)").lower().replace(" ", "-")
        if not aid:
            if len(agents) == 0:
                print("[error] You must define at least one agent.")
                continue
            break

        display = prompt(f"  Display name", default=aid.capitalize())
        role = prompt("  Role (e.g. primary_orchestrator, implementation, research, human_dispatcher)", default="agent")

        print(f"  Activation type:")
        print(f"    1) cli_session    — LLM CLI always open, background watcher runs")
        print(f"    2) human_relay    — Human must paste a prompt to a new LLM session")
        print(f"    3) human          — Human operator (no LLM)")
        print(f"    4) api_trigger    — External webhook triggers the agent")
        atype_choice = prompt("  Choice", default="1")
        atype_map = {"1": "cli_session", "2": "human_relay", "3": "human", "4": "api_trigger"}
        atype = atype_map.get(atype_choice, "cli_session")

        activation: dict = {"type": atype}

        if atype == "cli_session":
            activation["watcher_enabled"] = yn("  Enable background inbox watcher?", default=True)
            ax_app = prompt(
                "  AX app name or bundle id for direct doorbell (blank for terminal-only)",
                default="",
            )
            if ax_app:
                activation["ax_app"] = ax_app
        elif atype == "human_relay":
            activation["watcher_enabled"] = False
            base_model = prompt("  Base model/CLI (e.g. codex, claude, gemini)", default="")
            if base_model:
                activation["base_model"] = base_model
            identity_note = prompt(
                f"  Identity note shown in handoff prompt (identity-only form)",
                default=f"You are {display} ({aid}). Read only messages addressed to '{aid}'.",
            )
            activation["identity_note"] = identity_note
        elif atype == "human":
            activation["watcher_enabled"] = False

        notes = prompt("  Notes (optional)", default="")

        entry: dict = {
            "id": aid,
            "display_name": display,
            "role": role,
            "activation": activation,
        }
        if notes:
            entry["notes"] = notes

        agents.append(entry)
        print(f"  [added] {aid}")

        if not yn("\nAdd another agent?", default=True):
            break

    return agents


def enabled_agent_ids(agents: list[dict]) -> list[str]:
    """Known agent IDs eligible to hold a generated project release gate."""
    result = []
    for agent in agents:
        agent_id = agent.get("id")
        activation = agent.get("activation")
        role = str(agent.get("role", ""))
        if not isinstance(agent_id, str) or not agent_id:
            continue
        if agent.get("disabled") is True or role.startswith("legacy_disabled"):
            continue
        if isinstance(activation, dict) and activation.get("enabled") is False:
            continue
        result.append(agent_id)
    return result


RELEASE_CLOSURE_REQUIRED_KEYS = (
    "github.repo",
    "default_branch_base",
    "release_gate_agent",
    "release_closure.workflow",
    "release_closure.trigger_event",
    "release_closure.required_jobs",
    "release_closure.smoke_job",
    "release_closure.required_smoke_steps",
)


def _projects_file_path(projects_path: Path | None = None) -> Path:
    return (ROOT / "projects.json" if projects_path is None else projects_path).resolve()


def _print_project_key_error(project_id: str, key: str, projects_path: Path, detail: str) -> None:
    print(
        f"[error] project {project_id!r} has invalid projects.json key {key!r} "
        f"at {projects_path}: {detail}"
    )


def _require_project_value(
    project_id: str,
    key: str,
    question: str,
    projects_path: Path,
    *,
    input_fn: Callable[[str], str] | None = None,
    comma_list: bool = False,
) -> str | list[str]:
    while True:
        raw = prompt(
            question + (" (comma-separated; every item is required)" if comma_list else ""),
            input_fn=input_fn,
        )
        if not comma_list and raw:
            return raw
        if comma_list:
            items = [item.strip() for item in raw.split(",")]
            if raw and all(items):
                return items
        _print_project_key_error(
            project_id,
            key,
            projects_path,
            (
                "a non-empty list of non-empty strings is required"
                if comma_list
                else "a non-empty explicit value is required; no default or inherited value is allowed"
            ),
        )


def select_release_mode(
    project_id: str,
    projects_path: Path,
    *,
    input_fn: Callable[[str], str] | None = None,
) -> str:
    """Require a production decision without inferring or defaulting it."""
    while True:
        selected = prompt(
            "  Release environment (production or non-production/local; required)",
            input_fn=input_fn,
        ).lower()
        if selected in {"production", "non-production/local"}:
            return selected
        _print_project_key_error(
            project_id,
            "release_closure",
            projects_path,
            "enter exactly 'production' or 'non-production/local'; no default or inference is allowed",
        )


def collect_release_closure(
    project_id: str,
    projects_path: Path,
    *,
    input_fn: Callable[[str], str] | None = None,
) -> dict:
    """Collect one complete project-specific closure before it can be appended."""
    closure = {}
    for field, question, comma_list in (
        ("workflow", "  Production release workflow name or filename", False),
        ("trigger_event", "  Automatic production trigger event", False),
        ("required_jobs", "  Required release job names", True),
    ):
        closure[field] = _require_project_value(
            project_id,
            f"release_closure.{field}",
            question,
            projects_path,
            input_fn=input_fn,
            comma_list=comma_list,
        )
    while True:
        smoke_job = _require_project_value(
            project_id,
            "release_closure.smoke_job",
            "  Required job that carries post-deploy smoke steps",
            projects_path,
            input_fn=input_fn,
        )
        if smoke_job in closure["required_jobs"]:
            break
        _print_project_key_error(
            project_id,
            "release_closure.smoke_job",
            projects_path,
            f"{smoke_job!r} must appear in release_closure.required_jobs",
        )
    closure["smoke_job"] = smoke_job
    closure["required_smoke_steps"] = _require_project_value(
        project_id,
        "release_closure.required_smoke_steps",
        "  Required post-deploy smoke step names",
        projects_path,
        input_fn=input_fn,
        comma_list=True,
    )
    return closure


def print_release_closure_repair_guidance(
    project_id: str,
    projects_path: Path,
    *,
    github_enabled: bool,
    ambiguous_reinitialize: bool = False,
) -> None:
    if ambiguous_reinitialize:
        print(
            f"  [release closure] project {project_id!r} is an existing/ambiguous "
            "reinitialize entry; `release_closure` is not collected or written, and "
            "exact-SHA `success` stays fail-closed until its configuration is repaired."
        )
    elif github_enabled:
        print(
            f"  [release closure] project {project_id!r} is non-production/local; "
            "exact-SHA `success` stays fail-closed until its configuration is repaired."
        )
    else:
        print(
            f"  [release closure] project {project_id!r} has GitHub disabled, so "
            "`release_closure` is omitted and exact-SHA `success` is unavailable."
        )
    print(f"  Repair the local registry at {projects_path} with every required key:")
    for key in RELEASE_CLOSURE_REQUIRED_KEYS:
        print(f"    - {key}")


def select_release_gate_agent(
    project_id: str,
    known_enabled_agent_ids: list[str],
    *,
    projects_path: Path | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> str:
    """Require an explicit, exact selection; never choose a universal default."""
    local_projects_path = _projects_file_path(projects_path)
    if not known_enabled_agent_ids:
        raise ValueError(
            f"project {project_id!r} needs projects.json key 'release_gate_agent' "
            f"at {local_projects_path}, but no enabled agents were collected"
        )
    print("  Enabled release-gate agents: " + ", ".join(known_enabled_agent_ids))
    while True:
        selected = prompt(
            "  Release gate agent ID (required; exact enabled agent ID)",
            input_fn=input_fn,
        )
        if not selected:
            _print_project_key_error(
                project_id,
                "release_gate_agent",
                local_projects_path,
                "release_gate_agent is required; select an enabled agent ID",
            )
            continue
        if selected not in known_enabled_agent_ids:
            _print_project_key_error(
                project_id,
                "release_gate_agent",
                local_projects_path,
                f"Unknown or disabled release_gate_agent {selected!r}; "
                f"choose one of: {', '.join(known_enabled_agent_ids)}",
            )
            continue
        return selected


def collect_projects(
    known_enabled_agent_ids: list[str],
    *,
    input_fn: Callable[[str], str] | None = None,
    projects_path: Path | None = None,
    allow_new_release_closure: bool = True,
    project_sink: list[dict] | None = None,
) -> list[dict]:
    print("\n[Projects]\n")
    print("Register the code projects this workspace will coordinate work on.")
    print("Paths can be relative to projects_root or absolute.")
    print()

    projects_root = config_get_local("projects_root", "")
    projects = [] if project_sink is None else project_sink
    local_projects_path = _projects_file_path(projects_path)

    if not yn("Add projects now?", default=True, input_fn=input_fn):
        return projects

    while True:
        divider()
        print(f"Project #{len(projects) + 1}")
        pid = prompt(
            "  Project ID (lowercase, e.g. my-app)",
            input_fn=input_fn,
        ).lower().replace(" ", "-")
        if not pid:
            break

        display = prompt("  Display name", pid.replace("-", " ").title(), input_fn=input_fn)
        repos_raw = prompt_list(
            "  Repo IDs and paths (e.g. app:my-app  or  app)",
            input_fn=input_fn,
        )

        repos = {}
        for r in repos_raw:
            if ":" in r:
                rid, _, rpath = r.partition(":")
                repos[rid.strip()] = rpath.strip()
            else:
                repos[r] = r

        if not repos:
            repos = {pid: pid}

        preflight = None
        if yn("  Does this project have a preflight/build check command?", False, input_fn=input_fn):
            cmd_str = prompt("  Command (e.g. pnpm preflight --json)", input_fn=input_fn)
            if cmd_str:
                preflight = cmd_str.split()

        github_enabled = yn("  Enable GitHub integration for this project?", False, input_fn=input_fn)
        github: dict = {"enabled": github_enabled}
        release_mode = None
        if github_enabled:
            if allow_new_release_closure:
                release_mode = select_release_mode(
                    pid,
                    local_projects_path,
                    input_fn=input_fn,
                )
            if release_mode == "production":
                github["repo"] = _require_project_value(
                    pid,
                    "github.repo",
                    "  GitHub repo (owner/repo)",
                    local_projects_path,
                    input_fn=input_fn,
                )
            else:
                github["repo"] = prompt(
                    "  GitHub repo (owner/repo; add now or repair later)",
                    input_fn=input_fn,
                )
            pn = prompt("  GitHub Project number (optional)", input_fn=input_fn)
            if pn.isdigit():
                github["project_number"] = int(pn)
            github["backlog"] = {
                "exclude_labels": [
                    "type:epic",
                    "wontfix",
                    "duplicate",
                    "invalid",
                    "question",
                    "status:deferred",
                ],
                "require_any_label": [],
            }

        if release_mode == "production":
            default_branch_base = _require_project_value(
                pid,
                "default_branch_base",
                "  Default branch base",
                local_projects_path,
                input_fn=input_fn,
            )
        else:
            default_branch_base = prompt("  Default branch base", "main", input_fn=input_fn)
        release_gate_agent = select_release_gate_agent(
            pid,
            known_enabled_agent_ids,
            projects_path=local_projects_path,
            input_fn=input_fn,
        )
        entry = {
            "id": pid,
            "display_name": display,
            "repos": repos,
            "default_branch_base": default_branch_base,
            "preflight_command": preflight,
            "github": github,
            "release_gate_agent": release_gate_agent,
        }
        if release_mode == "production":
            entry["release_closure"] = collect_release_closure(
                pid,
                local_projects_path,
                input_fn=input_fn,
            )
        else:
            print_release_closure_repair_guidance(
                pid,
                local_projects_path,
                github_enabled=github_enabled,
                ambiguous_reinitialize=not allow_new_release_closure,
            )
        projects.append(entry)
        print(f"  [added] {pid}")

        if not yn("\nAdd another project?", True, input_fn=input_fn):
            break

    return projects


_local_config: dict = {}


def config_get_local(key: str, default=""):
    return _local_config.get(key, default)


def main(*, input_fn: Callable[[str], str] | None = None):
    global _local_config

    print("=" * 60)
    print("  llm-collab workspace init")
    print("=" * 60)
    print(f"\nWorkspace root: {ROOT}\n")

    config_file = ROOT / "collab.config.json"
    projects_file = ROOT / "projects.json"
    allow_new_release_closure = not (config_file.exists() or projects_file.exists())
    reinitialize = config_file.exists()
    if reinitialize:
        if not yn(
            "collab.config.json already exists. Reinitialize?",
            default=False,
            input_fn=input_fn,
        ):
            print("Aborted.")
            sys.exit(0)

    print("\n[Workspace Config]\n")
    workspace_name = prompt(
        "Workspace name (used for PM2 app names, slugs)",
        default=ROOT.name.lower().replace("_", "-"),
        input_fn=input_fn,
    )
    projects_root = prompt(
        "Projects root path (directory containing your project repos)",
        default=str(Path.home() / "Projects"),
        input_fn=input_fn,
    )
    project_state_root = prompt(
        "Project state root path (local queues/runbooks/memory; outside this git repo)",
        default=str(Path.home() / ".local" / "share" / "llm-collab" / "projects"),
        input_fn=input_fn,
    )
    poll_interval = prompt("Inbox poll interval in seconds", default="15", input_fn=input_fn)
    notifications = yn("Enable desktop notifications?", default=True, input_fn=input_fn)

    _local_config = {
        "workspace_name": workspace_name,
        "projects_root": projects_root,
        "project_state_root": project_state_root,
        "poll_interval_seconds": int(poll_interval),
        "notifications_enabled": notifications,
    }

    config = {
        "workspace_name": workspace_name,
        "schema_version": 2,
        "projects_root": projects_root,
        "project_state_root": project_state_root,
        "default_tags": [],
        "branch_pattern": "collab/{agent}/{task_slug}",
        "poll_interval_seconds": int(poll_interval),
        "notifications_enabled": notifications,
        "notifications_platform": "auto",
    }

    agents = collect_agents()
    all_agent_ids = [a["id"] for a in agents]
    all_enabled_agent_ids = enabled_agent_ids(agents)

    projects = collect_projects(
        all_enabled_agent_ids,
        input_fn=input_fn,
        allow_new_release_closure=allow_new_release_closure,
    )

    divider()
    print("\n[Writing files]\n")

    write_json(config_file, config)
    write_json(ROOT / "agents.json", {"agents": agents})
    if projects:
        write_json(projects_file, {"projects": projects})

    for agent in agents:
        aid = agent["id"]
        atype = agent.get("activation", {}).get("type", "")
        if atype == "human":
            continue
        identity = build_identity_md(agent, workspace_name, all_agent_ids, projects)
        memory = build_memory_md(agent)
        write_file(ROOT / "agents" / aid / "identity.md", identity)
        write_file(ROOT / "agents" / aid / "memory.md", memory)
        # Create empty inbox
        inbox_path = ROOT / "agents" / aid / "inbox.json"
        if not inbox_path.exists():
            write_json(inbox_path, {"agent": aid, "unread": [], "read": []})

    divider()
    print("\n[Next steps]\n")
    print("1. Bootstrap each agent session:")
    for a in agents:
        if a.get("activation", {}).get("type") not in ("human",):
            print(f"   bin/llm-collab session_bootstrap.py --agent {a['id']}")
    print()
    print("2. Generate memory snippets for your LLM tools:")
    for a in agents:
        if a.get("activation", {}).get("type") not in ("human",):
            print(f"   bin/llm-collab init_agent_memory.py --agent {a['id']} --target generic")
    print()
    print("3. Start PM2 watchers (optional, requires pm2):")
    print("   bin/llm-collab pm2_watchers.py start --all")
    print()
    print("4. Create your first chat:")
    print('   bin/llm-collab new_chat.py --title "..." --project <project_id>')
    print()
    print("5. Complete each project's optional UI/UX, DB, and bridge contract:")
    print("   See docs/multi-project.md and AGENTS.md before activating workers.")
    print()
    print("Done. See docs/getting-started.md for the full workflow.\n")


if __name__ == "__main__":
    main()
