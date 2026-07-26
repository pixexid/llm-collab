# Commit, Push, And PR Workflow

## Goal

No lane is PR-ready until local validation and required metadata are complete.

## Hard rules

- never push directly to `main`; only the release-gate role merges to `main`
- the implementer role may create/commit on its assigned task branch and, when
  granted git/PR authority, push that branch and open a PR for its own lane —
  but only within the assigned worktree and under the safeguards below. Any
  worker (including Claude) may hold this authority per the role model in
  `task-intake-and-delegation.md`; it is not reserved to one named agent.
- the merge/release gate stays with the queue-owner/release-gate role (Codex by
  default in Amiga, a tooling constraint): independent review, merge-state
  inspection, and the merge itself are not performed by the implementer on their
  own lane
- do not open PRs without linked tracking context (issue/task)
- require full local verification for the affected surface before PR
- commit only on the assigned worktree branch; verify
  `git branch --show-current` before each commit; out-of-scope work becomes a
  separate task/branch/PR so no shared repo/branch is left dirty

## Suggested branch layers

- worker branch: implementation
- review branch: integration + preview gate
- main: merge target

## Review-branch gate

Before PR creation, run project-required verification on the review branch.
Run browser/smoke checks only when the lane touches browser-relevant behavior.
Use the project's primary browser path first, and run fallback browser tooling only if the primary path fails.
Do not create the review branch from a worker lane until the worker handoff acceptance gate has already passed, including branch verification, checkpoint-commit verification, and `git status --short --untracked-files=all`.
For Amiga, this means invoking the repo-local `amiga-pre-pr-review` skill before
commit, push, or PR.

Before committing or opening a PR, an independent reviewer must manually review
the final branch diff against the target branch or merge base. The reviewer is
not the implementer. Treat this as the primary code-review gate:

- review against the merge base or target branch, not just the last edited file
- check correctness, regressions, missing verification, contract drift, and
  workflow/process consistency
- classify findings as blocker, follow-up, or note
- fix all blockers before commit when the fix is local and bounded
- rerun the affected verification after fixes
- only then commit, push, and open the PR

This manual branch-diff review is the repeated review loop. GitHub Codex PR
review is an external safety net, not the mechanism that should discover routine
issues for the first time.

The standard Amiga mechanism is collab/doorbell review: the implementer sends
the final branch, base ref or merge base, final head SHA, scope, and verification
evidence in the linked chat; the reviewer returns findings in the same durable
chat. Codex normally reviews Claude-authored lanes and opens the PR after a
clean result. Claude, Gemini, or another independent reviewer reviews
Codex-authored lanes; Codex may still run the PR opener, but the review result
and notes must come from the independent reviewer. If Codex is unavailable,
Claude may use the Codex MCP/review surface as a fallback, but the review
artifact must still be recorded in llm-collab with reviewer, implementer, base
or merge base, final head SHA, result, mechanism/source, and re-review
disposition. The Codex app `/code review` UI is operator-facing and should not
be documented as an agent-callable requirement.

For Amiga, `pnpm pr:open` is the mechanical bypass guard for this workflow. It
requires `--review-result` and `--review-notes` for PR creation/editing. Allowed
results are `clean`, `clean-after-fixes`, `blocked`, `skipped (docs-only)`, and
`skipped (non-runtime)`; `blocked` cannot open a ready PR. Skip values require a
clear rationale in the notes. The opener rejects self-review notes and rejects
notes whose `Head SHA:` does not match the current branch head, so a stale clean
review cannot cover later commits.

### Bounded amendment review and convergence

One independent cold full-diff review by a context-isolated reviewer is
mandatory before the initial PR-ready head. Implementer/reviewer identity
separation, fresh-context attestation, full changed-file coverage, and exact
live-head SHA binding remain mandatory.

Batch related findings locally into one reviewed amendment instead of producing
one pushed head per micro-fix. When an amendment stays inside both the accepted
contract and its changed-file set, the same independent reviewer may inspect the
batched delta, the affected invariants, and the resulting full-diff coherence at
the new exact head. The resulting attestation must still cover the complete
base-to-head diff at that exact head; a delta-only review is never sufficient.

Create a new cold context-isolated reviewer for the complete amended diff when
the amendment touches payments, auth, permissions, schema or migrations,
irreversible writes, a new product flow, or any file outside the accepted
contract's changed-file set. These boundaries override the same-reviewer
allowance.

Apply a convergence circuit breaker per finding family:

- A finding belongs to the same family when it is anchored to the same file or
  concerns the same named invariant/mechanism across files.
- When a second finding round lands in one family, the queue-owner/release-gate
  role names the family and marks it hot. One already-drafted round-two
  amendment may finish.
- Before any third same-family amended head, record exactly one durable
  disposition in the task and, once a PR exists, in a PR comment:
  `contract-clarified`, `descope`, `split`, `backend-first`, or
  `risk-accepted-followup`.
