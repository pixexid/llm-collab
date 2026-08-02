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

## Lane contract (Tier A, before the first branch)

A Tier A lane does not cut its first branch until one page of contract exists in
the task or linked issue, using the template in
[`lane-contract.md`](lane-contract.md):

- **authority boundary** — the one component that owns the decision or state
  this lane changes (a canonical store, a sole writer, a named module), and what
  every other component may assume. A guarantee that needs exactly-once,
  atomicity, or ordering must name a boundary capable of providing it; if no
  existing component can, that is the lane's first finding, resolved on the
  page — not discovered at review cycle five.
- **commit point** — the single operation after which the change is durable and
  visible.
- **retry behavior** — what a retry may repeat, and what it must never repeat.
- **non-goals** — the guarantees this lane explicitly does not provide. They
  constrain requested scope but never excuse a regression introduced by the diff.

A lane that changes the contract itself adds one stated step to its contract: a
**doc-drift sweep** — grep `AGENTS.md` and `docs/` for the phrases the change
supersedes, and update or explicitly defer every hit in the same PR. #356 round
5 produced three findings from stale companion docs (a hand-typed-SHA template,
an old escalation route, a retired reviewer rule) that a ten-minute sweep
catches before the first request.

Reviews then verify the diff against that page: review requests name it ("review
the full diff against the lane contract in <issue|TASK-id> through these lenses: ..."),
and the per-finding disposition below routes every arriving finding as
contract-violating, out-of-contract, or contract-gap. The contract stays one
page: a lane that cannot state its boundary on one page is two lanes. Split it
at intake, where splitting is cheap — not at the convergence cap, where it
costs a PR generation. The #345 → #346 → #347 → #349/#350 → #351 family spent
three PR generations discovering its boundary; naming it up front is the whole
point of this gate.

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

This manual branch-diff review happens once before the initial PR-ready head.
The first automatic GitHub Codex pass is the PR's only external bot review;
later heads receive local exact-head verification.

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

Batch related findings locally into one amendment instead of producing one
pushed head per micro-fix. Verify the affected invariants and resulting
base-to-head coherence locally. Do not run another bot or independent model
review on the amended head.

### Per-finding disposition at arrival (defer-first)

Every arriving finding is classified in writing against the lane contract the
moment it arrives, before any fix work starts:

- **contract-violating** — the diff fails a guarantee the lane contract
  promised, or introduces a regression to an existing guarantee even when the
  lane contract omitted it. Block and fix; this consumes a review-fix cycle.
- **out-of-contract** — real, but pre-existing or a requested broadening beyond
  this diff. Adjudicate in writing, file a follow-up issue identifying the
  finding's thread, and continue; the lane still ships. A named non-goal does
  not excuse a regression introduced by the diff.
- **contract-gap** — the finding shows the shipped feature would be wrong or
  unsafe *as specified*, despite sitting outside the written contract. Amend
  the lane contract once (`contract-clarified`, at most one use per family per
  PR as below); thereafter the finding is contract-violating.

The classification is auditable: it names the lane-contract clause or
pre-existing/broadening boundary relied on, and a deferred finding that was in
fact contract-violating is a gate violation attributable to the classifier.
The lane owner and release-gate worker may accept a known, bounded risk by
recording it and preserving the follow-up. Escalate only when they cannot reach
or execute a safe decision without operator-only input.

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
  before any further amendment. The lane owner and release-gate worker must
  choose explicitly: accept each remaining contract violation as a named,
  bounded risk with a follow-up issue and merge that exact head; or descope,
  split, use `backend-first`, or close the lane. `merge-with-followups` remains the default for
  findings classified deferrable before the cap; it is never a silent default
  for a contract violation. A durable operator escalation packet remains the escape
  only when the lane owner and release-gate worker cannot resolve the trade-off
  without operator-only input. Under per-finding defer-first
  most findings never reach the cap: only contract-violating findings are
  still open here, so a cap disposition is the rare case where the lane
  contract itself could not be satisfied — which usually means the authority
  boundary was wrong, and the fix is a new contract, not another reorder. A capped
  head with zero open actionable findings, a completed first connector pass,
  and local exact-head verification follows the normal merge gate with no
  convergence-disposition label.
  "No further amendment" bars content changes only; the publication steps the
  chosen disposition itself requires — pushing the already-reviewed head,
  opening its PR, and merging — remain permitted, so a lane that caps during
  the pre-PR loop can still land via `risk-accepted-followup`. Starting another
  review cycle past the cap is a process violation.
- A cap disposition never waives the PR Review Wait Gate. The cap bars another
  fix cycle, not waiting: the capped exact head must still pass the complete
  gate below, including its exact-head signal model and post-clean guard,
  before merge.
- Reaching the applicable cap requires a durable, operator-visible disposition,
  but visibility is not an approval gate. **A lane also has a wall-clock budget:
  more than 4 hours in the review-fix state, or a third amended head, forces the
  lane owner and release-gate worker to decide, for the exact current SHA,
  merge-with-followups or close.** Any later push invalidates that decision.
  This budget ends review-fix loops; it is not a connector terminal signal and
  waives none of the exact-head merge gates. Escalate to the operator only when
  the workers cannot reach or execute either safe outcome without operator-only
  input.

When a project supports structured review notes, the disposition may be
recorded as the optional line `Convergence-disposition: <value>` and must use
exactly one of the five values above.

One final exact-head local full-diff gate is mandatory before merge. Automated-review
findings on the current head that were classified contract-violating remain
blocking until fixed or individually accepted under the bounded-risk cap
disposition above; out-of-contract findings follow the defer-first disposition
and do not block. After a pushed
amendment, a stale review attestation is an expected transitional state rather
than evidence that product verification failed. Refresh the PR body only after
the amended head passes its required local verification.

GitHub Codex review is configured to start when a PR opens or becomes ready.
Every PR waits for that first pass before merge. Consume everything in
[the reviewed artifact set](#reviewed-artifact-set); silence and elapsed time are
never a substitute for the bot's terminal result.

## Local verify gate

`bin/verify.py` is the **required local gate** — the suite is not run on PRs by CI.
Run it before pushing a review head or opening a PR. It is side-effect-free and
does not fetch, so **fetch origin/main first** (fetch-only, works on any branch) or
the diff-check merge-base is stale and verify fails closed:

```bash
git fetch origin main
python3.11 bin/verify.py
```

Use a fetch-only preflight here, not `bin/local_main_sync.py --apply`: that tool is
the *post-merge* persistent-checkout synchronizer and deliberately fails closed
(`active_branch`) on a feature branch carrying review commits.

It runs two gates and fails if either fails: `python -m unittest discover -s
tests` from the repo root (so the top-level `llm_collab/` package imports —
running discover from inside `tests/` silently drops ~345 `import llm_collab.*`
modules to import errors and shrinks the suite; it also strips runner-session
identity vars like `CLAUDE_CODE_SESSION_ID` so they cannot leak through
`os.environ` into subprocess tests) and `git diff --check` (whitespace errors and
leftover conflict markers). The exit code is nonzero if either gate fails.

`.github/workflows/verify.yml` is a **manual `workflow_dispatch` escape hatch**
only (dependency/environment drift, incident reproduction). It does not run on
PRs, so it costs no per-PR Actions minutes; a dispatched run is supplementary
evidence, never the merge gate.

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

There is no automatic PR CI, so the merge prerequisite is **local exact-head
verification**, not a green check. A merge is allowed only after the orchestrator
has inspected:

- **local `bin/verify.py` run on the exact head SHA** (origin/main synced first) —
  this replaces "required CI green" as the objective gate
- a manually dispatched `verify.yml` run, if one exists, as *supplementary*
  evidence only (never the sole gate)
- `mergeStateStatus`
- all of [the reviewed artifact set](#reviewed-artifact-set)
- any requested changes or review replies after follow-up commits

To poll a PR for these, `bin/pr_watch.py --repo <owner/name> --pr <N>` reports the
first change across the PR timeline, reactions, and check-runs (the connector
posts its verdict as a comment **and** a reaction, and any check-runs — e.g. a
manually dispatched verify — arrive separately; none reliably bump `updated_at`),
then exits with a JSON delta. It watches one PR
and exits on the first event, so re-arm it to catch the next event on the same PR.

Do not idle on review while `mergeStateStatus` is dirty. A dirty merge state is
an active blocker: refresh the branch against the target base, resolve conflicts,
rerun verification, push, then locally verify the amended head and inspect the
existing first-pass review record. Do not request a second bot pass.

### GitHub Codex review policy

> **One automatic bot pass is mandatory for every PR (operator decision,
> 2026-07-31).** Do not merge an open PR before that pass completes. The Tier
> A/B/C rule in
> [`AGENTS.md` → Requesting Code Review](../../AGENTS.md#requesting-code-review-all-workers-every-repository),
> decides only whether a missing automatic trigger also requires the one manual
> fallback request; it never waives the automatic first-pass gate.
>
> What matters here: wait for the first pass, adjudicate every finding, fix the
> serious defects, verify the amended exact head locally, and do not request a
> second bot pass.
>
> "One pass" means one review per PR, not one per amended head. A missing or
> stalled first pass is a review-infrastructure blocker, not permission to merge.

The automatic connector pass starts when the PR opens. `bin/review_request.py`
exists only as the one Tier A fallback when that automatic trigger is absent;
it permits exactly one full-audit request for the PR. The connector review is
a final gate, never a design-discovery loop.

Use the mandatory one-pass GitHub Codex gate:

- the orchestrator's local review and required project gates are mandatory
- GitHub Codex review/comments are consumed before every merge
- a clean `chatgpt-codex-connector` review/comment that explicitly covers the
  exact current OID is terminal for that head
- a connector-authored `+1` (`thumbs-up`) **on the exact manual fallback request
  comment** is terminal CLEAN, after pickup, while the PR head still equals the SHA
  that request named. Verify all six: the actor is the connector, the reaction is
  on that request comment, the requested SHA, the current head, that this request is
  the latest for this head, and that it has not been edited since the reaction was
  left. Any head change voids that reaction-only clean signal. It does not create
  permission for a second bot pass: locally prove the amended head or remain blocked.
  An ambiguous or removed reaction is non-terminal, and a bare `eyes` reaction or the request comment itself is never a
  verdict — `eyes` means accepted and in progress
- the meaning of `+1` does **not** vary by tier. Tier A takes its strength from the
  mandatory final-head request, required local exact-head verification, mutation and
  verification evidence, and settle plus adjudication. Requiring a posted text
  review for Tier A would deadlock whenever the connector's clean protocol is
  reaction-only, and the request plus a connector-authored `+1` is already a durable
  GitHub artifact
- the clean verdict and request-comment `+1` are the only two connector-authored
  clean signal models. A completed non-clean review becomes a third terminal
  gate outcome only after every thread it initiated has the accepted
  dispositions defined below; nothing else in
  [the reviewed artifact set](#reviewed-artifact-set) is terminal
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
### Projects without GitHub

<a id="review-surface-independence"></a>

**Tier A does not depend on GitHub.** The gate is a property of the change, not of the
place the review happens, so a registered project with no GitHub surface still owes the
same review — it satisfies it with durable **mailbox request and verdict packets** naming
the exact repository commit OID, the project, the repository scope, the requester and the
reviewer. What GitHub exposes is that project's *implementation* of this gate, not the gate
itself — see [the reviewed artifact set](#reviewed-artifact-set) for what each lane's
implementation consists of.

One asymmetry, and it is not an oversight: **a non-GitHub lane has no reaction-only
terminal path.** A reaction is terminal because it sits on an identifiable request artifact
that names a head; a mailbox lane has no equivalent, so it needs a textual verdict packet.
Conditioning the requirement on GitHub instead would make the gate unsatisfiable for such a
project, which reads as an exemption and is not one.

### The reviewed artifact set

<a id="reviewed-artifact-set"></a>

**Every instruction in this repository that says to read, re-read or inspect review state
means the set below for the lane that change is on, and this is the only place either list
is written.**

**On a GitHub-backed lane, exactly these five:**

1. top-level PR comments
2. review bodies
3. review threads
4. inline review comments
5. reactions

**On a lane with no GitHub surface, exactly these two:** every durable **review request
packet** and every durable **verdict packet** in the mailbox naming the exact commit OID
under review. The count differs because GitHub splits one conversation across five
artifacts and a mailbox does not; what does not differ is that *all* of the lane's review
surface is read, which is the only property any instruction here relies on.

Naming five GitHub artifacts unconditionally would have made Tier A unsatisfiable on the
mailbox lane defined directly above — the gate would demand artifacts that cannot exist
there, which reads as an exemption and is not one.

Referenced, never restated. Twelve sites used to enumerate this and they had drifted into at
least five different versions -- some omitting comments, which is where a re-review request
lives, and some omitting reactions, which is where a terminal `+1` lives. Each omission was
a working path to merging on a superseded signal, and each was fixed one site at a time
until it became clear the list itself was the defect. A paraphrase of this list anywhere
else is a second source that goes stale the moment this one moves.

- a head-named clean connector verdict is not merge-immediate. Hold an
  approximately five-minute post-clean settle, then re-read [the reviewed artifact set](#reviewed-artifact-set) in full before
  merge, because the connector can emit multiple review artifacts for the same
  head
- **a reaction counts only on the latest, unedited request artifact.** GitHub keeps
  reactions across an edit, so a request comment edited to swap an old SHA for the
  current one still carries the `+1` the connector left for the *old* head, and all
  four checks then pass on a review that never happened. If the request artifact has
  been edited since the reaction was left, or is not the most recent request for this
  head, the reaction is not terminal. Do not issue another request to repair an
  invalid reaction; hold for a textual first-pass verdict or report the review
  infrastructure blocker
- a finding rejected or deferred without a code change is completed by its
  thread-linked disposition accepted by the lane owner and release-gate worker.
  Do not request another connector review on the unchanged head; the exact-head
  review already happened, and another request would violate the one-external-
  review-per-PR rule. Once every thread from that exact-head review has such
  a disposition, the completed review is a terminal gate outcome and receives
  the same settle and full artifact re-read as a clean verdict
- report the exact verdict, or the connector-authored `+1` on the manual fallback
  request comment with its timestamps and the SHA that request named, and confirm
  that no later push occurred
- a push after the first pass requires local exact-head verification of every fix;
  it does not start or require a second bot pass
- **there is no silence fallback.** A PR with no terminal first bot pass does not
  merge at any tier. Tier A may issue the one manual fallback request when the
  automatic trigger did not start; otherwise report the review-infrastructure
  blocker. At no tier is quiet a signal
- **no elapsed time is ever a terminal signal.** There is no resettable settle
  that ripens a head for merge. Waiting is what a Tier A head does while a
  requested review is outstanding; it is not a way to acquire the signal
- **the worker who pushed a review fix owns proving that amended exact head.**
  Rerun the required local exact-head verification and inspect the fix directly.
  Do not re-request or withdraw the bot review; the first pass is the review record
- neither a bot verdict nor a reaction waives mergeability, the lane's required
  local verification (tests and defect-verbatim mutation proof), or full
  inspection of [the reviewed artifact set](#reviewed-artifact-set).
  The first connector pass is the PR's external gate; local verification owns
  amended-head proof. A second model review must not be run. The pre-PR cold
  full-diff review is unchanged and happens once, before the first PR-ready head.

**All no-terminal-artifact fallbacks are deleted.** Automatic-trigger silence,
eyes-only artifacts, and prior-head artifacts are non-signals with no clock:

- **No terminal first pass.** At Tier A, request once if the automatic trigger
  never started. At every tier, hold until the bot returns a terminal review.
- **Eyes-only current-head artifact.** A current-head `eyes` reaction is not a
  terminal signal and never becomes one. It neither blocks nor ripens anything.
- **Prior-head artifacts only.** A prior-head clean verdict is not evidence for
  an unrelated push. After review findings, however, the completed first pass
  remains the PR review record and the amended head is proved locally.

#### First-pass precedence

The first bot pass is pending until a clean review or a completed review with
findings arrives. An `eyes` reaction is pickup only. If the automatic trigger did
not start, Tier A may issue one manual fallback request; do not retry it and do
not replace a missing terminal review with a timer or disposition. Non-GitHub
lanes use an exact-OID mailbox request and verdict, with the same no-silence rule.

If the PR is waiting only for the remote review state (the connector's first
pass) or merge state, keep it open and create or update a Codex heartbeat attached
to the current thread with a 6-minute cadence. Each heartbeat must re-check the
review state, merge state, and [the reviewed artifact set](#reviewed-artifact-set)
in full. A manually dispatched `verify.yml` run, if one was explicitly requested,
may be observed but is never a wait condition — never hold on an absent optional
Actions run. The heartbeat waits for the configured automatic first pass and
reports a stalled trigger; it never converts time into a pass.

PR-wait heartbeats are a safety-fuse, not the primary routing path. When a
heartbeat or queue owner finds actionable PR feedback that needs the implementer
to change their branch, it must send a durable mailbox packet and inspect the
`deliver.py` result. If `autobridge_ready: true`, the current Phase 1 route is
session autobridge and no AX doorbell was requested. If
`ax_doorbell_required: true`, ring the implementer once with AX even if busy.
Do not prove the composer empty first: composer content and `AXValue`
readability/opacity are never a hold for Codex, and busy alone is not a hold
either — the ring overrides and sends. Only a genuine targeting/operation
failure (no or ambiguous target, a non-Codex or unrecognized profile, an
AX-trust failure, a clear/type/submit failure, or post-submit identity loss)
means hold and enter recovery.
`VERIFIED` exit 0 confirms delivery; `QUEUED (UNCONFIRMED)` exit 0 preserves the
mailbox/blocker follow-up but is not exact-thread delivery proof and must not be
re-rung. The idle input gate applies only if attended screenshot/keyboard
Computer Use is needed as fallback. Do not silently wait for the next heartbeat
or depend on the operator to notice the PR comment.

The heartbeat may complete the wait after the release-gate worker verifies the
exact current head passes the required local exact-head verification, the PR is
mergeable with clean `mergeStateStatus`, and the full current comment, review,
payload of [the reviewed artifact set](#reviewed-artifact-set) has no actionable finding. Treat the GitHub
Codex review gate as complete when any of these holds:

- the latest top-level `chatgpt-codex-connector` review/comment explicitly
  covers the exact current OID and reports no actionable or major issues, or
- a connector-authored `+1` (`thumbs-up`) sits on **the exact manual fallback request
  comment**, the PR head still equals the SHA that request named, and no subsequent
  push occurred. Verify all six: the actor, that request comment, the requested SHA,
  the current head, that this request is the **latest** one for this head, and that it
  has **not been edited** since the reaction was left. The last two are not optional
  refinements: GitHub preserves reactions across an edit, so a request edited to swap
  an old SHA for the current one carries a `+1` for a review of the *old* head and the
  first four checks all pass. A `+1` attributable only by timestamp, or sitting on any
  other artifact, is **not** terminal. That reaction is terminal for the bot wait on that
  head when the required gates above remain clean, and it receives the same
  approximately five-minute post-clean settle and full re-read as a text verdict,
  or
- the connector completed the PR's first pass clean on a prior OID (by text
  verdict or request-comment `+1`), and the amended current head has complete
  local exact-head verification. The original artifact remains the one bot pass;
  every change since its reviewed/requested OID must be included in the local
  proof, and no arriving finding may remain unadjudicated, or
- the connector completed the PR's first review with findings and every thread
  it initiated has a thread-linked disposition accepted by the lane owner and
  release-gate worker. Any amended current head also needs local exact-head proof.
  This disposed-review completion receives the same settle and full artifact
  re-read.

The re-read that follows a reaction covers
**all of [the reviewed artifact set](#reviewed-artifact-set)**, and
revalidates all six reaction conditions. Do not create a new request for an
amended head.

For the clean-verdict path, do not merge immediately after the first
head-named clean artifact. Observe the approximately five-minute post-clean
settle and then re-read [the reviewed artifact set](#reviewed-artifact-set) in full. When an
exact-head review raises findings, apply their written dispositions; do not
request another connector review on the unchanged head.

If no clean verdict, valid fallback-request `+1`, or disposed-review completion
exists for this PR, it does not merge at any tier — there is no elapsed-time
substitute:

- **No automatic review exists for this PR**: Tier A issues the one manual
  fallback request; every other tier reports the review-infrastructure blocker.
- **The automatic review or fallback request is unanswered**: hold. No elapsed
  time, tier, or release-gate disposition substitutes for the required first pass.
Proceed only when all of these are true:

- the connector completed the PR's first pass; every finding has a written
  disposition accepted by the lane owner and release-gate worker; and required
  local exact-head verification passed after any fixes
- the required local exact-head verification is clean on the latest head (a
  manually dispatched verify.yml run, if any, is supplementary evidence only)
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
- the release-gate worker has recorded the merge decision under the standing
  project policy

Read [the reviewed artifact set](#reviewed-artifact-set) directly. Do not infer the
result from a review body alone: inline threads carry findings. After the terminal
first pass, observe the approximately five-minute settle and re-read the full set.
If feedback landed, fix or respond to it, push, and rerun the required local
verification on the new exact head; do not request a second bot pass.

If the wait cannot self-progress because local verification stalled, review state
is ambiguous, or the implementer has not acknowledged a routed review-fix request,
the heartbeat must escalate by doorbell with the exact blocker and next action.
Delete or rewrite any PR-wait heartbeat that misses this escalation path.


Keep the heartbeat active until rerun local verification, merge state, and current
PR comments/reviews are clean. Delete the PR-wait heartbeat immediately after the
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

1. synchronize the persistent local checkout to `origin/main` with the
   executable local-main sync gate (see the gate command below)
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
workflow: future sessions read it before starting or handing off more work, and
`current_runtime.py` refuses to bootstrap from a checkout that is not exactly
`origin/main`. After any merge, bring that checkout current with the executable
local-main sync gate instead of a manual fast-forward:

```bash
python3 bin/local_main_sync.py --apply
```

The gate fetches `origin/main`, classifies the checkout, and prints the exact
local and remote SHAs. It fast-forwards only when it is safe (`already_current`,
`aligned_to_main`, `fast_forwarded`) and fails closed with exit 1 on
`dirty_tracked`, `active_branch`, or `diverged` — it never stashes, discards, or
overwrites tracked or staged work. Project-private untracked state (`Chats/`,
`Logs/`, `State/`) is ignored. On a fail-closed classification, resolve the named
blocker by hand (land or move the local work aside with an explicit note); do not
leave the shared checkout behind `origin/main` just because GitHub is up to date.
For Amiga, that checkout is `/Users/pixexid/Projects/llm-collab`.

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

For `llm-collab` itself, refreshing the deployed runtime after every merge is
mandatory before new coordination work starts. Workers and new sessions must
not use a parked source checkout as the collaboration runtime.

Use:

```bash
<source_worktree>/bin/llm-collab deploy_runtime.py \
  --source <source_worktree>
```

The deploy command validates an exact `origin/main` source and the contract before
resetting only the deployed runtime's tracked files. It preserves source-checkout
dirt, runtime-state symlinks, and private files.

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
