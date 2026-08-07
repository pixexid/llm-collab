# BB Workers: Spawn, Drive, And Inspect

## Goal

How a lane operator gives one bounded assignment to one BB thread, keeps writing
work isolated, exchanges results, and distinguishes completion from a stalled
turn. Model selection is separate; use
[`bb-worker-profiles.md`](bb-worker-profiles.md) for that policy.

## What a BB worker is

A BB worker is a provider-backed BB thread acting for the orchestrator on one
assignment. Its BB thread and environment IDs identify BB resources only. They
do not give the worker an `agents.json` identity, an llm-collab exact-session
binding, a receipt, first-class participant status, or canonical-bus membership.
The orchestrator remains the integration point.

Use one worker thread per assignment or review disposition. A no-write
provisioning probe may precede one writing worker; it does not receive the
writing assignment. A new assignment, model, or independent review gets a new
thread.

## Spawn in an isolated worktree

Without `--new-environment worktree` or an explicit `--environment`, BB uses the
project's default source checkout. That is the shared checkout in the current
fleet. `--permission-mode full` bypasses sandbox and approval protections, so
combining it with that default is a live write hazard.

A read-only assignment may create its worktree and start in one command. This
form also provisions the environment for a later writing lane, but BB has no
read-only permission mode: `accept-edits`, `auto`, and `full` are all
write-capable. `accept-edits` below is the least-permissive available mode, not
an enforcement control. The no-write prompt bounds the assignment; the exact
post-turn checks below provide the proof. Resolve and record the requested base
SHA before spawning:

```bash
git rev-parse <base-branch>
bb thread spawn \
  --project <bb-project-id> \
  --new-environment worktree \
  --base-branch <base-branch> \
  --provider codex \
  --model gpt-5.6-luna \
  --reasoning-level medium \
  --permission-mode accept-edits \
  --title "Provision <writing task>" \
  --prompt "NO-WRITE WORKTREE PREFLIGHT. BB has no read-only permission mode. Do not modify files, create commits, or begin implementation. Report pwd, current branch, HEAD SHA, and git status --short --untracked-files=all, then end the turn." \
  --json
```

BB 0.35.1 has no standalone environment-create command, and `bb thread spawn`
requires `--prompt`; it cannot create a chosen-base worktree without starting a
turn. Do not put a writing delegation in that first turn. Wait for the probe to
become idle, then inspect the provisioned environment:

```bash
bb thread wait <probe-thread-id> --status idle
bb thread show <probe-thread-id> --json
bb environment status <environment-id> --merge-base-branch <base-branch> --json
```

Before activating the writer, verify the exact path and branch from `show` and
require a healthy response (`outcome == "available"`) plus every one of these
`status` predicates:

```text
workspace.workingTree.hasUncommittedChanges == false
workspace.checkout.headSha == <requested-base-sha>
workspace.mergeBase.baseRef == <requested-base-sha>
```

Reject a dirty tree and a moved HEAD separately: cleanliness says nothing about
a commit made by the probe. Include the verified path, branch, base ref, and
base SHA in the frozen writing delegation. Only then attach the writing worker
to the verified environment:

```bash
bb thread spawn \
  --project <bb-project-id> \
  --environment <verified-environment-id> \
  --provider codex \
  --model gpt-5.6-sol \
  --reasoning-level high \
  --permission-mode full \
  --title "<writing task title>" \
  --prompt "<frozen writing delegation with the verified path, branch, base ref, and base SHA>" \
  --json
```

The probe must be idle before that spawn and must receive no further messages.
After the writer successfully returns the same environment ID, archive the
probe immediately:

```bash
bb thread archive <probe-thread-id> --json
```

Do not archive it earlier: when it is the last attached thread, BB destroys the
managed environment. Do not leave it merely idle afterward either; an idle
thread can be re-driven against the writer's worktree. Archive is a reversible
lane-ownership marker, not a capability barrier, so never unarchive or message
that probe. This handoff leaves one live thread with the writing assignment.

