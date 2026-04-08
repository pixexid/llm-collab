# Migration: Amiga → llm-collab

This document is the migration contract for moving from the Amiga-embedded `.ai-collaboration` workspace to a standalone `llm-collab` workspace.

The migration has two goals:
1. Preserve all existing chat history and task state
2. Switch all new writes to the universal workspace format (schema v2)

---

## Schema differences

| Concept | Amiga (v1) | llm-collab (v2) |
|---------|-----------|-----------------|
| Inbox state | `State/inbox/read/{agent}.json` (list of read paths) | `agents/{id}/inbox.json` (unread + read arrays, relative paths) |
| Agent memory | `Memory/{Agent}.memory.md` | `agents/{id}/memory.md` |
| Identity | Verbal injection / CLAUDE.md snippet | `agents/{id}/identity.md` (generated, read at bootstrap) |
| PM2 app names | `amiga-collab-{agent}` | `{workspace_name}-{agent}` |
| Message tags default | `["amiga"]` | `[]` |
| Branch pattern | `codex/{agent}/{task_slug}` | `collab/{agent}/{task_slug}` (configurable) |
| Project scope | None (all messages are implicitly Amiga) | `project_id` field on messages and tasks |
| GitHub defaults | Hardcoded `pixexid/amiga` patterns | Per-project in `projects.json` |
| Preflight | Hardcoded `pnpm preflight --json` | Per-project `preflight_command` |

---

## Pre-migration checklist

- [ ] Freeze Amiga `.ai-collaboration` workspace (stop writing new chats/tasks there)
- [ ] Note all active task IDs and their current status
- [ ] Note all agent IDs in Amiga `agents.json`
- [ ] Back up `State/worktrees.json` (40+ entries may exist)
- [ ] Stop PM2 watchers: `pm2 stop all`

---

## Step 1: Initialize the new workspace

```bash
git clone https://github.com/your-org/llm-collab ~/Projects/_collab
cd ~/Projects/_collab
python scripts/init.py
```

When defining agents, mirror the Amiga roster:

| Amiga ID | New ID | Activation type | Notes |
|----------|--------|-----------------|-------|
| operator | operator | human | No change |
| amiga-operator-cmo | amiga-operator-cmo | cli_session | Internal triage bot |
| codex | codex | cli_session | Primary orchestrator |
| cdx2 | cdx2 | human_relay | base_model: codex |
| claude | claude | cli_session | UI/UX implementation |
| gemini | gemini | cli_session | Research |
| antigravity | antigravity | human_relay | base_model: claude/codex |

When defining projects, add at minimum:

```json
{
  "id": "amiga",
  "display_name": "Amiga House Cleaning",
  "repos": {
    "app": "../amiga_house_cleaning_company",
    "docs": "../amiga_house_cleaning_company_docs"
  },
  "default_branch_base": "main",
  "preflight_command": ["pnpm", "preflight", "--json"],
  "github": {
    "enabled": true,
    "repo": "pixexid/amiga",
    "project_number": null
  }
}
```

---

## Step 2: Migrate inbox state

The Amiga inbox state format (`State/inbox/read/{agent}.json`) stores **read** message paths as an array. The new format stores both `unread` and `read` as separate arrays in `agents/{id}/inbox.json`.

Run the migration script:

```bash
python scripts/migrate_from_amiga.py \
  --source ~/Projects/amiga_house_cleaning_company_docs/.ai-collaboration \
  --workspace ~/Projects/_collab
```

> **Note**: `migrate_from_amiga.py` is included below. It does NOT move Chats/ or Tasks/ — those stay in the Amiga workspace as historical archive. Only inbox state is migrated.

### Manual migration (if preferred)

For each agent, copy read paths from:
```
{amiga}/.ai-collaboration/State/inbox/read/{agent}.json
```
Into:
```
{new_workspace}/agents/{agent}/inbox.json
```
Format conversion:
```python
# Old format (array of paths)
["/abs/path/to/message.md", ...]

# New format
{
  "agent": "codex",
  "updated_utc": "...",
  "unread": [],
  "read": ["relative/path/to/message.md", ...]
}
```
Paths must be relative to the new workspace root.

---

## Step 3: Copy memory files

```bash
# From Amiga Memory/ to new agents/ dirs
cp ~/Projects/amiga_house_cleaning_company_docs/.ai-collaboration/Memory/Claude.memory.md \
   ~/Projects/_collab/agents/claude/memory.md

cp ~/Projects/amiga_house_cleaning_company_docs/.ai-collaboration/Memory/Codex.memory.md \
   ~/Projects/_collab/agents/codex/memory.md

# ... repeat for each agent
```

Edit each memory file to remove Amiga-specific absolute paths if desired.

---

## Step 4: Migrate worktree state (optional)

If you have active worktrees, copy `State/worktrees.json`:

