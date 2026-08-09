# BB Workers: Spawn, Drive, And Inspect

## Goal

How a lane operator gives one bounded assignment to one BB thread, keeps writing
work isolated, exchanges results, and distinguishes completion from a stalled
turn. Model selection is separate; use
[`bb-worker-profiles.md`](bb-worker-profiles.md) for that policy.

`bin/bb_spawn.py` is the only sanctioned assignment-spawn path; it implements
the spawn gate for this workflow and records the resulting assignment. The
native `bb thread spawn` examples below document the lifecycle shape only and
must not be invoked as an alternate spawn path.

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

Provisioning is not an assignment, and the distinction decides which command to
use. The preflight below carries no delegated work: it creates the worktree and
reports what it found. It therefore runs on the native command, which is the
only thing that can create an environment, and it is not an exception to the
sanctioned-path rule. Every assignment that follows — read-only or writing —
goes through `bin/bb_spawn.py` with `--environment <verified-id>`.

`bin/bb_spawn.py --new-environment worktree` refuses rather than doing this
step for you. bb provisions a new worktree asynchronously and returns
`environmentId: null`, so that form would create a real thread and only then
reject the envelope, leaving an orphan with no assignment record. The refusal is
deliberate containment; resolution semantics are
[GH-718](https://github.com/pixexid/llm-collab/issues/718). Until that lands,
create the environment here and attach to it.

BB has no read-only permission mode: `accept-edits`, `auto`, and `full` are all
write-capable. `accept-edits` below is the least-permissive available mode, not
an enforcement control. The no-write prompt bounds the preflight; the exact
post-turn checks below provide the proof. Resolve and record the requested base
SHA before spawning:

Resolve it **in the selected repository**, not in whatever checkout you happen to
be standing in. Unscoped `git` reads the current checkout's `origin`, so on any
multi-project or multi-repo lane it yields a SHA from an unrelated repository and
provisioning either fails to resolve it or silently starts from the wrong commit.
Ask the repository for the path rather than rebuilding it from `projects.json`:
the value may be absolute, relative to `projects_root`, or `..`-relative, and
`resolve_project_repo_path` already owns those rules.

```bash
repo_root=$(python3.11 -c "import sys; sys.path.insert(0, 'bin'); from _helpers import resolve_project_repo_path; print(resolve_project_repo_path('<project-id>', '<repo-id>'))")
base_branch=$(jq -r '.projects[] | select(.id=="<project-id>") | .default_branch_base' projects.json)
git -C "$repo_root" fetch origin "$base_branch"
base_sha=$(git -C "$repo_root" rev-parse "origin/$base_branch")
bb thread spawn \
  --project <bb-project-id> \
  --new-environment worktree \
  --base-branch "$base_sha" \
  --provider codex \
  --model gpt-5.6-luna \
  --reasoning-level medium \
  --permission-mode accept-edits \
  --title "Provision <writing task>" \
  --prompt "NO-WRITE WORKTREE PREFLIGHT. BB has no read-only permission mode. Do not modify files, create commits, or begin implementation. Report pwd, current branch, HEAD SHA, and git status --short --untracked-files=all, then end the turn." \
  --json
```

Never pass a branch name as the base: BB resolves the local ref, while
`bin/local_main_sync.py` deliberately advances only the detached HEAD in a
parked checkout, so local `main` drifts. On 2026-08-07, `--base-branch main`
created a worktree at `03431b9a`, 44 commits behind `origin/main` at
`5afba296`, while `outcome == "available"` and
`headSha == mergeBase.baseRef` both passed; only comparison with the
independently resolved SHA caught the stale base.

BB has no standalone environment-create command — verified against 0.35.1 and
again at 0.36.0, where `bb environment` exposes only inspect-and-operate
subcommands — and `bb thread spawn` requires `--prompt`, so it cannot create a
chosen-base worktree without starting a turn. That is why provisioning takes a
turn at all. Do not put a writing delegation in that first turn. Wait for the probe to
become idle, then inspect the provisioned environment:

```bash
bb thread wait <probe-thread-id> --status idle
bb thread show <probe-thread-id> --json
bb environment status <environment-id> --merge-base-branch "$base_sha" --json
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
python3.11 bin/bb_spawn.py \
  --assignment-kind writing \
  --collab-project <project-id> \
  --repo-target <repo-id> \
  --environment <verified-environment-id> \
  --base-sha "$base_sha" \
  --provider codex \
  --model gpt-5.6-sol \
  --reasoning-level high \
  --permission-mode full \
  --title "<writing task title>" \
  --prompt "<frozen writing delegation with the verified path, branch, base ref, and base SHA>" \
  --json
```

The assignment goes through the script, not the native command: that is what
applies the spawn gate and writes the assignment record. Write the delegation to
a file and pass `"$(cat <file>)"` — `bb thread tell` and shell interpolation both
eat backticks, and a prompt that arrives with a hole in it still reports success.

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

Every BB assignment, read-only included, always names an exact provider, model,
and reasoning level; never replace those fields with generic placeholders or an
inferred default. Read-only assignments produce the evidence that gates
decisions, and the measured glm-5.2 coordinate drift and muse-spark degeneration
corrupt an audit as readily as a patch—worse, because no diff review catches a
bad citation; see [`bb-worker-profiles.md`](bb-worker-profiles.md).
The example records current practice. On the BB bootstrap/first-delivery path,
`pi / kimi-coding/k3 / high` is the only authoring-qualified coordinate: that gate
admits an authoring assignment only when its resolved `BbProfile` is a member of
that exact one-profile qualified set, and refuses other profiles. The explicit
`bb thread spawn` writing path is not covered by this qualification gate; it
operates under the orchestrator's review controls and is not claimed to be
profile-qualified here.

Known failure modes are hard exclusions for every text-bearing assignment. Do
not assign
`pi / meta/muse-spark-1.2-contributor`, whose text output degenerated, or
`pi / zai/glm-5.2`, whose source coordinates drifted.

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
