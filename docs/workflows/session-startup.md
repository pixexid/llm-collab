# Session Startup

## Goal

Start from a known-good environment before claiming or editing work.

## Bootstrap first

```bash
cd <workspace_root>
python bin/session_bootstrap.py --agent <agent_id>
```

## Read before acting

1. collaboration inbox
2. active task board
3. project-level instructions (`projects/<project_id>/...` when present)
4. repo-specific contributing/agent guidance

## Required preflight

Do not claim tasks or edit code until the active checkout is healthy.

Typical preflight checks:

- dependencies installed
- environment files present/readable
- project build/test command surface usable
- GitHub access usable (if this lane needs GitHub)
- browser/runtime validation path usable (if this lane needs it)

If any item fails: stop, fix environment, re-run checks.

