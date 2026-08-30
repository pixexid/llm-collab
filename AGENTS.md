# AGENTS.md

## Archive status

This repository is the retired `llm-collab` implementation. Its implementation,
documentation, workflows, runbooks, state formats, and examples are historical
artifacts, not current collaboration instructions. Inspect or maintain them only
when a task explicitly scopes work to this archive.

Do not launch, restore, extend, or use the archived runtime to coordinate work.
Do not treat its roles, queues, mirrors, inbox files, runtime commands, ownership
rules, or delivery mechanics as authority for any project.

## Current collaboration

Current work uses native BB:

- Run every agent session in a visible BB thread.
- Give each worker one bounded task, with the responsible manager as its direct
  parent.
- Run independent work in parallel. Serialize only real dependencies or
  semantic/file collisions.
- Never keep a thread alive to poll or sleep. Use native events or a bounded
  one-shot automation with a named owner and cleanup for external waits.
- Native thread status, parent delivery, and interactions are authoritative.
  Silence is not evidence of success or delivery.
- Preserve dirty worktrees, untracked files, and exact evidence until their
  owner explicitly disposes of them.
- Inspect native state before retrying. Never blindly retry or duplicate an
  instruction or mutation.

## Operator requests

Operator Inbox is the only channel for a request requiring operator approval,
review, a decision, clarification, or permission.

- Send exactly one `needs-decision` message from the thread that owns the
  request.
- Use `urgent` only when delay is time-sensitive and harmful.
- Do not duplicate the actionable request in chat, notifications, issues, or
  another inbox.
- After sending, return `WAITING: operator response in Inbox` and resume from
  the native reply.
- Request credentials through the Secrets plugin, never through Operator Inbox.
- If Operator Inbox is unavailable, return `BLOCKED:` naming the missing plugin.
- If credentials are required and Secrets is unavailable, return `BLOCKED:
  Secrets plugin unavailable`.

## Archive maintenance

- Make the smallest complete change within the explicitly authorized scope.
- Inspect `git status` before switching branches, pulling, staging, committing,
  or cleaning. Never discard unrelated tracked changes or untracked evidence.
- Do not rewrite historical documentation as current guidance. Preserve its
  historical meaning unless the task explicitly asks to correct or annotate it.
- Do not modify generated, runtime, migration, or evidence artifacts merely to
  make an archival check pass.
- Fixtures presented as real system output must retain their recorded shape and
  provenance. Deliberately malformed fixtures must say what they represent and
  why recorded output cannot represent that case.

## Repository safety

This repository defines a GitHub autolink for the `GH-` prefix. Never place a
GitHub closing keyword immediately before `GH-<number>` in a commit message, PR
body, or issue comment unless closure is intentional. Use `Related GH-123`,
`Related #123`, or the full issue URL instead.

If implementation work is explicitly authorized:

- Reject embedded NUL bytes in SQLite text constraints that rely on
  `length`, `GLOB`, `LIKE`, or `substr`; those functions can stop at the NUL.
- For correlated authority-sensitive reads, pin one opened descriptor chain.
  Re-resolving ancestors by pathname does not prove a shared root.
- Bounds on work that claims completeness must fail closed, never silently
  truncate. A partial result is acceptable only when it carries an explicit
  truncation signal.
- After a non-idempotent operation reports success or becomes ambiguous, every
  downstream failure must suppress retries. Preserve a recoverable native
  identity; otherwise surface ambiguity.
- For schema-dependent changes, identify the database used for proof, who
  migrated it, and the production migration path.

Run the smallest discriminating focused checks first. For implementation or
shared-contract changes, also run the full suite:

```bash
python3.11 -m unittest discover -s tests
```

Documentation-only archival cleanup does not require the implementation suite;
run targeted content, link, and diff checks instead.

End work with `DONE: <sha> / <files> / <checks>` or `BLOCKED: <reason>`.