A writing lane always names an exact provider, model, and reasoning level;
never replace those fields with generic placeholders or an inferred default.
The example records current practice, not an authoring qualification: no BB
profile is authoring-qualified yet, and
[GH-596](https://github.com/pixexid/llm-collab/issues/596) tracks that missing
selector and evaluation. The compensating pre-merge control is the
orchestrator's review of the real diff, focused tests, mutation proof when
executable logic changes, and the connector pass—not a claimed property of the
model. Until GH-596 lands, an explicitly selected writing lane operates under
those controls even though the profile policy remains prospective.

Known failure modes are hard exclusions for writing work. Do not assign
`pi / meta/muse-spark-1.2-contributor`, whose text output degenerated, or
`pi / zai/glm-5.2`, whose source coordinates drifted. A corrupted glyph or
wrong coordinate inside a patch is not safe merely because review may catch it.

## Communicate in both directions

The orchestrator sends follow-ups through BB itself:

```bash
bb thread tell <thread-id> "<message>" --mode steer
bb thread tell <thread-id> "<message>" --mode queue
```

`steer` is the default and lands inside an active turn. Use it for a correction,
hard stop, or unblock that must affect the work already running. `queue` waits
until the active turn finishes. `bb thread tell` is the inbound transport; do
not add a mailbox packet or wake step.

The worker returns through BB. The orchestrator ingests the result with BB's
read surfaces:

```bash
bb thread output <thread-id>
bb thread show <thread-id> --json
bb thread log <thread-id> --format minimal
```

If a durable record is wanted, the orchestrator authors the packet under its own
registered identity after reading and verifying the BB result. A BB worker must
not author a mailbox packet. It has no collab identity, and any registered agent
it supplies to `deliver.py --from` is recorded as the author, including that
agent's live session and thread-pair provenance. That is impersonation, not
relay. [GH-604](https://github.com/pixexid/llm-collab/issues/604) tracks a real
relay-provenance surface; it is prospective, not present.

Contract v12's unchanged predicate remains the authority for whether the
orchestrator's `deliver.py` call offers an AX doorbell. If it does, run only the
exact command it prints. Whether that doorbell can land is a dynamic runtime
property: run the printed command, or run
`axsend-ensure tree --app Codex --editable-only` and read its current `windows`
count. Do not infer reachability from process names: `pgrep -x Codex` can return
no match while the surface is live inside `ChatGPT.app`. BB is the routine
worker fleet; AX is not a BB transport or routine lane. BB provides no delivery
receipt, and the orchestrator still verifies the named artifact and exact head.

## Delegate for a loopless worker

BB workers do not keep an autonomous task loop alive. Every delegation must
state all of these explicitly:

- the frozen scope and exact deliverable;
- the terminal action, such as commit/push/PR or a named BB result;
- `Do not end the turn until the deliverable and terminal action are both done.`

An acknowledgement and `idle` status are not completion evidence. Verify both
artifacts before reporting the task finished. If the work adjudicates a review
finding, the worker also posts the disposition on the exact review thread; a
mailbox packet alone does not close that discussion. A genuine blocker gets a
clear BB report instead of silence or an invented partial completion; the
orchestrator authors any durable blocker packet.

## Inspect completion and stalls

Use BB's read surfaces rather than inferring state from elapsed time:

```bash
bb thread show <thread-id> --json
bb thread output <thread-id>
bb thread log <thread-id> --format minimal
```

`show` reports thread/environment state, `output` returns the latest final
answer, and `log` shows the turn history. Add `--work-status` or `--git-diff` to
`show` when the artifact is a repository change.

`bb thread wait <thread-id>` waits for `idle` by default, but a normal completed
turn also becomes idle. A status-only monitor therefore cannot distinguish
ordinary completion from a worker ending its turn with work outstanding. The
actionable stall signal is **idle while the delegated deliverable or terminal
action is still outstanding**. Inspect the output, log, and artifact before
re-driving the worker.

## See also

- [`bb-worker-profiles.md`](bb-worker-profiles.md) — measured model routing and
  current selection limits.
- [`task-intake-and-delegation.md`](task-intake-and-delegation.md) — frozen task
  shape and one-writer rules.
- [`commit-push-prs.md`](commit-push-prs.md) — lane, verification, and PR gates.
