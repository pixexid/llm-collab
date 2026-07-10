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
    "project_number": 1,
    "backlog": {
      "exclude_labels": ["type:epic", "wontfix", "duplicate", "invalid", "question", "status:deferred"],
      "require_any_label": []
    }
  }
}
```

## Requirements

- `gh` CLI installed and authenticated: `gh auth login`

---

## Backlog eligibility

For GitHub-backed projects, open GitHub issues are the source of truth for whether work remains. The backlog resolver includes every open issue except labels listed in `github.backlog.exclude_labels`. By default, epics, terminal issue labels, and `status:deferred` are excluded.

`github.backlog.require_any_label` can narrow the backlog with exact labels or wildcard patterns such as `area:*`. Keep it empty unless label hygiene is strong enough that unlabeled issues should not become executable work.

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
bin/llm-collab check_github_task_mirrors.py --project my-app

# Report sync state between local tasks and GitHub project board
bin/llm-collab report_github_project_task_sync.py --project my-app
```

Both scripts require exact `project_id` matches. Projectless and foreign task
mirrors are always excluded. The report defaults to
`{project_state_root}/{project_id}/github-project-task-sync.md`.

The deprecated `--strict-project` option remains accepted as a no-op for older
automation. Use `scripts/migrate_from_amiga.py` for intentional legacy
backfills instead of weakening normal adapter scope.

---

## Implementation note

The GitHub adapter scripts (`check_github_task_mirrors.py`, `report_github_project_task_sync.py`) read project config from `projects.json` — they do not hardcode any repo paths or issue number formats. This is the key difference from the Amiga implementation: all GitHub defaults are project-scoped, not workspace-global.

---

## Without GitHub

If `github.enabled` is `false`, tasks are managed entirely through the local file system. GitHub is not required, assumed, or implied anywhere in the core system.
