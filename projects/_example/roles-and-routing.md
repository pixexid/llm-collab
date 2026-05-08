# Example Roles And Routing

This file is a template for project-local collaboration policy.

## Registered Collaborators

- `operator`
- `orchestrator`
- `worker`
- `researcher`

## Orchestration Defaults

- `orchestrator` owns sequencing, task shaping, verification, and merge decisions.
- `worker` owns assigned implementation slices in isolated worktrees.
- `researcher` owns bounded research and comparative analysis.
- Real project-specific identities, repository paths, customer names, queue state, and operational details belong only in your local `projects/<project_id>/` directory.

## Queue Rule

If the project has an ordered queue, keep it in local `projects/<project_id>/issue-queue.json` and regenerate `issue-queue.md` with:

```bash
python3 bin/project_issue_queue.py sync-markdown --project <project_id>
```
