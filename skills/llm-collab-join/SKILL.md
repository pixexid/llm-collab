---
name: llm-collab-join
description: Join an llm-collab workspace safely. Use when starting work in an llm-collab checkout, onboarding a model account, or refreshing collaboration pointers for a project lane.
---

# llm-collab join

## Purpose
Use the existing llm-collab contract and bootstrap seam without copying collab command families into memory files.

## Setup
- Run from an `llm-collab` workspace checkout.
- Know your agent id.

## Workflow
1. Read `AGENTS.md`.
2. Read `docs/workflows/session-startup.md`, `docs/workflows/collab-thread-quickstart.md`, and `docs/workflows/task-intake-and-delegation.md`.
3. Run `bin/llm-collab current_runtime.py --agent <agent_id>`.
4. Follow the inbox packet and the target project's own repository instructions.
5. If bootstrap reports a newer contract version or drift warning, refresh your memory pointers instead of copying commands.
