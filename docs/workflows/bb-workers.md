# BB Workers: Spawn, Drive, And Inspect

## Goal

How a lane operator gives one bounded assignment to one BB thread, keeps writing
work isolated, exchanges results, and distinguishes completion from a stalled
turn. Model selection is separate; use
[`bb-worker-profiles.md`](bb-worker-profiles.md) for that policy.

`bin/bb_spawn.py` is the only sanctioned assignment-spawn path; it reaches
`plan_spawn` in `llm_collab/spawn_gate.py`, implements the spawn gate for this
workflow, and records the resulting assignment. The native `bb thread spawn`
examples below document the lifecycle shape only and must not be invoked as an
alternate assignment-spawn path.

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
an enforcement control.

**`full` is the intended mode for a worker, by operator policy.** A restricted
mode prompts for permission on trivial actions, which defeats the point of
delegating. Containment does not come from the permission mode and never did: it
comes from worktree isolation, the frozen delegation contract's MUST and MUST
NOT clauses, and the review gates. Permission mode is therefore not a control,
and not a factor when routing a lane to a profile. Choose `accept-edits` for a
preflight because a probe has nothing to write, not because it is safer.

That matters here because the `pi` provider rejects `accept-edits` and supports
only `full`, so a `pi` preflight is impossible: provision with a provider that
currently accepts `accept-edits`, then attach the `pi` writer to the verified
environment. Check current metadata after initializing `bb_cmd` below with
`"${bb_cmd[@]}" provider models <provider> --environment <id> --json`, and treat
the spawn's own HTTP 400 as authoritative. Do not freeze a provider-to-mode
table here; that is live provider metadata.

The no-write prompt bounds the preflight; the exact post-turn checks below
provide the proof. Resolve and record the requested base SHA before spawning:

Resolve it **in the selected repository**, not in whatever checkout you happen to
be standing in. Unscoped `git` reads the current checkout's `origin`, so on any
multi-project or multi-repo lane it yields a SHA from an unrelated repository and
provisioning either fails to resolve it or silently starts from the wrong commit.
Ask the repository for the path rather than rebuilding it from `projects.json`:
the value may be absolute, relative to `projects_root`, or `..`-relative, and
`resolve_project_repo_path` already owns those rules.

Use the exact project's configured `bb.executable` for the entire native
lifecycle. `bin/bb_spawn.py` obtains the same setting through
`client_from_project()`; using bare `bb` here could provision on one server and
ask the assignment gate to attach on another. An absent or malformed
`bb.executable` **refuses** — there is no PATH fallback. Resolve through the
seam itself (`bb_executable_from_project()`, GH-728) rather than re-reading
the field with jq: a second copy of the validation rules in a second language
diverges the first time the Python rule changes, and this procedure already
refused differently from the seam once. Preserve every configured argv token
in a Bash array:

```bash
repo_root=$(python3.11 -c "import sys; sys.path.insert(0, 'bin'); from _helpers import resolve_project_repo_path; print(resolve_project_repo_path('<project-id>', '<repo-id>'))")
base_branch=$(jq -r '.projects[] | select(.id=="<project-id>") | .default_branch_base' projects.json)
bb_cmd=()
while IFS= read -r -d '' token; do bb_cmd+=("$token"); done < <(python3.11 -c "import sys; sys.path[:0] = ['bin', '.']; from _helpers import get_project; from llm_collab.bb_client import bb_executable_from_project; sys.stdout.write(''.join(token + '\0' for token in bb_executable_from_project(get_project('<project-id>'))))")
# Belt-and-braces only: this count catches the seam subprocess producing
# nothing (its refusal text goes to stderr with a non-zero exit). Validity of
# the argv is the seam's decision, not this check's.
[ "${#bb_cmd[@]}" -gt 0 ] || { echo "REFUSED: missing bb.executable" >&2; exit 1; }
git -C "$repo_root" fetch origin "$base_branch"
base_sha=$(git -C "$repo_root" rev-parse "origin/$base_branch")
spawn_receipt=$(
  "${bb_cmd[@]}" thread spawn \
    --project <bb-project-id> \
    --new-environment worktree \
    --base-branch "$base_sha" \
    --provider codex \
    --model gpt-5.6-luna \
    --reasoning-level medium \
    --visibility visible \
    --permission-mode accept-edits \
    --title "Provision <writing task>" \
    --prompt "NO-WRITE WORKTREE PREFLIGHT. Do not modify files, create commits, or begin implementation." \
    --json
) || { echo "DO NOT RETRY: native spawn returned no trusted receipt" >&2; exit 2; }
printf '%s\n' "$spawn_receipt"
if ! printf '%s' "$spawn_receipt" | PYTHONPATH="<runtime_root>" \
  python3.11 <runtime_root>/bin/validate_bb_spawn_receipt.py; then
  echo "DO NOT RETRY: spawn receipt visibility was refused; reconcile the printed native id first" >&2
  exit 2
fi
```

