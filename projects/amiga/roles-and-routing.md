# Amiga Roles And Routing

Project-specific collaborator identities and routing rules for Amiga.

## Registered collaborators

- `operator`
- `amiga-operator-cmo`
- `codex`
- `cdx2`
- `claude`
- `gemini`
- `antigravity`

## Orchestration defaults

- `codex` is the default orchestrator
- non-trivial implementation lanes are usually delegated to a worker (`cdx2` by default)
- UI-heavy fallback lanes may go to `claude` or `antigravity`
- research lanes may go to `gemini`
- the canonical ordered remaining lane queue lives in `projects/amiga/issue-queue.json`
- fresh sessions should read `projects/amiga/issue-queue.md` before choosing or activating the next issue-sized lane

## Identity hard rules

- `codex` and `cdx2` are distinct identities
- human-originated messages use `operator`
- do not impersonate `amiga-operator-cmo`

## Amiga docs-sync rule

When app behavior changes in the Amiga app repo, update sibling docs in the same session before considering the lane shippable.
