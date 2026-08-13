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
Manual promotion of an orchestrator role into a BB thread, including the frozen
spawn brief, is owned by
[`Manual BB-Native Orchestrator Cutover`](bb-native-cutover-runbook.md).

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
owning `session_id`, exact `project_id`, `started_at`, process `pid`, and the
stable watcher-invocation `argv_marker`. The marker content and serialization
are owned by the companion GH-722 implementation contract; this workflow
consumes that ownership and liveness record and does not redefine its format.

## Standard watcher set

The session gate and sanctioned writing-spawn path require the configured BB
server to report `exec-tracking` exactly `running`; stopped, disabled, missing,
or unreadable status fails closed even when both host markers are fresh. Then
start exactly two host watchers as persistent `Monitor` calls from the repository checkout:
`pr-artifacts` and `heartbeat`. Substitute the exact collaboration project,
current orchestrator session ID, and `$SD` session scratchpad. The script
resolves the project's configured `bb.executable`, `github.repo`, and the
deduplicated native BB projects for every registered repository target. No
watcher may substitute a PATH-default BB, a checkout-default GitHub repository,
or a partial native-project result.

The watchers report changes; they do not decide that work is complete.

- Every worker and reviewer brief ends with one direct terminal tell to the
  exact orchestrator thread,
  `bb thread tell <orchestrator-thread>`, carrying `DONE|BLOCKED`, summary, exact
  head, and evidence. The orchestrator does not poll `wait`, `show`, or `output`
  on a live worker. It performs one bounded evidence read only after that exact
  terminal tell or a watcher abnormal pointer.
- The plugin observes native `thread.failed`, `thread.deleted`, and
  `thread.archived` events plus one load-time abnormal-state reconciliation.
  It resolves the current project role at fire time and sends one fixed,
  all-`agent-only` pointer with `queue-if-active`. Normal `idle` never wakes;
  it only re-arms a consumed pending pointer. An abnormal pointer says **go
  inspect**; it is not completion evidence.
- PR/review/check changes and deployed-version drift enter that same plugin
  authority through `bb silent-wake emit`. One plugin SQLite reservation
  coalesces producers and daemon reloads. Accepted and ambiguous sends suppress
  retry. A retryable 4xx (408, 425, or 429) keeps the reservation durable and
  schedules one event-loop retry; plugin reload resumes that same reservation. A
  terminal confirmed send failure releases an unchanged reservation and makes
  the host cycle fail before its baseline or marker advances; if another event
  changed that same reservation in flight, its claimant sends the latest state
  instead of releasing it unseen.
- A startable issue excludes work blocked on an external actor, parked by a
  decision, and epics. A drained queue produces a status line, not permission to
  invent work. Queue and lane activation remain owned by
  [`Task Intake And Delegation`](task-intake-and-delegation.md).
- At handoff, **`TaskStop` both host watchers first; then write the handoff**, whose
  status must say **`watchers stopped: yes` and list the stopped watcher task
  IDs**. The ordered succession procedure below owns the complete teardown.