Every fleet thread — provision probe, worker, and reviewer — is visible. The
operator full-visibility rule is absolute: the sanctioned client and this
native bootstrap both pass `--visibility visible`, and every returned receipt
must prove top-level `visibility == visible`. Missing, malformed, hidden, or
unexpected visibility is a refusal; do not retry because the native spawn may
already have created a thread. The command above prints the one receipt, then
validates its visibility through the shared runtime seam before continuing.

Do not rely on the remembered project default or inherited visibility: the
supported `bb project show <id> --json` API exposes project metadata but no
visibility getter/setter, while `bb thread spawn --help` confirms omitted
execution flags use remembered defaults. An explicit visible flag is therefore
the enforced invariant, including when a caller supplies a hidden parent;
hidden-parent inheritance must never launder a hidden child into acceptance.
This rule exists because the observed failure pattern hid approximately 65
BB threads when a dispatcher passed `--visibility hidden` and hidden-parent
inheritance amplified it. The operator unhid that existing fleet; new fleet
threads remain visible by construction.

Never pass a branch name as the base: BB resolves the local ref, while
`bin/local_main_sync.py` deliberately advances only the detached HEAD in a
parked checkout, so local `main` drifts. On 2026-08-07, `--base-branch main`
created a worktree at `03431b9a`, 44 commits behind `origin/main` at
`5afba296`, while `outcome == "available"` and
`headSha == mergeBase.baseRef` both passed; only comparison with the
independently resolved SHA caught the stale base.

BB has no standalone environment-create command — verified against 0.35.1,
0.36.0, and 0.37.0, where `bb environment` exposes only inspect-and-operate
subcommands — and `bb thread spawn` requires `--prompt`, so it cannot create a
chosen-base worktree without starting a turn. That is why provisioning takes a
turn at all. Do not put a writing delegation in that first turn. Wait for the probe to
become idle, then inspect the provisioned environment:

```bash
"${bb_cmd[@]}" thread wait <probe-thread-id> --status idle
"${bb_cmd[@]}" thread show <probe-thread-id> --json
"${bb_cmd[@]}" environment status <environment-id> --merge-base-branch "$base_sha" --json
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
  --json
```

The assignment goes through the script, not the native command: that is what
applies the spawn gate, injects the task-free profile-only first prompt, and
writes the assignment record. It does not accept a caller prompt. Write the
delegation to a file. After the execution event proves the exact triple, deliver
that file as the first task-bearing turn with `"${bb_cmd[@]}" thread tell <thread-id>
"$(cat <file>)" --mode queue`. The explicit queue mode keeps the task behind the
active profile-only turn; `steer` remains reserved for corrections to work already
running. Shell interpolation eats backticks, and a prompt
that arrives with a hole in it still reports success, so prefer BB's file or
attachment surface when the brief contains shell syntax.
Provider, model, and reasoning level are required flags; remembered project
defaults are never assignment authority. The first attached turn is
profile-only and receives no task content. Send the task-bearing frozen brief
only after the BB execution event proves the exact requested triple. For Claude
Code workers and reviewers, the only admitted triple is exact
`claude-code / claude-opus-5[1m] / medium`; Fable is supervisor-only.

The probe must be idle before that spawn and must receive no further messages.
After the writer successfully returns the same environment ID, archive the
probe immediately:

```bash
"${bb_cmd[@]}" thread archive <probe-thread-id> --json
```

Do not archive it earlier: when it is the last attached thread, BB destroys the
managed environment. BB 0.37.0 adds a five-minute archive Undo; a live probe
confirmed that immediate `bb thread unarchive` restores both the thread and its
retiring managed environment. That recovery window softens an accidental
archive but does not make expiry safe, so retain the ordering above. Do not
leave the probe merely idle afterward either; an idle
thread can be re-driven against the writer's worktree. Archive is a reversible
lane-ownership marker, not a capability barrier, so never unarchive or message
that probe. This handoff leaves one live thread with the writing assignment.

Every BB assignment, read-only included, always names an exact provider, model,
and reasoning level; never replace those fields with generic placeholders or an
inferred default. Read-only assignments produce the evidence that gates
decisions, and the measured glm-5.2 coordinate drift and muse-spark degeneration
corrupt an audit as readily as a patch—worse, because no diff review catches a
bad citation; see [`bb-worker-profiles.md`](bb-worker-profiles.md).

Two paths can start an authoring assignment, and they are deliberately governed
by different rules. On the inbound activation path, an arriving packet supplies
the assignment with no human in the loop. BB bootstrap resolves the profile from
its own policy and admits the assignment only when that profile belongs to the
qualified set; otherwise it refuses fail closed.

The explicitly selected path is deliberately not gated on qualification. Its
`plan_spawn` seam, reached through `bin/bb_spawn.py`, enforces the Contract v15
hard model exclusions and the isolation, exact base-SHA, registry, and scope
checks, and nothing about qualification. This is a decision, not an oversight:
an orchestrator decided both to start the specific lane and which exact provider,
model, and reasoning level it runs on, and that lane's output passes through the
review controls in
[`commit-push-prs.md`](commit-push-prs.md). The native `bb thread spawn` command
is not this assignment-spawn seam and remains forbidden as an alternate path.

