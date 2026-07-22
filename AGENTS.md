# AGENTS.md

This repository is the shared `llm-collab` coordination runtime. It is not the
Amiga workspace, the Nuvyr workspace, or any other product repository.

## Required Reading

Before changing shared tooling or operating a project lane, read:

- `README.md`
- `docs/multi-project.md`
- `docs/workflows/session-startup.md`
- `docs/workflows/task-intake-and-delegation.md`

Then read the target project's own repository instructions and local policy
under `{project_state_root}/{project_id}/`.

## Project Boundary

Project-scoped is the default. Universal behavior is allowed only when it is
project-independent by construction.

- Every chat, message, task, queue, runtime binding, report, and project-aware
  command must use one registered `project_id`.
- A project-aware reader or mutator must require an exact project match. Do not
  treat missing, empty, or `null` project IDs as belonging to the requested
  project. Legacy backfills belong only in explicit migration tooling.
- Project-specific repositories, commands, design sources, database refs, tool
  surfaces, GitHub settings, and runbooks come from that project's
  `projects.json` entry, project-local state, or explicit task fields.
- Do not hardcode one project's values in shared `bin/`, `scripts/`, templates,
  generated guidance, or universal workflow docs.
- `agents.json` is universal only for collaborator identity and activation
  capabilities. Keep product paths, design contracts, database settings, queue
  state, and routing policy out of it.
- Keep generated and runtime outputs under
  `{project_state_root}/{project_id}/`; one project must not overwrite another
  project's report or queue.
- Amiga compatibility is project-specific. An Amiga fallback must be guarded by
  an exact `project_id == "amiga"` check and must never become a workspace
  default.

When changing a shared contract, add focused coverage for Amiga and at least
one non-Amiga project, then run the full test suite:

```bash
python3.11 -m unittest discover -s tests
```

## Adding A Project

For an existing workspace, update `projects.json` directly. Do not rerun
`scripts/init.py` unless the intent is to reinitialize the whole workspace.

1. Register a unique `id`, display name, repositories, base branch, preflight,
   and GitHub configuration. Add project-specific `ui_ux`, `db`, and
   `claude_desktop_bridge` configuration only when applicable.
2. Create local state at `{project_state_root}/{project_id}/`; keep real project
   state outside this public Git checkout.
3. Add repository-level `AGENTS.md` and worker guidance to the product repo.
   Bind examples and commands to the exact checkout and `--project <id>`.
4. For a GitHub-backed project, materialize and validate the project queue:

   ```bash
   bin/llm-collab project_issue_queue.py reconcile --project <id> --write
   bin/llm-collab project_issue_queue.py validate --project <id>
   ```

   Projects without GitHub integration can use the local task board without a
   GitHub-backed issue queue.

5. Create a representative project-scoped chat and task, sync its contract,
   and validate it before activating a worker:

   ```bash
   bin/llm-collab task_contract.py sync --task TASK-... --write
   bin/llm-collab task_contract.py validate --task TASK-... --stage assignment
   ```

6. Confirm that the task, queue, generated guidance, and runtime state contain
   no paths, database refs, tool surfaces, or policies from another project.

## Shared Checkout Safety

This checkout may contain another lane's local work. Inspect `git status` before
switching branches, pulling, staging, or cleaning. Preserve unrelated tracked
changes and untracked files unless their owner explicitly authorizes removal.

## GitHub Autolink Safety

This repository has a GitHub autolink for the `GH-` issue prefix. Treat
`GH-123` as a real issue reference here, not as inert project shorthand.

Do not put any GitHub closing keyword immediately before a `GH-<number>`
reference in PR bodies, merge commit bodies, ordinary commit messages, or issue
comments, even inside negated prose. Use neutral wording such as `Related
GH-123`, `Related #123`, or a full issue URL when the referenced issue should
stay open.

Two incidents established this as repo-local policy:

- PR #153 placed negated non-resolution wording adjacent to issue #135 and
  GitHub changed issue #135 to closed when the PR merged.
- PR #198 repeated the same class through the `GH-` autolink; the merge commit
  body put a closing keyword adjacent to the autolinked reference for issue 91,
  and GitHub changed GH-91 to closed.