Each host watcher emits a liveness line every 20 cycles. It refreshes its
project-scoped marker through `bin/_watcher_liveness.py` only after a cycle
completed every check; a failed or incomplete cycle leaves the marker stale.
Plugin `running` state is the native abnormal-wake liveness signal; there is no
third marker. A changed checkout is not a changed running plugin: after an
independently reviewed plugin draft, the orchestrator reloads through the
project's configured BB executable and performs the dedicated visible
all-agent-only probe described in
[`Exec-Tracking Plugin`](exec-tracking-plugin.md#silent-wake-activation-gate).

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

- **Quote mid-lane proposals before reporting action.** When an orchestrator
  sends a proposal or correction into an active lane, the worker quotes the
  instruction verbatim in its report before describing what it did. A proposal
  about a gap between two surfaces returned as its opposite: an exclusion became
  an `unless` clause permitting the excluded case. The change and report were
  each internally coherent; only placing the original beside the result exposed
  the inversion. A paraphrase reports the worker's interpretation, which is the
  thing under suspicion.
- **A grep bounds its pattern, not the world.** A zero-hit or low-hit result says
  only what that pattern matched; any claim about what exists requires a second,
  differently shaped check. A directory-axis guard check missed a multiline
  form; `run the exact` missed three required-reading files containing `run
  exactly`, including the sharpest correction under audit; and a match count
  became an issue work plan before reading showed the matched text said the
  opposite.
- **Line-scoped checks cannot see wrapped prose.** A grep over hard-wrapped
  Markdown reads fragments, not sentences. One sweep called an instruction
  unconditional because its condition was on the preceding wrapped line. Widen
  the result to its enclosing sentence or paragraph before recording a prose
  finding; a false defect that reaches a worker costs a lane.
- **A line that holds only until it is tested is not a line.** When a proposed
  rule's scope cannot be decided by inspection, do not ship it as a sweep;
  convert the residue into a maintenance stream with a recorded trigger. A sweep
  for prose that implied a condition acquired a new loophole under every hostile
  reading because implication was not pattern-decidable. Recording that
  undecidability and its maintenance trigger produced an honest deferral;
  pretending the sweep had a completion criterion produced an open-ended lane.
  Related GH-751.
- **Never gate a connector verdict on `commit_id` alone.** At the exact head,
  evidence that the pass produced a result can be a review with a non-empty
  body, a top-level connector comment naming that head, a 👍 reaction, or new
  inline threads. The
  [`PR Review Wait Gate`](commit-push-prs.md#pr-review-wait-gate) is canonical
  for which signals are terminal and under what conditions; this list only
  prevents `commit_id`-only gating. An empty-bodied review means the pass is
  starting, not that a verdict has arrived; the connector can post the
  body-bearing review up to six minutes later. On one head, a review at the exact head, zero new threads, a green full
  suite, and written dispositions for every prior finding appeared to satisfy
  every merge condition; the real review arrived six minutes later carrying a
  P1. Two sibling fields are equally unsafe as finding records: a thread's
  `commit_id` re-anchors to the newest head and says where the thread points
  now, not which head raised it, while its `line` can become `null` when a fix
  restructures the code beneath it. **The written disposition must carry the
  finding, never lean on where the thread sits.**
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
- **Scope fixture provenance by role.** A fixture that stands in for real system
  output is recorded from the live system; review it before committing, sanitize
  non-load-bearing content, and preserve the fields and states under test. Prefer
  a recording that contains what nobody would author. A fixture representing a
  malformed, adversarial, or otherwise un-inducible state is authored deliberately
  and carries a comment saying what it represents and why it cannot be recorded.
  The defect is not authorship: it is an authored fixture claiming to represent
  reality with no live sample in the loop to contradict it. A validator and its
  hand-authored fixtures agreed that a timestamp was text while the live system
  returned an integer, so every real-data cycle failed; review and a large test
  suite missed it because nothing sampled live output. The version fixture at
  `tests/fixtures/bb/settings_version.json` was re-recorded from the live CLI
  rather than edited by hand; the replacement recording at
  `tests/fixtures/bb/thread_list.json` contains 164 rows, including five
  error-status rows from the provider quota that killed two workers that day—
  states an author would not have supplied. Redacting a task title keeps the
  recording honest; changing an integer timestamp to text destroys the evidence.
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
- **Tightening a definition invalidates its own prior citations, silently.**
  Re-audit every *use* of a definition in the same pass that narrows it; nothing
  flags the stale ones. A change that tightened `ENFORCED` to exclude alert-only
  behaviour left, two sections below, an `ENFORCED` label whose cited evidence
  was a watcher event and a session-gate line that prints a warning and returns
  0 — alert-only, forbidden by the definition introduced in that same changeset.
  The rule failed against itself inside the commit written to fix that class of
  failure. Grep the label, term, or constant you just narrowed and re-check each
  hit against the new wording before pushing; the citations that were true under
  the old definition are exactly the ones nobody re-reads.
- **CI green on a schema-dependent change is not deploy safety.** Wherever CI
  migrates its own test database, the suite validates the change against a schema
  **CI created** — not the one production has. Every writer passes, every check is
  green, and the merge ships code whose target schema does not exist where it
  lands. With auto-deploy-on-main and a migration runner scoped to local or CI
  environments, there is no step between green and broken. An amiga PR that was
  green everywhere would have broken all production notification writes on merge;
  it was stopped by asking where the schema under test came from, not by any
  check. This is the same claim-versus-evidence shape as the entries above — CI
  proves the code matches a schema, and it is read as the code being safe to
  deploy. **The gate itself lives in
  [`Commit, Push, And PR Workflow`](commit-push-prs.md), which owns merge
  obligations; this entry is the rationale, not a second policy home.**
- **Re-check once after merge.** The connector can re-pass an amended head
  asynchronously; inspect the complete reviewed artifact set and adjudicate any
  late finding under the canonical PR workflow.

## Succession protocol

Continuation is by compaction by operator preference because a new orchestrator
session adds a handoff boundary. On BB 0.37.0, compact an idle or errored long
authority thread with `bb thread compact <thread-id>`; completion or refusal is
visible in its timeline. Start a new session only for genuine
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
3. Check the two project-scoped host-watcher markers and the `exec-tracking`
   plugin's running state before starting watchers or
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
6. Start the two host watchers above; do not start a third host process.
7. Recover live work through the canonical BB, delegation, and PR workflows
   linked at the top of this document.

If anything looks inconsistent during bootstrap, read the predecessor
session's tail before improvising. A contradiction is evidence to recover, not
permission to guess.

## Supervisor arrangement

The singleton supervisor preserves continuity across projects and orchestrator
generations and decides on the operator's behalf wherever doing so keeps the
process unblocked; each per-project orchestrator owns that project's technical
execution and verification.

- Route decisions beyond the per-project orchestrator's stated authority to the
  supervisor. The supervisor decides unless the canonical operator-only boundary
  in [`AGENTS.md`](../../AGENTS.md#workers-own-their-own-setup) applies; do not
  carry a `pending operator decision` state for anything else. Keep technical
  scope, implementation choices, bounded recovery, and the normal review/release
  flow with the orchestrator.
- Spend expensive-model tokens on orchestration, independent review, and
  judgment. Delegate bounded work that a cheaper eligible worker can perform;
  evaluation candidates use their own execution tokens.
- The supervisor is the mandatory handoff signal path, not a second queue or
  task store. Lane ownership, caps, activation, and terminal evidence remain in
  the linked canonical workflows.
- **Formerly operator-only approval gates** (operator ruling, 2026-08-11): the
  operator has delegated these to the supervisor and orchestrator jointly. Such
  an approval is satisfied by recorded supervisor+orchestrator concurrence with
  a **dispositioned distinct-provider read** — a written read from a
  non-Anthropic model (routinely a read-only BB thread on `codex / gpt-5.6-sol`
  at `high`; the heaviest calls may use a stronger external model), agreed with
  or overruled with reasoning in the concurrence record. The read informs and
  does not veto; a record that silently ignores it is incomplete. Reasoning and
  revert path are written on the task or issue artifact. Operator approval
  remains valid everywhere. Routine technical calls are unchanged.

## Model routing policy

Authoring labels in this document are deliberately reduced and structural. An
operational clause is only a top-level bullet or numbered step inside exactly
`## Model routing policy` or `## bb-update procedure`. Use exactly three labels:
`ENFORCED` when a named code path refuses the action, quarantines the input, or
forces a safe state — an alert alone is **not** enforcement, because it reports
the violation while leaving it possible;
`CHECKED-CONVENTION` when practice has a runnable command that detects a
violation, and `JUDGMENT` when compliance is not mechanically decidable, so it
carries no command by design; inventing one for a `JUDGMENT` clause is the
failure this scope avoids.

A clause that is mechanically decidable but has **no tested checker yet** is
none of the three. Record it as an `IMPLEMENTATION GAP` naming the work that
would close it. Calling it `JUDGMENT` would assert an oracle gap that does not
exist; calling it `CHECKED-CONVENTION` would credit a checker nobody wrote. Both
are the same defect the vocabulary exists to prevent.

A normative document may name a **stable invocation** of tested code; it must
not carry the implementation. Embedded interpreters, JSONL parsing,
snapshot-diffing, invented record identity, pagination and multi-stage pipelines
belong in a tested script, where bounds and both-direction proofs can live. Nothing outside these two sections carries any
labeling implication. Extending the scope is a per-section deliberate
follow-up, and a section that resists crisp classification is recorded as such
rather than swept, as in Related GH-751.

Operator policy for orchestrator-selected work:

- **JUDGMENT** Route complex authoring to `k3` or `sol`. When a live `k3` attempt reports a
  quota refusal, use `sol`, never `luna`, for that complex authoring lane.
- **JUDGMENT** Use `luna` as the daily driver for work within its measured boundary.
- **IMPLEMENTATION GAP** Request maximum reasoning effort for `luna` and `glm-5.2`. Contract v15's
  hard exclusions still apply to every text-bearing assignment, so this effort
  setting does not authorize an excluded model; only new measurement and a
  contract change can lift an exclusion.
  No tested checker exists for this today. The command removed from here
  parsed JSONL, diffed a snapshot and invented record identity, and it
  misclassified real rows; see
  [GH-771](https://github.com/pixexid/llm-collab/issues/771).
- **ENFORCED** Apply the hard model-exclusion rule. On the explicitly selected
  path, `plan_spawn` in `llm_collab/spawn_gate.py`, reached through
  `bin/bb_spawn.py`, refuses `excluded_model`; on the inbound bootstrap path,
  the profile gate refuses `bb_bootstrap_profile_unavailable`. See
  [`BB Workers`](bb-workers.md#spawn-in-an-isolated-worktree) and
  [`BB Worker Profiles`](bb-worker-profiles.md) for the path controls.
  **JUDGMENT** On this section's explicitly selected path, choosing a qualified
  profile is deliberately not gated on qualification, as [`BB Workers`](bb-workers.md#spawn-in-an-isolated-worktree)
  states. Choosing a qualified profile, and not copying
  `AUTHORING_QUALIFIED_PROFILES` into a handoff or this policy, is orchestrator
  judgment backed by the review controls; inspect that authority and query the
  execution machine as the workflow requires. The inbound qualification refusal
  named above is the separate enforced path.
- **JUDGMENT** Treat quota as live provider state, never as a carried assertion. Establish it
  from the latest execution on the selected profile and inspect `output` and
  `log` for a provider quota or permission refusal before rerouting.
- **JUDGMENT** Key each model-evaluation row by the executed
  `(provider, model, reasoning_level)` triple read from the execution event, not
  requested argv or a declared default. The artifact definition and source are
  owned by
  [`bb-plugins/exec-tracking/README.md`](../../bb-plugins/exec-tracking/README.md).
  **IMPLEMENTATION GAP** Store the bb version observed for that execution with
  the row so comparisons across CLI versions remain interpretable.
  No tested checker exists for this today. The command removed from here
  parsed JSONL, diffed a snapshot and invented record identity, and it
  misclassified real rows; see
  [GH-771](https://github.com/pixexid/llm-collab/issues/771).

This section states routing rules and checks. It intentionally asserts no
current bb version, qualified-set membership, model availability, or quota
state.

## bb-update procedure

The exact pin is a safety control. Do not loosen it to make an unobserved bb
release pass.

1. **JUDGMENT** Stop new BB lane starts, and compare the installed version with
   `PINNED_BB_VERSION`. If the probe itself fails, repair the probe before using
   any quiet result. **ENFORCED** on the spawn path only: a mismatch refuses the
   spawn as `bb_version_mismatch` (`REFUSAL_VERSION_MISMATCH` in
   `llm_collab/bb_client.py`, reached through `bin/bb_spawn.py`). The
   `orchestrator_watch.py heartbeat` event and the `session_gate.py` line are
   **alert-only** — the gate prints `SESSION SETUP INCOMPLETE` and still returns
   0 — so by the definition above they report the violation while leaving it
   possible, and they are not the control.
2. **JUDGMENT** Read the four load-bearing CLI properties named at the top of
   `llm_collab/bb_client.py`. Re-observe **all four** against the live installed
   CLI before changing the pin; prior observations and release notes are not
   substitutes.
3. **JUDGMENT** Re-record `tests/fixtures/bb/settings_version.json` from the live configured
   CLI's `settings version --json` output. Do not hand-edit a file whose purpose
   is to preserve a recording.
4. **IMPLEMENTATION GAP** Inspect every version-mismatch test before moving the pin. A wrong-version
   literal must not equal the new pin; otherwise the test becomes a match while
   claiming to prove refusal.
   No tested checker exists for this today, and the command removed from here
   was a different shape from the evaluation-log ones: it read
   `PINNED_BB_VERSION` and ran unbounded `rg` literal searches over `tests`, so
   it flagged **any** test containing the pin string — including a legitimate
   assertion that the pin equals that value — while enumerating without a
   budget. Closing this gap means distinguishing a wrong-version literal from a
   correct reference to the pin, which is separate work from the evaluation-row
   recorder; see [GH-771](https://github.com/pixexid/llm-collab/issues/771).
5. **JUDGMENT** Sweep `AGENTS.md`, `docs/`, fixtures, and tests for commands whose semantics
   changed and version-stamped claims that must be re-verified. Update or record
   a disposition for each affected claim; never carry a live observation
   forward as an assertion about the new release.
6. **JUDGMENT** Author the pin, fixture, test, and necessary documentation changes in the
   orchestrator thread. This change cannot be delegated through the version gate
   it unblocks. Use the normal one-writer and PR workflow, including the required
   verification and first automatic review; this is not an exception to those
   gates.
7. **JUDGMENT** Reload `exec-tracking` through the project's configured
   executable, never a bare `bb`, or a wrapper project reloads the PATH
   installation instead of the instance its spawns use, leaving the intended
   plugin stale while the proof appears to run. Then launch one controlled probe
   and inspect the new recorded row in the configured project's executed-triple
   artifact. A running plugin does not follow a changed checkout, and a running
   status alone does not prove recording. No command here distinguishes a
   reloaded plugin revision from a stale running plugin, so this step is
   **JUDGMENT** by design.
8. **IMPLEMENTATION GAP** Record the observed bb version beside every evaluation row created during the
   update or its qualification probes.
   No tested checker exists for this today. The command removed from here
   parsed JSONL, diffed a snapshot and invented record identity, and it
   misclassified real rows; see
   [GH-771](https://github.com/pixexid/llm-collab/issues/771).

BB 0.37.0 observations for this procedure:

- Keep the `editMessages` experiment **off** for supervisor and orchestrator
  authority threads. Message rewind retains workspace changes but cannot rewind
  role generations, leases, watcher ownership, or durable decisions; use
  `bb thread compact` for context pressure instead.
- `sharedSkillRoots` can inject one physical user/project skill collection into
  providers as read-only skills. Treat those roots as discovery, not another
  editable contract copy; canonical repository guidance still lives here.
- The waiting-state and background-task fixes do not restore orchestrator
  polling. Direct terminal reports and event-worthy silent pointers remain the
  push authority defined by the standard watcher set.

The current pin is read from source, the installed version is read live, the
qualified set is read from its code authority, model availability is queried on
the execution machine, quota is established by a live execution result, and the
executed triple is read from its execution event. None is inferred from this
document.
