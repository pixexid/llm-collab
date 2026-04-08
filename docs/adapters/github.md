# GitHub Integration (Optional Adapter)

The GitHub adapter syncs GitHub Issues and GitHub Projects with local task files. It is entirely optional — the core workspace functions without it.

---

## Enable per project

In `projects.json`:

```json
{
  "id": "my-app",
  "github": {
    "enabled": true,
    "repo": "owner/my-app",
    "project_number": 1
  }
}
```

## Requirements

- `gh` CLI installed and authenticated: `gh auth login`

---

## What it does

### Issue mirroring

Creates local task files for GitHub issues. A local task named `gh-42-fix-auth__TASK-abc.md` corresponds to GitHub issue #42.

### Project board sync

Reads a GitHub Projects board and reflects item statuses in local tasks.

---

## Usage

```bash
# Mirror open issues to local tasks
python bin/check_github_task_mirrors.py --project my-app

# Report sync state between local tasks and GitHub project board
python bin/report_github_project_task_sync.py --project my-app
```

By default, both scripts include legacy unscoped tasks (`project_id` missing) for migration compatibility.
Use `--strict-project` to require exact `project_id` match only.

---

## Implementation note

The GitHub adapter scripts (`check_github_task_mirrors.py`, `report_github_project_task_sync.py`) read project config from `projects.json` — they do not hardcode any repo paths or issue number formats. This is the key difference from the Amiga implementation: all GitHub defaults are project-scoped, not workspace-global.

---

## Without GitHub

If `github.enabled` is `false`, tasks are managed entirely through the local file system. GitHub is not required, assumed, or implied anywhere in the core system.
