# Example Project Overrides

Copy this directory to `projects/<project_id>/` in your local workspace when a project needs local routing, queue, runbook, or memory-template overrides.

Real project directories are runtime-local and gitignored. Do not commit customer, company, repository, queue, task, worker, or operational state from a real project into this open-source repo.

## Contents

- `roles-and-routing.md`: optional project-specific routing and ownership policy
- `issue-queue.example.json`: example ordered queue shape
- `issue-queue.example.md`: generated human-readable queue example
- `runbooks/`: local project runbooks
- `memory-templates/`: local agent memory snippets

To create a real queue for a project:

```bash
mkdir -p projects/my-app
cp projects/_example/issue-queue.example.json projects/my-app/issue-queue.json
python3 bin/project_issue_queue.py sync-markdown --project my-app
```