- Only `contract-clarified` permits continued work in the same lane, and it
  requires updating the task/spec with the corrected invariant before the third
  head. `contract-clarified` may be used at most once per family per PR; a
  second same-family disposition must be one of the terminal values.
- Same-file anchoring counts mechanically: two finding rounds whose findings
  touch the same file are the same family regardless of which named invariants
  they cite. Orchestrator judgment applies only to grouping cross-file
  invariant findings.

Hard cycle cap, independent of family counting:

- A review-fix cycle is one finding round plus its amendment, regardless of
  reviewer freshness: same-reviewer re-reviews under the bounded amendment
  allowance consume cycles exactly like fresh cold reviews.
- The cycle counter is per task/lane, not per PR: it starts at the initial
  cold review — including the pre-PR collab/doorbell review loop — and carries
  into the PR once one exists. Opening the PR never resets the count.
- After the initial cold review, at most 2 review-fix cycles are permitted per
  lane; 3 when the contract scope includes payments, auth, permissions,
  schema/migrations, or irreversible writes.
- Docs-only lanes whose no-consumer scan proves zero runtime consumers always
  cap at 2 cycles: residual prose ambiguity in an unconsumed document is a
  follow-up issue, never another cycle.
- At the cap, inspect the exact current head. Only when actionable findings
  remain open at the capped head is exactly one terminal action required
  before any further amendment: merge at the current head with
  `risk-accepted-followup` (open findings move to a new issue), `descope`,
  `split`, `backend-first`, or a durable operator escalation packet. A capped
  head with zero open actionable findings and a clean exact-head re-review
  follows the normal merge gate with no convergence-disposition label.
  "No further amendment" bars content changes only; the publication steps the
  chosen disposition itself requires — pushing the already-reviewed head,
  opening its PR, and merging — remain permitted, so a lane that caps during
  the pre-PR loop can still land via `risk-accepted-followup`. Starting another
  review cycle past the cap is a process violation.
- A cap disposition never waives the PR Review Wait Gate. The cap bars another
  fix cycle, not waiting: the capped exact head must still pass the complete
  gate below, including its exact-head signal model and post-clean guard,
  before merge.
- Reaching the applicable cap requires an operator-visible escalation message
  recorded independently, whether or not open findings require a terminal
  disposition. When open findings do require a terminal disposition, record
  the escalation alongside it. Spending more than 2 hours of wall-clock time
  in the review-fix state requires escalation before the next cycle; a lane
  found past its cap is a process violation that must also be escalated.

When a project supports structured review notes, the disposition may be
recorded as the optional line `Convergence-disposition: <value>` and must use
exactly one of the five values above.

One final exact-head full-diff gate is mandatory before merge, and actionable
automated-review findings on the current head remain blocking. After a pushed
amendment, stale review-attestation CI is an expected transitional state rather
than evidence that product verification failed. Refresh the PR body only after
the amended head passes its required review.

There is no automatic GitHub Codex review flow to rely on: automatic review is off
account-wide. A review exists because someone requested it. Consume any
review/comment/reaction that appears, and see the policy below for when requesting one
is mandatory.

## PR requirements

Include:

- linked issue/task
- verification summary
- risk notes
- docs-sync confirmation when behavior contracts changed
- for work that must leave its referenced issue open, use a neutral reference
  such as `Related #123`, `Related to #123`, or the full issue URL. Do not
  place any GitHub closing keyword (`close`, `closes`, `closed`, `fix`,
  `fixes`, `fixed`, `resolve`, `resolves`, or `resolved`) immediately before
  the issue reference anywhere in the PR body or a commit message, even inside
  negated prose. Issue references include ordinary `#123` links, full issue
  URLs, and any project-local autolink pattern that GitHub creates reference
  events for. Observed incidents show that negated non-resolution wording can
  still place a closing keyword next to an issue reference and auto-close that
  issue when the PR merges. For portable workflow docs, do not use
  repository-specific autolinks such as `GH-123` as linked-issue examples
  unless that project's local guidance establishes the autolink.

## PR Review Wait Gate

Do not merge a PR from green CI alone. A merge is allowed only after the
orchestrator has inspected:

