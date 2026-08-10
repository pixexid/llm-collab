# Orchestrator Sessions

## Goal

Keep an orchestrator session observable, economical, and recoverable across a
session boundary. This document owns the orchestrator watcher set, succession,
supervisor relationship, model routing, and bb-update procedure.

**Standing direction: bb is the workbench, and we adapt it to fit.** Use a
builtin where it genuinely fits and build a custom plugin or runtime-side tool
where it does not, so that each round leaves worker operation easier and harder
to get wrong. Apply the ladder per instance — builtin as-is, builtin plus a
guardrail, custom plugin, runtime-side tooling — and stop at the first rung that
holds. Two constraints keep this from becoming plugin sprawl: every adopt-or-build
decision rests on a hands-on probe of what the builtin actually does rather than
on its documentation, and every customization must name a failure it makes
impossible or a manual step it deletes. When a lane hits bb friction, capture it
as a candidate with its rung rather than working around it silently; the running
list lives on [GH-562](https://github.com/pixexid/llm-collab/issues/562).

It does not own worker provisioning, profile availability, delegation, lane
limits, or PR gates. Use
[`bb-workers.md`](bb-workers.md),
[`bb-worker-profiles.md`](bb-worker-profiles.md),
[`task-intake-and-delegation.md`](task-intake-and-delegation.md), and
[`commit-push-prs.md`](commit-push-prs.md) for those procedures.

## Orchestrator topology

The supervisor is one singleton across all projects. Orchestrators are
per-project: each project has exactly one orchestrator session, and that session
runs one watcher set parameterized for its project. Starting another
orchestrator is a project-topology decision, not an ad hoc concurrency choice.

A project joining the workbench requires a named inbox owner before its first
lane, and the per-project orchestrator owns its project's drain. Record the owner
in the new project's own onboarding or runtime state, so the record lives with
the project it governs and is readable by whoever starts that project's first
lane. The exact field is not yet specified; the project-scoped state rule above
governs this record, so do not carry another project's state in it. Inbox readers
are already project-exact, so this is not a code gap: without the owner a project
can go live with nobody draining it, and work stalls silently with no error
anywhere.

Each project's handoff file lives in that project's own runtime state at
`{project_state_root}/{project_id}/orchestrator-handoff.md`. A handoff is project
state, not a checkout document: a checkout is not a project, and two registered
projects can coordinate the same product checkout — at which point a
checkout-relative path makes both orchestrators overwrite one succession file and
lose the other's live-lane and environment-delta state. Discoverability is served
by the session-gate hook, which prints the handoff path for the project it
resolved; a breadcrumb copy in the checkout would be a second thing to go stale.
Its watcher markers live
at `{project_state_root}/{project_id}/watchers/<name>.alive` and record the
owning `session_id`, exact `project_id`, and `started_at`. The marker content and
serialization are owned by the companion GH-722 implementation contract; this
workflow consumes that ownership record and does not redefine its format.

## Standard watcher set

Start all three watchers at the beginning of every BB orchestrator session as
persistent `Monitor` calls from the repository checkout. Substitute the exact
collaboration project, current orchestrator session ID, and `$SD` session
scratchpad. The script resolves the project's configured `bb.executable`,
`bb.project_id`, and `github.repo`; no watcher may substitute a PATH-default
BB or a checkout-default GitHub repository.

The watchers report changes; they do not decide that work is complete.

- A worker leaving `active` means **go look**. `idle` never means finished:
  read both `thread output` and `thread log`, then inspect the promised artifact
  and terminal action. `output` can be stale while a turn is active, and a
  wedged active lane produces no transition event. `bb thread output` returns the
  last *final* output; when a provider quota-death kills a worker mid-turn, that
  is the previous turn's report — complete, clean, and successful-looking — while
  the current turn produced nothing. Thread status is `error`, not `idle`. Read
  `bb thread log` alongside `output`, and treat the branch head as the truth
  about what landed; this matters even when the worker pushed its commit before
  it could post its dispositions.
- A startable issue excludes work blocked on an external actor, parked by a
  decision, and epics. A drained queue produces a status line, not permission to
  invent work. Queue and lane activation remain owned by
  [`Task Intake And Delegation`](task-intake-and-delegation.md).
- At handoff, **`TaskStop` every watcher first; then write the handoff**, whose
  status must say **`watchers stopped: yes` and list the stopped watcher task
  IDs**. The ordered succession procedure below owns the complete teardown.

Every watcher emits a liveness line every 20 cycles. It refreshes its
project-scoped marker through `bin/_watcher_liveness.py` only after a cycle
completed every check; a failed or incomplete cycle leaves the marker stale.

### Worker lifecycle

```bash
python3.11 bin/orchestrator_watch.py worker-lifecycle --project <COLLAB_PROJECT_ID> --session <SESSION_ID> --state-dir "$SD"
```

`--include-hidden` keeps probe threads observable. The watcher records
transitions, so steady state does not notify repeatedly.

### PR connector artifacts

```bash
python3.11 bin/orchestrator_watch.py pr-artifacts --project <COLLAB_PROJECT_ID> --session <SESSION_ID> --state-dir "$SD"
```

A merged or closed PR stays in the watch set through the full post-merge re-pass
window, then retires. Reopening resets that countdown. Failed, malformed, or
over-cap enumeration and any failed per-PR poll make the whole cycle incomplete.
`bin/pr_watch.py` remains the signature source covering the timeline, reactions,
and check runs; in particular, a clean connector pass can be reaction-only. The
meaning of connector artifacts and the required post-merge recheck remain
canonical in
[`Commit, Push, And PR Workflow`](commit-push-prs.md#pr-review-wait-gate).

### Guarded heartbeat and bb currency

```bash
python3.11 bin/orchestrator_watch.py heartbeat --project <COLLAB_PROJECT_ID> --session <SESSION_ID> --state-dir "$SD"
```

The heartbeat compares the installed BB version with `PINNED_BB_VERSION`, and
reports bounded open-PR, live-worker, and open-issue counts. It alerts on a
mismatch and on every failed, malformed, or over-cap probe; a broken check is
not a quiet repository and does not refresh liveness.

**The heartbeat reports inputs, and deliberately does not compute the lane
count.** A BB thread's status is not a lane: an idle writer waiting on review
still holds its lane, and an active read-only probe holds none. No store in this
repository records writing-lane occupancy, so a number derived here would be a
plausible wrong answer — worse than none, because it would be acted on. The
orchestrator holds the lane list and applies the cap; the definition and the
exemptions live in
[`Lane WIP limit`](task-intake-and-delegation.md#lane-wip-limit). If a
lane-occupancy store is ever added, the heartbeat should read it rather than
re-derive it.

## Verification traps

- **Audit all four connector artifact classes:** review threads, review bodies,
  issue/PR comments, and reactions. A finding can live only in a review body,
  where thread enumeration cannot see it; a clean pass can be reaction-only,
  with no review object or comment. Use `python3.11 bin/pr_watch.py`, whose
  signature covers the timeline, reactions, and check runs; a
  `gh pr view --json reviews,comments` poller cannot prove either outcome.
- **A declared default is not an executed value.** Read the executed provider,
  model, and reasoning level from the execution event.
- **Strong output and hollow verification are not in tension.** Mutation-prove
  regardless of how well a report reads. A worker can deliver a genuinely
  thorough fifteen-path enumeration of every read its change made reachable,
  including a correctly reasoned deliberately-unbounded entry, while a test
  named for a bound still passes with `read(-1)` because it asserts only that
  some read occurred. Quality of reasoning and hollowness of a specific test are
  independent.
- **Record fixtures from the live system rather than authoring them, and prefer
  recordings that contain what nobody would author.** A validator and its
  hand-authored fixtures agreed that a timestamp was text while the live system
  returned an integer, so every real-data cycle failed; review and a large test
  suite missed it because nothing sampled live output. The version fixture under
  `tests/fixtures/bb/` was re-recorded from the live CLI rather than edited by
  hand; the replacement recording contains 164 rows, including five
  error-status rows from the provider quota that killed two workers that day;
  those are states an author would not have supplied.
- **Treat queued specs as caches, including task lists and scratchpad plans.** A
  decision that changes where something lives, who owns it, or what a term means
  invalidates queued work that assumes the old world, and nothing marks it
  stale. A follow-up instruction was queued, then a later decision moved its
  artifact; the stale instruction was dispatched
  into a contract-versioned document until review caught the project-boundary
  violation. Reread queued items mentioning the affected artifact when such a
  decision lands, and ask what changed before dispatching work that has waited.
- **Bind verification to its checkout.** Print `pwd` and
  `git rev-parse HEAD` inside the suite directory, in the same shell invocation
  as the test run.
- **Automatic reachability is a new execution surface.** Making existing code
  automatic can break it without changing a line of that code, so a diff that
  only adds a caller cannot expose the defect. One session found four: an
  unbounded `read_text` that was fine for a hand-run command but not at every
  session start, a 20-second fetch deadline chosen for a deliberate command, a
  plain `open()` that could block on a FIFO, and `print` block-buffering under a
  pipe-backed Monitor that silently delivered nothing while the watcher reported
  healthy. Enumerate every read, subprocess, and network call the change newly
  reaches and give each a verdict.
- **A weak mutation fails for the wrong reason.** Removing an exact-match
  comparison made every candidate match and tripped a collision guard, so the
  cross-project tests failed because resolution broke, not because a
  cross-project write occurred. Inverting the comparison so a thread resolved to
  the wrong project was the discriminating mutation and exposed the actual
  defect: a row written into another project's artifact. A mutation that kills a
  test proves only coupling to the mutated line unless the failure names the
  property.
- **Gate autolink safety.** Make the prohibited-pattern check exit nonzero before
  publication; a warning on its own line prints and proceeds.
- **Re-check once after merge.** The connector can re-pass an amended head
  asynchronously; inspect the complete reviewed artifact set and adjudicate any
  late finding under the canonical PR workflow.

## Succession protocol

Continuation is by compaction by operator preference because a new orchestrator
session adds a handoff boundary. Start a new session only for genuine
context-quality degradation; when a boundary is necessary, the succession
protocol below remains the fallback. Anything verifiable headlessly or by a
worker must never wait idle on an operator click.

The per-generation handoff lives at
`{project_state_root}/{project_id}/orchestrator-handoff.md`. It carries only state
that a successor cannot recover from the repository and GitHub:

- handoff status and last-updated time, including `watchers stopped: yes` and
  the stopped watcher task IDs;
- live BB threads, lanes, and PRs that need continuation;
- **Environment deltas and operator announcements**: tool or CLI changes,
  quota events, untracked configuration changes, machine changes, and operator
  statements that can invalidate a lane;
- open decisions and genuine external blockers.

The environment-deltas section is mandatory. If nothing changed, say that
explicitly; an empty section is a claim that there were no deltas, not an
omission. Do not copy this document's procedure into the handoff.

### Predecessor

1. **`TaskStop` every watcher first.**
2. **Then write the handoff file.** Reconcile its live-thread, PR, decision,
   blocker, and environment-delta state; its status section must state
   **`watchers stopped: yes` and list the stopped watcher task IDs**.
3. Flag the handoff and ping the supervisor session. Writing the file without
   that ping is not a
   handoff.

The order and evidence are load-bearing. A predecessor's surviving watchers
double-notify the successor, while a surviving heartbeat can argue a retired
session into starting work. That leaves two orchestrators driving one project:
the session-level form of the same one-writer conflict that lane isolation
prevents.

### Successor bootstrap

1. Start through the deployed `current_runtime.py` launcher, read `AGENTS.md`
   and all Required Reading, and load Ponytail. The canonical startup command
   and runtime-root rule live in [`Session Startup`](session-startup.md).
2. Read the handoff in full, beginning with Environment deltas and operator
   announcements.
3. Check the three project-scoped watcher markers before starting watchers or
   work. A fresh marker owned by a different session means the predecessor's
   watcher is still alive and teardown is incomplete; use the task IDs recorded
   in the handoff and do not proceed until no fresh foreign-session owner
   remains. Marker ownership and freshness are defined by the companion GH-722
   implementation contract, not by this workflow.
4. Check environment currency before the first spawn. Repository currency is
   not tool currency: compare the live installed bb version with
   `PINNED_BB_VERSION` as the heartbeat does, then verify every additional tool,
   quota, configuration, or machine delta named by the handoff. A mismatch here
   is planned recovery; the same mismatch at the first refused spawn is an
   outage.
5. Ping the supervisor session that the successor is online.
6. Start the watcher set above.
7. Recover live work through the canonical BB, delegation, and PR workflows
   linked at the top of this document.

If anything looks inconsistent during bootstrap, read the predecessor
session's tail before improvising. A contradiction is evidence to recover, not
permission to guess.

## Supervisor arrangement

The singleton supervisor preserves continuity across projects and orchestrator
generations and routes operator-owned decisions; each per-project orchestrator
owns that project's technical execution and verification.

- Route business priority, irreversible choices, credentials/account changes,
  and product decisions without stated authority to the supervisor. Keep
  technical scope, implementation choices, bounded recovery, and the normal
  review/release flow with the orchestrator. The broader escalation boundary is
  canonical in [`AGENTS.md`](../../AGENTS.md#workers-own-their-own-setup).
- Spend expensive-model tokens on orchestration, independent review, and
  judgment. Delegate bounded work that a cheaper eligible worker can perform;
  evaluation candidates use their own execution tokens.
- The supervisor is the mandatory handoff signal path, not a second queue or
  task store. Lane ownership, caps, activation, and terminal evidence remain in
  the linked canonical workflows.

## Model routing policy

Operator policy for orchestrator-selected work:

- Route complex authoring to `k3` or `sol`. When a live `k3` attempt reports a
  quota refusal, use `sol`, never `luna`, for that complex authoring lane.
- Use `luna` as the daily driver for work within its measured boundary.
- Request maximum reasoning effort for `luna` and `glm-5.2`. Contract v15's
  hard exclusions still apply to every text-bearing assignment, so this effort
  setting does not authorize an excluded model; only new measurement and a
  contract change can lift an exclusion.
- Apply the current qualification and exclusion rules from
  [`BB Worker Profiles`](bb-worker-profiles.md) and
  [`BB Workers`](bb-workers.md#spawn-in-an-isolated-worktree). Do not copy the
  current qualified-set membership into a handoff or this policy. Its authority
  is `AUTHORING_QUALIFIED_PROFILES` in `llm_collab/bb_bootstrap.py`; inspect that
  value for the rule-enforced set and query the execution machine as the profile
  workflow requires for live availability.
- Treat quota as live provider state, never as a carried assertion. Establish it
  from the latest execution on the selected profile and inspect `output` and
  `log` for a provider quota or permission refusal before rerouting.
- Key each model-evaluation row by the executed
  `(provider, model, reasoning_level)` triple read from the execution event, not
  requested argv or a declared default. The artifact definition and source are
  owned by
  [`bb-plugins/exec-tracking/README.md`](../../bb-plugins/exec-tracking/README.md).
  Store the bb version observed for that execution with the row so comparisons
  across CLI versions remain interpretable.

This section states routing rules and checks. It intentionally asserts no
current bb version, qualified-set membership, model availability, or quota
state.

## bb-update procedure

The exact pin is a safety control. Do not loosen it to make an unobserved bb
release pass.

1. Stop new BB lane starts and compare the installed version with
   `PINNED_BB_VERSION`. If the probe itself fails, repair the probe before using
   any quiet result.
2. Read the four load-bearing CLI properties named at the top of
   `llm_collab/bb_client.py`. Re-observe **all four** against the live installed
   CLI before changing the pin; prior observations and release notes are not
   substitutes.
3. Re-record `tests/fixtures/bb/settings_version.json` from the live configured
   CLI's `settings version --json` output. Do not hand-edit a file whose purpose
   is to preserve a recording.
4. Inspect every version-mismatch test before moving the pin. A wrong-version
   literal must not equal the new pin; otherwise the test becomes a match while
   claiming to prove refusal.
5. Sweep `AGENTS.md`, `docs/`, fixtures, and tests for commands whose semantics
   changed and version-stamped claims that must be re-verified. Update or record
   a disposition for each affected claim; never carry a live observation
   forward as an assertion about the new release.
6. Author the pin, fixture, test, and necessary documentation changes in the
   orchestrator thread. This change cannot be delegated through the version gate
   it unblocks. Use the normal one-writer and PR workflow, including the required
   verification and first automatic review; this is not an exception to those
   gates.
7. Run `"${bb_cmd[@]}" plugin reload exec-tracking` — the project's configured
   executable, never a bare `bb`, or a wrapper project reloads the PATH
   installation instead of the instance its spawns use, leaving the intended
   plugin stale while the proof appears to run. Then launch one controlled probe and require
   a new recorded row in the configured project's executed-triple artifact. A
   running plugin does not follow a changed checkout, and a running status alone
   does not prove recording.
8. Record the observed bb version beside every evaluation row created during the
   update or its qualification probes.

The current pin is read from source, the installed version is read live, the
qualified set is read from its code authority, model availability is queried on
the execution machine, quota is established by a live execution result, and the
executed triple is read from its execution event. None is inferred from this
document.