On the explicitly selected path, the orchestrator must name the exact provider,
model, and reasoning level in the assignment, prefer a measured profile, and
record the executed triple by reading it back from the execution event, never
from a declared default. The qualified set is defined by the bootstrap profile
policy and checked on the inbound path; this workflow does not restate its
membership as runtime authority.

Known failure modes are hard exclusions for every text-bearing assignment. Do
not assign
`pi / meta/muse-spark-1.2-contributor`, whose text output degenerated, or
`pi / zai/glm-5.2`, whose source coordinates drifted.

## Communicate in both directions

The orchestrator sends follow-ups through BB itself:

```bash
"${bb_cmd[@]}" thread tell <thread-id> "<message>" --mode steer
"${bb_cmd[@]}" thread tell <thread-id> "<message>" --mode queue
```

`steer` is the default and lands inside an active turn. Use it for a correction,
hard stop, or unblock that must affect the work already running. `queue` waits
until the active turn finishes. `bb thread tell` is the inbound transport; do
not add a mailbox packet or wake step.

The worker or reviewer returns through one direct terminal BB tell to the exact
orchestrator thread:

```bash
"${bb_cmd[@]}" thread tell <orchestrator-thread-id> \
  "DONE|BLOCKED <assignment> | summary=<one line> | head=<exact SHA or none> | evidence=<exact artifacts and checks>"
```

The orchestrator does not run live-worker `wait`/`show`/`output` loops. After
that specific terminal report, or an abnormal watcher pointer for `error`,
`stopping`, or disappearance of a previously active worker, it may perform one
bounded `show`/`output`/`log` evidence read and then inspect the named artifact.
Normal `idle`, liveness, marker refresh, and unchanged baselines are silent.

If a durable record is wanted, the orchestrator authors the packet under its own
registered identity after reading and verifying the BB result. A BB worker must
not author a mailbox packet. It has no collab identity, and any registered agent
it supplies to `deliver.py --from` is recorded as the author, including that
agent's live session and thread-pair provenance. That is impersonation, not
relay. Relay provenance was considered and deliberately not built;
[GH-604](https://github.com/pixexid/llm-collab/issues/604) records that decision.
Revisit it only if a concrete downstream decision consumes producer identity.

Under the operator-sourced standing routing rule in
[`AGENTS.md`](../../AGENTS.md#bb-worker-surface), focus is BB until the Codex
app reaches parity with the Claude app and BB; use the Codex app only for
app-exclusive tooling. Do not switch harnesses merely because `deliver.py`
prints an AX command; GH-748 tracks that separate output change. Ask one
question: does this task need a Codex-app-only tool that BB cannot reach? If no,
use BB and never AX. If yes, do not delegate that app-exclusive work to a BB
worker; the orchestrator or operator performs it in the app through the
conditional AX procedure. This selects the surface for the work, not a wake
route: no AX path targets a BB-backed session, and a BB session binding
continues through BB. Even then, `VERIFIED` proves only that a turn rendered in
the lagging app UI; it does not
establish delivery to a working harness or a reply path, and app-side silence is
not evidence about a collaborator. BB provides no
delivery receipt, and the orchestrator still verifies the named artifact and
exact head.

## Delegate for a loopless worker

BB workers do not keep an autonomous task loop alive. Every delegation must
state all of these explicitly:

- the frozen scope and exact deliverable;
- the terminal action, such as commit/push/PR or a named BB result;
- one `DONE|BLOCKED` tell to the exact orchestrator thread with summary, exact
  head, and evidence;
- `Do not end the turn until the deliverable and terminal action are both done.`

Implementation workers commit and push their exact branch head, then report it;
they do not open the PR. The orchestrator independently verifies that pushed
head and owns PR creation and every later review, merge, and disposition gate.

An acknowledgement and `idle` status are not completion evidence. Verify both
artifacts before reporting the task finished. If the work adjudicates a review
finding, the worker also posts the disposition on the exact review thread; a
mailbox packet alone does not close that discussion. A genuine blocker gets a
clear BB report instead of silence or an invented partial completion; the
orchestrator authors any durable blocker packet.

## Inspect a pushed report or abnormal event

After a specific terminal report or abnormal watcher event, use one bounded BB
read rather than inferring state from elapsed time:

```bash
"${bb_cmd[@]}" thread show <thread-id> --json
"${bb_cmd[@]}" thread output <thread-id>
"${bb_cmd[@]}" thread log <thread-id> --format minimal
```

`show` reports thread/environment state, `output` returns the latest final
answer, and `log` shows the turn history. Add `--work-status` or `--git-diff` to
`show` when the artifact is a repository change. These are evidence reads after
an event, never a polling loop. A terminal tell remains a draft until its exact
head and artifact are independently verified.

## See also

- [`bb-worker-profiles.md`](bb-worker-profiles.md) — measured model routing and
  current selection limits.
- [`task-intake-and-delegation.md`](task-intake-and-delegation.md) — frozen task
  shape and one-writer rules.
- [`commit-push-prs.md`](commit-push-prs.md) — lane, verification, and PR gates.