- GitHub Actions checks on the latest head SHA
- `mergeStateStatus`
- all of [the reviewed artifact set](#reviewed-artifact-set)
- any requested changes or review replies after follow-up commits

Do not idle on review while `mergeStateStatus` is dirty. A dirty merge state is
an active blocker: refresh the branch against the target base, resolve conflicts,
rerun verification, push, and then request/inspect review again.

### GitHub Codex review policy

> **Reviews are MANUAL ONLY (operator decision, 2026-07-25).** Automatic review is
> off account-wide. Nothing arrives unless someone asks for it, so the question is
> no longer "how long do we wait" but "must this change be reviewed at all". That
> is decided by the Tier A/B/C rule in
> [`AGENTS.md` → Requesting Code Review](../../AGENTS.md#requesting-code-review-all-workers-every-repository),
> which is the **only** place that defines it. This copy previously restated the tiers
> and went stale the moment Tier A was narrowed — the restatement still carried the
> superseded meaning in which "untrusted" covered our own workspace. Pointing is the
> rule this repository sets for exactly that reason.
>
> What matters here: **Tier A must request on the candidate final head**, failing to
> request is itself a gate violation, **Tier B/C do not wait** when nothing was
> requested, and any finding that does arrive is adjudicated in writing at every tier.
>
> "Request once" means **one initial request per candidate final head**, not once
> per PR: any amendment stales the review and requires a new request. The limit
> governs *initial* requests only — the single request-anchored re-trigger below is
> an explicit exemption, and is the only recovery for a silently dropped request.
> Read as absolute it leaves a Tier A worker choosing between violating it and
> having no recovery at all.

Use `local_required_github_codex_opportunistic` as the default queue-runner
policy:

- the orchestrator's local review and required project gates are mandatory
- GitHub Codex review/comments are consumed when they appear; under manual-only
  they appear because they were requested
- a clean `chatgpt-codex-connector` review/comment that explicitly covers the
  exact current OID is terminal for that head
- a connector-authored `+1` (`thumbs-up`) **on the exact manual-review request
  comment** is terminal CLEAN, after pickup, while the PR head still equals the SHA
  that request named. Verify all six: the actor is the connector, the reaction is
  on that request comment, the requested SHA, the current head, that this request is
  the latest for this head, and that it has not been edited since the reaction was
  left. Any head change voids it and requires a new request. An ambiguous or removed reaction is
  non-terminal, and a bare `eyes` reaction or the request comment itself is never a
  verdict — `eyes` means accepted and in progress
- the meaning of `+1` does **not** vary by tier. Tier A takes its strength from the
  mandatory final-head request, independent exact-head P0–P2 review, mutation and
  verification evidence, and settle plus adjudication. Requiring a posted text
  review for Tier A would deadlock whenever the connector's clean protocol is
  reaction-only, and the request plus a connector-authored `+1` is already a durable
  GitHub artifact
- these are the only two exact-head terminal signal models; no other review,
  comment, or reaction artifact is terminal
- **a connector review body that lists no findings is not a clean verdict.** The
  connector posts its findings as inline review threads, and the review body can be
  boilerplate — a heading, the reviewed commit, and a collapsed "About Codex"
  section — while unresolved P1 threads sit on that head. An empty body with live
  threads reads exactly like a pass (llm-collab#317 at `87e8e47`, 2026-07-26: body
  listed nothing, six unresolved threads including three P1s, one of which made the
  generated command unrunnable on macOS). `reviewThreads`, not the body, is the
  finding list
- **bind an exact-head finding through its initiating review commit, and adjudicate
  every thread, resolved or not, regardless of `isOutdated`.** A worker who reads only
  this heading is the one who clicks Resolve, records nothing, and then omits the thread
  from the gate — so the heading states the rule rather than the narrower version of it.
  Two distinct questions:
  - *is this finding about the current head?* Only if the thread's **initiating
    review commit OID** equals the current head OID. Read it from
    `comments.nodes[0].pullRequestReview.commit.oid`, falling back to
    `comments.nodes[0].originalCommit.oid` for a thread with no backing review.
    **Never `comments.nodes[0].commit.oid`** — that field is mutable and GitHub
    advances it to the current head for a thread that is still non-outdated, so it
    reports every live stale thread as a current-head finding.
  - *is this finding still open?* Only GitHub resolution, or a written disposition
    **that identifies the thread**, closes it. **Resolution is not adjudication.**
    `AGENTS.md` requires every arriving finding to be adjudicated in writing at every
    tier. A checklist phrased over *unresolved* threads could not see a thread someone
    clicked Resolve on with nothing recorded, which is the one way to lose a finding
    silently — so the merge checklist enumerates **every** thread, resolved or not. A
    resolved thread still owes a thread-linked written outcome; resolution closes it
    for GitHub, the writing is what discharges it. Identifying means the
    disposition contains the thread's node ID or its `#discussion_r...` comment URL.
    Identification is **necessary and not sufficient**: a human must also validate
    that the disposition came from someone authorised on *this* pull request, that it
    concerns this PR rather than merely mentioning the thread from elsewhere, and that
    it states an actual closing outcome. A comment reading "Still unresolved: `<url>`"
    identifies the thread perfectly and closes nothing. Closure is never derived
    mechanically from a body — the identifier exists so a human's decision can be
    audited afterwards, not so a consumer can skip the human. Grouped prose naming
    findings by title closes nothing either, because there is no way to check which
    thread it answered, including for its author. **A push is not an adjudication**,
    and neither is a summary that does not say which thread it disposes.
  `isOutdated` answers neither. It is diff-position metadata — whether the thread
  still maps onto the current diff — and using it as the exclusion criterion is
  wrong in both directions. llm-collab#313 disproves it directly: at `9822524`,
  twelve unresolved non-outdated threads initiated at `a54e33f` or `82ae7e9`, so
  the rule counts twelve stale threads as current-head findings; five unresolved
  outdated threads, which the rule drops silently though nobody ever answered
  them; and **zero** threads actually initiated at the current head. Those same
  twelve report `comments.nodes[0].commit.oid` as `9822524`, which is why the
  field matters as much as the rule
### The reviewed artifact set

<a id="reviewed-artifact-set"></a>

**Every instruction in this repository that says to read, re-read or inspect review state
means exactly these five, and this is the only place the list is written:**

1. top-level PR comments
2. review bodies
3. review threads
4. inline review comments
5. reactions

Referenced, never restated. Nine sites used to enumerate this and they had drifted into at
least five different versions -- some omitting comments, which is where a re-review request
lives, and some omitting reactions, which is where a terminal `+1` lives. Each omission was
a working path to merging on a superseded signal, and each was fixed one site at a time
until it became clear the list itself was the defect. A paraphrase of this list anywhere
else is a second source that goes stale the moment this one moves.

- a head-named clean connector verdict is not merge-immediate. Hold an
  approximately five-minute post-clean settle, then re-read [the reviewed artifact set](#reviewed-artifact-set) in full before
  merge, because the connector can emit multiple reviews for the same head — and because
  a same-head re-review request posted during the settle lives in a comment and
  supersedes the artifact being settled
- **a reaction counts only on the latest, unedited request artifact.** GitHub keeps
  reactions across an edit, so a request comment edited to swap an old SHA for the
  current one still carries the `+1` the connector left for the *old* head, and all
  four checks then pass on a review that never happened. If the request artifact has
  been edited since the reaction was left, or is not the most recent request for this
  head, the reaction is not terminal. What to do next turns on the **re-trigger
  budget**, not on whether a valid artifact happens to exist right now. If the single
  request-anchored re-trigger for this head has not been used, use it. If it has been
  used — whether it is still valid, or was since edited, deleted or otherwise made
  invalid — the budget is spent and there is no further request to issue: continue to
  the exact-head operator disposition. Reading this as "issue one whenever no valid
  latest request exists" produces a third same-head request every time an artifact is
  tampered with, and the single re-trigger is the only exempted recovery
- when a re-review was explicitly requested, that re-review supersedes older
  same-head clean artifacts **and older same-head reactions**, not the verdict path
  alone. Only the explicit
  re-review verdict can satisfy that path, and it receives the same
  approximately five-minute post-clean settle and full re-read
- report the exact verdict, or the connector-authored `+1` on the manual-review
  request comment with its timestamps and the SHA that request named, and confirm
  that no later push occurred
- any push creates a new head and invalidates every prior verdict and reaction
  lifecycle
- **there is no silence fallback for an unrequested review.** Under manual-only,
  waiting out a clock for a review nobody asked for is a path that always ends in
  silence. A Tier A head with no request has exactly one remedy: **issue the
  request.** Failing to request is itself a gate violation, so an unrequested head
  is not a head awaiting a waiver — it is a head whose worker has not yet done the
  mandatory step, and no waiver is available for skipping it. Only once the request
  exists does the precedence flow below apply: the initial request, its single
  request-anchored re-trigger, and the two request-anchored expiries, at the end of
  which an operator waiver is the defined outcome. A Tier B/C head with no request
  does not wait at all. At no tier is quiet a signal: never merge on silence
- **no elapsed time is ever a terminal signal.** There is no resettable settle
  that ripens a head for merge. Waiting is what a Tier A head does while a
  requested review is outstanding; it is not a way to acquire the signal
- after every review-fix push, evaluate the new exact head under the same rule:
  a clean exact-head verdict or head-attributable connector `+1` is terminal.
  If neither exists, **any head carrying an outstanding request** is not
  merge-eligible no matter how long it has been quiet — the request, not the tier,
  is what makes it pending, so an amended Tier B/C head whose review was requested
  waits exactly as a Tier A head does. Heartbeat inspections observe the wait and
  merge nothing
- neither a bot verdict nor a reaction waives required CI, mergeability, the
  independent exact-head review, or full inspection of [the reviewed artifact set](#reviewed-artifact-set)

**All three former no-terminal-artifact fallback variants are deleted, not
shortened.** Each one measured how long to wait for a review that manual-only
review never sends unrequested, so each clock always expired -- and its expiry
handed a Tier A worker a path to merge on nothing. What remains is a
classification of non-signals, with no clock attached to any of them:

- **No explicit review request.** At Tier A this is a **gate violation to fix,
  not a delay to wait out**: request the review. At Tier B/C there is nothing to
  wait for and nothing to merge on.
- **Eyes-only current-head artifact.** A current-head `eyes` reaction is not a
  terminal signal and never becomes one. It neither blocks nor ripens anything.
- **Prior-head artifacts only.** Any push creates a new head and invalidates every
  prior verdict and reaction lifecycle; a prior-head `Codex Review:` body or
  `eyes`/`+1` reaction is not head-attributable for the current head and is
  ignored for terminal-signal purposes.

#### Explicit requested-review precedence

An explicitly requested review remains pending until its roughly 30–35-minute
clock expires unless one of the two exact-head terminal signals arrives; it
never ages into a merge-eligible state, because no silence fallback exists to
age into. Anchor each clock to the corresponding explicit request artifact's
GitHub `created_at`, never to the latest push or the time the head became
reviewable. That clock decides only when to re-trigger once and when to
escalate -- never when to merge. A current-head `eyes` reaction alone is
non-terminal: it does not exit requested-review precedence or reset that
request's clock.

When the initial request's clock expires without a terminal signal, treat that
request as silently dropped and issue exactly one re-trigger. **The re-trigger
repeats the full request shape — focus and the exact head SHA — never a bare
`@codex review`:** a reaction is terminal only when it sits on a comment that
named the current head, so a SHA-less retry cannot produce a usable signal even
when the connector answers it.
The re-trigger is the sole automatic retry and starts its own 30–35-minute clock
at its GitHub `created_at`. If that clock also expires without a terminal
signal, do not re-trigger again. The PR remains unmergeable until a
human/operator records an explicit disposition bound to the exact current
head. The disposition must state exactly one of these outcomes: merge of that
exact head is authorized despite the absent connector terminal signal; or that
exact head must not merge and remains blocked or is closed. An ambiguous note,
a disposition not bound to the current head, or an older-head disposition does
not lift the merge block. Any later push invalidates the disposition and
restarts exact-head evaluation.

An exact-current-head merge authorization lifts only the missing
connector-signal subgate caused by the silently dropped requested review. It is
not a connector terminal signal, is not a third automated terminal-signal
model, and creates no fallback path. It does not waive independent exact-head
review, green required checks, mergeability, a full re-read of
[the reviewed artifact set](#reviewed-artifact-set), unresolved-feedback handling, or
project/operator auto-merge authority. If a connector clean signal later
arrives, its signal-specific settle and reread still apply normally; the
operator authorization does not masquerade as that signal or inherit its
handling. Report and escalate the stuck review at each expiry. The existing
PR-wait heartbeat observes these clocks; neither the re-trigger nor the
operator authorization is a new terminal signal or watcher mechanism. The
longer request-anchored timer exists because a dropped request is
indistinguishable from a review that is still processing, unlike the
absent-request variant, where there is nothing to drop.

If the PR is waiting only for remote checks or remote review state, keep it open
and create or update a Codex heartbeat attached to the current thread with a
6-minute cadence. Each heartbeat must re-check the PR checks, review state, review
threads/comments, connector reactions, and merge state. "Automatic" is dropped
deliberately: reactions arrive because a review was requested, and a heartbeat that
waits for an automatic one waits forever.

PR-wait heartbeats are a safety-fuse, not the primary routing path. When a
heartbeat or queue owner finds actionable PR feedback that needs the implementer
to change their branch, it must send a durable mailbox packet and inspect the
`deliver.py` result. If `autobridge_ready: true`, the current Phase 1 route is
session autobridge and no AX doorbell was requested. If
`ax_doorbell_required: true`, first prove the native composer is empty, then
ring the implementer once with AX even if busy. A non-empty, unreadable,
unprovable, or `AXValue`-opaque composer means hold and enter recovery.
`VERIFIED` exit 0 confirms delivery; `QUEUED (UNCONFIRMED)` exit 0 preserves the
mailbox/blocker follow-up but is not exact-thread delivery proof and must not be
re-rung. The idle input gate applies only if attended screenshot/keyboard
Computer Use is needed as fallback. Do not silently wait for the next heartbeat
or depend on the operator to notice the PR comment.

When the operator has authorized the merge path for the PR or PR class, the
heartbeat may complete the wait after it verifies the exact current head has
green required checks, the PR is mergeable with clean `mergeStateStatus`, the
independent exact-head review is clean, and the full current comment, review,
inline-comment, and thread payload has no actionable finding. Treat the GitHub
Codex review signal as clean when either:

- the latest top-level `chatgpt-codex-connector` review/comment explicitly
  covers the exact current OID and reports no actionable or major issues, or
- a connector-authored `+1` (`thumbs-up`) sits on **the exact manual-review request
  comment**, the PR head still equals the SHA that request named, and no subsequent
  push occurred. Verify all six: the actor, that request comment, the requested SHA,
  the current head, that this request is the **latest** one for this head, and that it
  has **not been edited** since the reaction was left. The last two are not optional
  refinements: GitHub preserves reactions across an edit, so a request edited to swap
  an old SHA for the current one carries a `+1` for a review of the *old* head and the
  first four checks all pass. A `+1` attributable only by timestamp, or sitting on any
  other artifact, is **not** terminal. That reaction is terminal for the bot wait on that
  head when the required gates above remain clean, and it receives the same
  approximately five-minute post-clean settle and full re-read as a text verdict.

The re-read that follows a reaction covers **top-level PR comments as well as
reviews, threads and reactions**, and revalidates all six reaction conditions. A
re-review request posted during the settle becomes the latest request for this head
and supersedes the older request's `+1`, so a re-read that skipped comments could
merge on a reaction that no longer passes the latest-request check.

For the clean-verdict path, do not merge immediately after the first
head-named clean artifact. Observe the approximately five-minute post-clean
settle and then re-read [the reviewed artifact set](#reviewed-artifact-set) in full. When an
explicit re-review was requested for the same head, ignore older same-head
clean artifacts for this path and apply the same settle and re-read to the
explicit re-review verdict.

If neither a terminal GitHub Codex verdict nor a head-attributable connector
`+1` exists for the exact current head, a Tier A head does not merge — there is
no elapsed-time substitute. Fix the missing request, or follow
[Explicit requested-review precedence](#explicit-requested-review-precedence)
to the operator disposition. A Tier B/C head **for which no review was requested**
needs no connector signal at all. A requested review is pending whatever its tier:
once a request exists, precedence is tier-agnostic, so a voluntarily or
operator-requested Tier B/C review is outstanding until a terminal signal or an
exact-head operator disposition -- exactly as the fixture matrix records it.
Proceed only when all of these are true:

- the independent exact-head review found no actionable issues
- required checks are green on the latest head
- the PR is mergeable and `mergeStateStatus` is clean
- **every arriving finding has a thread-linked written outcome, whatever head it was
  initiated on and whatever its current resolution state.** The origin rule above
  decides which findings are *about* this head; it does not narrow this checklist.
  Two ways to lose one, and this bullet closes both: a prior-head thread nobody
  answered is unadjudicated rather than closed, and a thread someone clicked Resolve
  on without recording anything is no longer *unresolved* — so a checklist phrased
  over unresolved threads never looks at it again. Enumerate every thread, resolved
  or not, and require the written outcome for each
- [the reviewed artifact set](#reviewed-artifact-set) contains no unresolved actionable feedback, whatever head it was raised
  on — prose feedback is dropped by a "current head" reading exactly as a thread is
- **an actionable finding that arrived with no thread carries a written outcome too.**
  A review body or a top-level comment has no node ID to link, so the
  thread-identification rule cannot reach it — and "no longer unresolved" is not a
  standard prose can meet, because nobody resolves a comment. Quote or link the comment
  and state the outcome, in the same place the thread dispositions go. Without this a
  finding is discharged by whoever pushes next, which is the rule this section opens by
  rejecting
- the project/operator has authorized auto-merge for this PR or queue class

Read [the reviewed artifact set](#reviewed-artifact-set) directly. Do not infer the current
result from stale inline review-thread objects alone. The watcher must report the
exact current-head verdict, or the connector-authored `+1` on the manual-review
request comment together with the SHA that request named, with timestamps, and
confirm that no later push occurred. Where both request-anchored clocks have expired,
an exact-head operator disposition has lifted the missing-signal subgate, **and no
terminal connector signal has arrived since**, the watcher reports **that
disposition** instead, naming the head it was given for --
otherwise the one path the precedence section authorises for a silently dropped
review cannot be completed, because a connector artifact is precisely what does not
exist there. A signal that arrives late still wins, and that is an action rather than
a principle: **after selecting the disposition path and before reporting it or
merging, re-read [the reviewed artifact set](#reviewed-artifact-set) once more.** Choosing the
branch is not the same as checking it, and without that read a verdict landing in
between is completed straight past — skipping the settle and full re-read it is owed.
If the read finds one, abandon the disposition branch and report the signal instead. An operator disposition is
not a connector terminal signal and never substitutes for one where a signal did
arrive. Either terminal signal
stops the heartbeat from waiting for further artifacts;
it does not waive post-signal handling. For a head-named clean verdict **and for a
request-comment `+1` alike**, the approximately five-minute post-clean settle and a full
re-read of [the reviewed artifact set](#reviewed-artifact-set) remain mandatory before merge. If review feedback lands, fix or respond
to it, push the update, rerun the manual branch-diff review and required
local/CI checks, then evaluate the new exact head from scratch before
continuing toward merge.

If the wait cannot self-progress because checks stalled, review state is
ambiguous, or the implementer has not acknowledged a routed review-fix request,
the heartbeat must escalate by doorbell with the exact blocker and next action.
Delete or rewrite any PR-wait heartbeat that misses this escalation path.


Keep the heartbeat active until rerun checks, merge state, and current PR
comments/reviews are clean. Delete the PR-wait heartbeat immediately after the
merge, then continue normal post-merge cleanup in the same Codex thread.

## Autonomous Queue Runner State

For unattended or standing-instruction loops, record the current loop mode in:

```bash
python3 bin/autonomous_loop.py start --project <project_id> --agent codex --mode next_lane
```

The state file lives at:

```text
{project_state_root}/<project_id>/autonomous-loop.json
```

Use it to distinguish these states:

- `next_lane`: recover inbox/queues/PRs and activate the next safe lane
- `worker_wait`: a worker is active; check inbox and bridge status without
  interrupting visible work
- `acceptance`: worker checkpoint is ready for dirty-worktree/task-contract
  acceptance
- `pr_wait`: PR is open; re-check checks/reviews/comments/merge state
- `fix_loop`: blocker exists but can be fixed by Codex or re-delegation
- `post_merge`: merge completed; run cleanup and return to queue recovery
- `queue_empty`: no remaining lane after validation

There should be one queue-runner heartbeat per project loop. Task-specific
heartbeats are subordinate: create them only for a concrete wait such as Claude
Desktop, a worker handoff, or a PR, and delete/update them when the main
queue-runner state moves. Do not leave a stale task heartbeat competing with the
persistent queue runner.
## Post-merge

After merge:

1. fast-forward local `main`
2. run targeted post-merge smoke only when the merge is browser-relevant
3. evaluate the exact merge SHA through the project's configured release
   authority
4. only after terminal success or an explicit honest non-success disposition,
   move the local task from `review` to `done`
5. after the `review → done` transition succeeds, perform any required project
   queue refresh for lane-ordering or state changes
6. only then run the branch/worktree cleanup gate in applying mode

`PENDING`, `MISSING`, `FAILURE`, or `CANCELLED` stops this sequence: preserve
the task in `review` and preserve the implementation lane. Do not apply cleanup
or advance the queue runner beyond `post_merge`.

For a production-affecting merge, the `review → done` transition waits for the
release-closure gate below ("Release closure does not end at merge"): terminal
deploy+smoke success for the exact merge SHA, or an explicit Codex disposition
on a non-success. Never mark done with the release outcome unknown or red. A
docs-only or otherwise non-production-impacting merge exits via an explicit
scope disposition (recorded as such); a skipped deploy is never called deploy
success.

Within the `review → done` command, the target-state task contract is validated
at stage `done` before the release evaluator and before activity/task/queue
mutation. For projects with `db.production_schema_guard: true` this blocks `none` or
unapproved `local-schema-only` classification and preserves every required
`shared-supabase-required` evidence field across `success`, `non-production`,
and `risk-accepted-followup` dispositions. Existing done history remains
grandfathered. Post-merge cleanup is verification/application only, never an
alternate transition path.

For `llm-collab` itself, the shared local checkout is part of the shipped
workflow. After a PR that changes workflow docs, scripts, gates, skills, agent
routing, or queue behavior merges, fast-forward the canonical local checkout
that future sessions read before starting or handing off more work. For Amiga,
that checkout is `/Users/pixexid/Projects/llm-collab`.

If tracked local dirt blocks the fast-forward, stash or move the tracked dirt
aside with an explicit note. Do not leave the shared checkout behind
`origin/main` just because GitHub is up to date. Project-private untracked files
may remain untracked; do not delete or commit them just to sync tracked workflow
files.

The cleanup gate is:

```bash
python3 bin/post_merge_cleanup.py \
  --project <project_id> \
  --apply \
  --remove-plain-dirs \
  --discard-disposable-dirty \
  --fail-on-blockers
```

Run it from `/Users/pixexid/Projects/llm-collab`. For Amiga this command scans
the app repo and `/Users/pixexid/Projects/amiga-worktrees`, not only branch refs
visible from `/Users/pixexid/Projects/amiga`. The queue runner must not clear
`post_merge`, return to `idle`, or activate the next lane until this command has
either:

- removed all safe stale worktrees, stale branches, and disposable plain
  directories; or
- reported every deferred dirty/active item with a concrete reason in the
  current thread or task notes.

Stay in the same Codex thread after merge/local cleanup by default. Do not send
a `codex -> codex` self-handoff or force a fresh `check inbox` thread unless the
operator explicitly asks for a new session/handoff or context safety requires a
thread boundary.

Workflow/process edits are first-class deliverables, not disposable local dirt.
If an orchestrator edits repo instructions, skills, workflow docs, queue
scripts, agent memory, or bridge/runtime instructions while fixing a process
failure, it must classify that diff before starting the next lane:

- own PR
- explicitly bundled into the current task PR
- intentionally abandoned or kept local with operator approval

Run `git status --short --branch --untracked-files=all` in each touched repo,
commit/push/open the PR for changes that should persist, and record any
intentional local remainder. Do not assume future merge cleanup will preserve
uncommitted workflow edits.

## Release closure does not end at merge (GH-1524)

A **production-affecting** merged PR is not a closed release until the **main
production deploy for the exact merge SHA** — including its post-deploy smoke —
reaches terminal success. Docs-only merges intentionally skip the heavy deploy
job in Amiga's `deploy.yml`; a skipped deploy is a no-op run, never
"deploy+smoke success", and such merges are outside this gate's scope.
The df55a282 incident proved the gap: a post-deploy smoke failure (run
29537490993) sat unnoticed for hours because nothing consumed the deploy
signal, and a later unrelated green deploy looked like cover.

The gate, enforced with `bin/deploy_release_watch.py`:

```bash
bin/deploy_release_watch.py --project amiga --merge-sha <full-merge-sha> [--wait]
```

The repo, base branch, workflow, and required job/smoke-step evidence come from
the project's `release_closure` object in `projects.json` (project boundary:
job/step names are project-specific and never live in shared `bin/`). A project
without that config fails closed with exit 64.

- **Exact-SHA correlation is absolute.** A deploy run for a different or
  earlier SHA never satisfies this merge's closure, no matter how green.
- **Only the automatic run counts**: the project's configured
  `release_closure.trigger_event` on its configured `default_branch_base`
  (Amiga: `push` on `main`). A same-SHA run under any different event or
  branch is non-authoritative and never satisfies — or supersedes — the
  configured automatic run's outcome.
- **Success = deploy AND post-deploy smoke terminal success** for that exact
  SHA, proven by POSITIVE evidence: every job named in the project's
  `release_closure.required_jobs` present and successful (a skipped required
  job = not a release) and every configured `required_smoke_steps` present and
  successful inside the configured `smoke_job`. All names come from the
  project's `projects.json` `release_closure` — no project inherits another's
  labels. Empty or partial run evidence fails closed.
- **`FAILURE` / `CANCELLED` / `MISSING` are each actionable**: the watcher
  sends ONE durable llm-collab packet plus ONE doorbell ring. A missing run is
  a distinct alarm, never silence and never a pass.
- **On any non-success the task is NOT done**: closure is blocked until Codex
  records a terminal disposition. Preserve the run id and logs
  (`gh run view <id> --log-failed`); **no blind retry or redeploy** as the
  reflex response.
- **Ownership:** Claude is the ongoing main-deploy watcher; Codex is the
  terminal task/release closer.

Do not idle the active thread just to wait for asynchronous deploy automation if local post-merge
work is already complete. Treat deploy as a later checkpoint unless it has actually failed or a new
production-impacting merge would stack on top of an unresolved deploy state.

For `llm-collab` itself, refreshing `main` after every merge is mandatory before
new coordination work starts from a persistent checkout. Workers and new
sessions must not keep using an old feature branch as the collaboration runtime
after its changes merge.

Use:

```bash
git fetch origin main
git switch main
git pull --ff-only origin main
git status --short --branch --untracked-files=all
```

Do not delete or commit project-private untracked files during this refresh.
Examples include `.secrets/`, local runtime state, generated worker memory
templates, and project-local config examples. They should remain local unless a
separate reviewed task explicitly promotes them into the open-source repo.

## Branch/worktree cleanup contract

Post-merge cleanup is required, not optional.

- remove merged `codex/review/*` branches (local and remote)
- remove stale worker branches only when their lane is verified complete
- remove stale worktrees only when their lane is verified complete
- keep only active worktrees and one intentional root parking branch (or `main`)

Do not treat `merged` as sufficient evidence that a worker branch/worktree is disposable.

Worker branches/worktrees are deletion candidates only when all of the following are true:

1. the related PR is merged or the related issue is closed
2. the related local task mirror has an exact `project_id` match for the
   cleanup command's `--project`; a missing, empty, null, or foreign project ID
   is not a task match
3. the related local task mirror is `done`, not `open`, `in_progress`, `blocked`, or `review`
4. the branch is not the active branch of any existing worktree
5. the worktree is clean enough to discard (`git status --short --untracked-files=all`)
6. the branch tip is merged into `main` or patch-equivalent to a merged commit on `main`
7. no active chat/task/brief still points to that branch/worktree as the implementation lane

If any one of those checks fails, defer cleanup.

Safe cleanup order:

1. fetch/prune refs (`git fetch --all --prune`)
2. split cleanup candidates into:
   - safe now: merged review branches and worker lanes whose task is `done`
   - defer: any branch/worktree still referenced by an active task/chat or still carrying non-disposable files
3. verify each candidate worktree is disposable (`git status --short --untracked-files=all`)
4. verify each candidate branch is merged or patch-equivalent to `main`
5. remove stale worktrees first (`git worktree remove [--force] <path>`)
6. prune stale worktree metadata (`git worktree prune`)
7. delete stale local branches (`git branch -d` or `-D` when explicitly safe)
8. delete stale remote branches (`git push origin --delete <branch...>`)

Do not keep merged branch clutter; clean branch lists are required for reliable lane selection.

The manual sequence above is the policy model. The executable gate above is the
required loop mechanism. If the gate reports `ok_to_clear_post_merge: false`,
the queue runner is still in `post_merge` or `fix_loop`, not `next_lane`.