```bash
cp ~/Projects/amiga_house_cleaning_company_docs/.ai-collaboration/State/worktrees.json \
   ~/Projects/_collab/State/worktrees.json
```

The schema is compatible — no conversion needed. Worktree paths are absolute, so they remain valid.

---

## Step 5: Update CLAUDE.md and agent memory snippets

Remove the Amiga-specific collab config from CLAUDE.md:

```markdown
# Remove this section from your global CLAUDE.md:
<Project_Collaboration>
When working on the Amiga project, the collaboration workspace lives at:
- `/Users/pixexid/Projects/amiga_house_cleaning_company_docs/.ai-collaboration`
...
</Project_Collaboration>
```

Replace with the universal snippet:

```bash
python bin/init_agent_memory.py --agent claude --target claude-code --write
python bin/init_agent_memory.py --agent codex --target codex
```

Update CLAUDE.md to point to the new workspace bootstrap:

```markdown
<Project_Collaboration>
Your collaboration workspace: /Users/pixexid/Projects/_collab

Bootstrap: python /Users/pixexid/Projects/_collab/bin/session_bootstrap.py --agent claude

If the user says "check your inbox":
  python /Users/pixexid/Projects/_collab/bin/inbox.py --me claude --limit 5
</Project_Collaboration>
```

---

## Step 6: Verify parity

```bash
# Verify new workspace is operational
python bin/session_bootstrap.py --agent codex
python bin/task_board.py
python bin/inbox.py --me codex

# Create a test message end-to-end
python bin/new_chat.py --title "Migration smoke test" --project amiga
echo "Test message" | python bin/deliver.py \
  --chat last --from codex --to claude --title "Migration test"
python bin/inbox.py --me claude
```

---

## Step 7: Switch PM2 watchers

```bash
# Stop old Amiga watchers
cd ~/Projects/amiga_house_cleaning_company_docs/.ai-collaboration
pm2 stop all

# Start new workspace watchers
cd ~/Projects/_collab
python bin/pm2_watchers.py start --all
python bin/pm2_watchers.py status --all
```

Old PM2 apps are named `amiga-collab-{agent}`. New apps are named `{workspace_name}-{agent}`. They can coexist during cutover.

```bash
# Remove old apps after verifying new ones work
pm2 delete amiga-collab-codex amiga-collab-claude amiga-collab-gemini ...
```

---

## Step 8: Archive the Amiga workspace

The Amiga `.ai-collaboration` directory should be kept as a historical archive but no longer written to:

```bash
# Mark as archive in README
echo "# ARCHIVED — migrated to ~/Projects/_collab on $(date +%Y-%m-%d)" > \
  ~/Projects/amiga_house_cleaning_company_docs/.ai-collaboration/ARCHIVED.md
```

---

## Migration script

Save as `scripts/migrate_from_amiga.py`:

```python
#!/usr/bin/env python3
"""Migrate inbox state from Amiga workspace to llm-collab format."""

import argparse
import json
from pathlib import Path
from datetime import datetime, timezone


def utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, help="Path to Amiga .ai-collaboration dir")
    p.add_argument("--workspace", required=True, help="Path to new llm-collab workspace")
    args = p.parse_args()

    source = Path(args.source)
    workspace = Path(args.workspace)

    read_dir = source / "State" / "inbox" / "read"
    if not read_dir.exists():
        print("[skip] No Amiga inbox state found.")
        return

    for state_file in sorted(read_dir.glob("*.json")):
        agent_id = state_file.stem
        amiga_state = json.loads(state_file.read_text())

        # Amiga format: array of absolute paths
        read_paths = amiga_state if isinstance(amiga_state, list) else amiga_state.get("messages", [])

        # Convert to relative paths (best effort)
        relative_paths = []
        for p in read_paths:
            try:
                rel = str(Path(p).relative_to(source))
                relative_paths.append(rel)
            except ValueError:
                relative_paths.append(p)  # keep absolute if can't relativize

        out_path = workspace / "agents" / agent_id / "inbox.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        new_state = {
            "agent": agent_id,
            "updated_utc": utc_iso(),
            "unread": [],
            "read": relative_paths,
        }

        out_path.write_text(json.dumps(new_state, indent=2))
        print(f"  [migrated] {agent_id}: {len(relative_paths)} read messages → {out_path.relative_to(workspace)}")

    print("\nDone. Verify with: python bin/inbox.py --me <agent_id> --all")


if __name__ == "__main__":
    main()
```

---

## Rollback

To roll back to the Amiga workspace:

1. Stop new workspace watchers: `python bin/pm2_watchers.py stop --all`
2. Restart old watchers: `cd {amiga}/.ai-collaboration && pm2 start pm2/ecosystem.config.cjs`
3. Restore CLAUDE.md to original Amiga bootstrap config

No data is deleted during migration. The Amiga workspace remains intact as the archive.
