# Example Project Overrides

Copy this directory to `{project_state_root}/<project_id>/` when a project needs local routing, queue, runbook, or memory-template overrides.

Real project directories are runtime-local. Prefer a `project_state_root` outside this Git checkout, such as `~/.local/share/llm-collab/projects`. Do not commit customer, company, repository, queue, task, worker, or operational state from a real project into this open-source repo.

## Contents

- `roles-and-routing.md`: optional project-specific routing and ownership policy
- `issue-queue.example.json`: example ordered queue shape
- `issue-queue.example.md`: generated human-readable queue example
- `runbooks/`: local project runbooks
- `memory-templates/`: local agent memory snippets

For a GitHub-backed project, create the real queue from its registered backlog:

```bash
mkdir -p ~/.local/share/llm-collab/projects/my-app
bin/llm-collab project_issue_queue.py reconcile --project my-app --write
bin/llm-collab project_issue_queue.py validate --project my-app
```

Projects without GitHub integration can use local tasks without an issue queue.
`issue-queue.example.json` documents the schema only; do not copy its placeholder
lanes into a real project queue.
