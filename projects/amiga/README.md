# Amiga Project Overrides

This directory contains Amiga-specific collaboration behavior layered on top of the universal `docs/workflows/` baseline.

Use this as the reference example for project-specific customization under `projects/{project_id}/`.

## Contents

- `issue-queue.json`: canonical ordered remaining issue queue for Amiga
- `issue-queue.md`: generated human-readable queue view
- `design-queue.json`: canonical ordered `/design` sandbox/spec queue before `/app` implementation
- `design-queue.md`: human-readable `/design` queue view
- `history/`: archived snapshots written automatically when the final queued lane completes
- `roles-and-routing.md`: Amiga-specific identities, orchestration, and routing constraints
- `runbooks/`: Amiga-specific operational runbooks
- `memory-templates/`: seed memory templates for Amiga collaborators
- `project.example.json`: example `projects.json` entry for Amiga
