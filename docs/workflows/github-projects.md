# GitHub Projects Workflow

## Goal

Use GitHub Projects for planning visibility while local tasks remain execution truth.

## Source-of-truth split

Use GitHub Projects for:

- roadmap visibility
- prioritization
- phase/epic tracking

Use local tasks for:

- owner assignment
- execution scope
- verification notes
- handoff/blocker state

## Drift handling

If GitHub project state and local task state disagree, resolve it in the same session.

## Mirror + sync tools

```bash
bin/llm-collab check_github_task_mirrors.py --project <project_id>
bin/llm-collab check_github_task_mirrors.py --project <project_id> --archive-closed-active
bin/llm-collab report_github_project_task_sync.py --project <project_id>
```

All mirror lookups use exact project matching. The sync report is written to
`{project_state_root}/{project_id}/github-project-task-sync.md` unless
`--output` overrides it.

## Alignment semantics

- `ok`: no action needed
- `mismatch`: execution drift, fix immediately
- `review`: possible valid state but needs human judgement
