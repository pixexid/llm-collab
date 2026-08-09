<!-- CONTRACT_VERSION: 17 -->
# AGENTS.md

## This file is the source of truth

This file plus `docs/workflows/` is the canonical worker contract. Memory files,
skills, project notes and branch-local docs must **point at it, never restate it** — a
restated command is a cached copy that goes stale without telling anyone. On
2026-07-25 that cost 27 packets over eleven hours: eight memory files taught a
`deliver.py` invocation without `--repo-targets`, and nothing reported it.

`python bin/contract_drift.py --agent <you>` reports your own stale copies;
`session_bootstrap.py` runs it for you and prints the contract version at session start.

This repository is the shared `llm-collab` coordination runtime. It is not the
Amiga workspace, the Nuvyr workspace, or any other product repository.

## Shared Philosophy

These defaults govern every worker and project; mechanics remain in the linked
workflows below.

- **Make the smallest complete change.** Load Ponytail; delete or reuse before
  adding code, and fix the root cause at the shared seam rather than every symptom.
- **Use evidence, not recollection.** Memory, summaries, prompts, and model knowledge
  are leads. Read current source and the installed environment. For volatile APIs,
  libraries, models, or platforms, use current primary docs or live metadata and
  official web sources when local evidence cannot answer.
- **Use capabilities before inventing.** Check relevant tools, skills, and plugins as
  soon as they may help. Reuse the repository's authority; do not create a second
  store, queue, policy engine, or helper for a decision it already owns.
- **Work as a team, with one writer.** Ask the worker with relevant context early via
  the durable mailbox; identify the sender and exact scope, and keep safe work moving.
  Collaborate on design and review, but keep one writer per implementation lane.
- **Prove the claim, not the activity.** A green test, review, wake, or summary counts
  only when it distinguishes the failure being ruled out. Inspect the actual artifact
  and prefer the smallest check that fails when the claimed invariant breaks.
- **Make learning durable.** Update the existing canonical source in the same change,
  link to it, and remove stale copies. Do not leave reusable knowledge only in a chat,
  memory, PR body, or worker session.
- **Finish outcomes.** Setup, messages, reviews, and tests are intermediate. Keep safe
  queue work moving; escalate only an exact authority or external blocker; carry clean
  work through merge, release, reconciliation, and justified closure. Preserve explicit
  follow-ups, and never close a requested feature merely because review exposed defects.

### Recent contract changes

Contract v16 (2026-08-08) changes first-class Claude registration eligibility:
an explicitly named native session must prove that its named artifact belongs to
the target project's app checkout before registration can write a lease. The
canonical restart ownership, bootstrap drift report, and dead-id packet recovery
live in
[`Session Startup`](docs/workflows/session-startup.md#restarted-first-class-sessions).
The version signal makes the stricter registration acceptance visible to cached
workers. Related GH-538.

Contract v17 (2026-08-09) qualifies exactly `pi / kimi-coding/k3 / high` for
BB authoring on the bootstrap/first-delivery path, using the measured profile
identity. The explicitly selected `bin/bb_spawn.py` path reaches
`plan_spawn` in `llm_collab/spawn_gate.py`, is deliberately not covered, and
continues to rely on orchestrator review controls. Canonical details live in
[`BB Workers`](docs/workflows/bb-workers.md). Related GH-705.

Contract v15 (2026-08-08) makes measured BB model failures hard exclusions for
every text-bearing assignment, read-only included. Read-only work produces the
evidence that gates later decisions, and unlike a patch it has no diff review to
catch a corrupted glyph or drifted source coordinate. The canonical scope,
models, and measured rationale live in
[`BB Workers`](docs/workflows/bb-workers.md#spawn-in-an-isolated-worktree).

This changes what a worker may do: a read-only assignment that the spawn gate
previously admitted is now refused. The version bump makes that changed
activation eligibility visible to cached workers. Related GH-647.

Contract v14 (2026-08-08) makes send-time routing admission fail closed before
durability. `deliver.py` admits only an exact target, a valid Codex AX fallback,
or the documented explicit broadcast; every other unresolved route refuses
before any durable write. An admitted broadcast identifies itself, and
exact-session readers remain exact rather than consuming null-targeted worker
packets. Canonical route fields and refusal semantics live in
[`Message file schema`](docs/schema-reference.md#message-file-schema); sender
procedure and dispatch proof live in
[`Send a packet`](docs/workflows/collab-thread-quickstart.md#4-send-a-packet).

The version bump makes the changed rule visible to cached workers. On 2026-08-05
a Pi worker sent two packets to Claude: both were written durably, both reported
success, and both carried `target_session_id: null`. They were found only by
reading the chat directory by hand after the sender asked why there had been no
reply, costing 35 minutes on a merge-blocking lane. The old contract explicitly
taught silent unbound dispatch, so correcting its text without a version signal
would repeat the stale-copy failure this marker exists to prevent. Related
GH-590, GH-554, and GH-535.

Contract v13 (2026-08-07) corrects the lane WIP cap to count **writing lanes
only**. Read-only lanes — audits, probes, scoping, and reviews with no branch and
no designated writer — never counted and do not now. One-writer-per-lane
remains a separate, unchanged rule. The scarce resource is **orchestrator
verification capacity**, a judgment rather than a constant: the limit protects
independent verification rather than rationing worktrees.

Practice had already diverged for a full session under an override recorded only
in a chat message, so cached worker instructions still taught the obsolete
"implementation lanes" wording without a version signal. This bump makes that
drift visible. Canonical definitions, rationale, and activation mechanics live
in [`Lane WIP limit`](docs/workflows/task-intake-and-delegation.md#lane-wip-limit).

Contract v12 (2026-08-06) makes the **durable packet plus session-autobridge
dispatch the routine wake for every watcher/monitor-backed recipient, Codex
included**, and demotes AX to the fallback. This supersedes only the routing half
of v10: AX is still Codex-only, and still only ever the exact command
`deliver.py` prints — what changes is *when* it is used, never *who* may receive
it.

`deliver.py` has behaved this way for some time; the contract text was the part
out of date. A dispatchable autobridge target takes precedence and suppresses the
doorbell (`wake_fallback_allowed = not autobridge_ready and not
dispatch_scope_refused`). Read that predicate literally rather than enumerating
cases from it: AX is available whenever **no dispatchable target resolved and the
refusal was not terminal**. Missing and inactive bindings are the common shapes,
but they are not the whole set — an explicit target that contradicts the
recipient's binding refuses with `exact_binding_mismatch` and leaves the fallback
allowed too.

Exactly two states are **terminal** and suppress every wake lane: an
**unreadable** binding and a **scope refusal**. Both set
`dispatch_scope_refused`, which makes `wake_fallback_allowed` false, because no
lane may wake a recipient whose authoritative record could not be read or whose
scope forbids the packet. Those are repairs; do not try to ring through them.

An unbound recipient may still take the Codex AX fallback when eligible. If no
AX or explicit broadcast route exists, contract v14 refuses before the durable
write instead of producing the former silent `exact_binding_required` dispatch
failure.

Why it is worth the change: while AX was the only route to Codex, no Pi worker
could reach it — Pi workers cannot ring AX — so every reply had to be relayed by
a Claude thread. On 2026-08-05 that cost 35 minutes with two GH-549 packets
unread behind a single relay. With the recipient bound, any worker's `deliver.py`
reaches Codex directly and the relay hop disappears. Mechanics in
`docs/workflows/session-autobridge-runbook.md`.

Contract v11 (2026-08-03) ends idle standoffs at their real cause: bad
delegation. A question and a task delegation are different acts — a question gets
an answer, a delegation gets a deliverable — and must never share a packet. A
delegation is a frozen, bounded work order with an exact deliverable and no open
question; an acknowledgement is not a deliverable, so confirm the artifact before
claiming progress. Workers mirror this: self-label an ack ("ACK only — no work
started"), never stay silent when blocked, and never guess a mixed packet.
Re-driving only works on a live worker — confirm the session is alive and the
delivery landed before treating silence as waiting. Track what you actually sent,
and never report a worker "on task" when you only asked it a question. Mechanics
in `docs/workflows/task-intake-and-delegation.md` → `## Delegation is a frozen
task, not a message`.

Contract v10 (2026-07-31) makes AX a Codex/ChatGPT-app doorbell only. Never
invent or hand-author an AX command: run only the exact command printed by
`deliver.py`. For every non-Codex watcher/monitor-backed recipient, including
Claude, deliver durably and stop; the recipient's watcher owns pickup.

Contract v9 (2026-07-31) makes the configured GitHub Codex review a mandatory
one-pass PR gate. Every PR waits for that first bot review before merge. Fix and
adjudicate its findings, verify the amended head locally, and do not request a
second bot pass. No elapsed time or review tier bypasses the first pass.

Contract v8 (2026-07-31) adds the shared philosophy above: simple complete changes,
current primary evidence, early use of existing capabilities, durable collaboration,
discriminating proof, preserved knowledge, and finished outcomes are universal worker
defaults.

Contract v7 (2026-07-30) makes review **one pass, not a loop**. Request the
bot/connector review **once per PR** — not once per amended head. Fix what that
single pass (plus the one independent local review of record) surfaces, verify the fix
at the new exact head yourself (focused tests green and the fix visibly closes the
findings), and **merge**. Do **not** re-request a review on the fixed head, and do
**not** re-review each new head: the per-candidate-head re-request cycle is retired
because it never converged — each re-review surfaced a deeper edge case and PRs ran
5–6 rounds. Capture remaining edge-case findings as a tracked **follow-up issue**
rather than another round; a confirmed *serious* defect is still fixed before merge,
but the bar is "confirmed serious," not "the bot found one more thing." This
supersedes the v5/v6 request-shape and re-trigger rules wherever they conflict.

Contract v6 (2026-07-28) makes the installed **Ponytail** skill mandatory at
full intensity before every task: planning, review, implementation, testing,
documentation, and operations. Load it first; missing Ponytail is incomplete
worker setup to repair, not permission to continue without it.

Contract v5 (2026-07-28) rewrote the merge gate and the Tier A entry conditions.
If your last session predates it, discard cached cap-default, deferral,
reviewer-count and request-format rules, and read
[`commit-push-prs.md`](docs/workflows/commit-push-prs.md), which is canonical for
all of it:

- a Tier A lane writes a one-page **lane contract** before the first branch, and
  the review verifies the diff against it;
- findings route **per-finding** at arrival — contract violations and regressions
  block, pre-existing issues and broadenings defer in writing;
- **One external bot review per PR**: the first connector pass is the PR's
  external gate, and amended heads use local exact-head proof rather than a
  second model review;
- the cap default is **merge-with-followups**; 3 amended heads or 4 hours in
  review-fix forces a recorded merge-with-followups-or-close decision;
- review requests are generated mechanically by `bin/review_request.py`; a
  hand-typed SHA is a process defect;
- at most **two active writing lanes**; definitions, exclusions, and rationale
  live in [`Lane WIP limit`](docs/workflows/task-intake-and-delegation.md#lane-wip-limit).

Earlier versions are in the git history of this file. Nothing below depends on
reading them.

## Required Reading

Before changing shared tooling or operating a project lane, read:

- the installed `ponytail` skill and apply it at full intensity;
- `README.md`
- `docs/multi-project.md`
- `docs/workflows/session-startup.md`
- `docs/workflows/collab-thread-quickstart.md` — starting and running a collab
  thread end to end
- `docs/workflows/orchestrator-sessions.md` — watcher, succession, supervisor,
  model-routing, and bb-update protocol for orchestrator sessions
- `docs/workflows/task-intake-and-delegation.md`
- `docs/workflows/bb-workers.md` — required before spawning or driving BB workers
- `## Requesting Code Review` in this file — it governs every repository a lane
  touches, not just this one

Then read the target project's own repository instructions and local policy
under `{project_state_root}/{project_id}/`.

## Project Boundary

Project-scoped is the default. Universal behavior is allowed only when it is
project-independent by construction.

- Every chat, message, task, queue, runtime binding, report, and project-aware
  command must use one registered `project_id`.
- A project-aware reader or mutator must require an exact project match. Do not
  treat missing, empty, or `null` project IDs as belonging to the requested
  project. Legacy backfills belong only in explicit migration tooling.
- Project-specific repositories, commands, design sources, database refs, tool
  surfaces, GitHub settings, and runbooks come from that project's
  `projects.json` entry, project-local state, or explicit task fields.
- Do not hardcode one project's values in shared `bin/`, `scripts/`, templates,
  generated guidance, or universal workflow docs.
- `agents.json` is universal only for collaborator identity and activation
  capabilities. Keep product paths, design contracts, database settings, queue
  state, and routing policy out of it.
- Keep generated and runtime outputs under
  `{project_state_root}/{project_id}/`; one project must not overwrite another
  project's report or queue.
- Amiga compatibility is project-specific. An Amiga fallback must be guarded by
  an exact `project_id == "amiga"` check and must never become a workspace
  default.

When changing a shared contract, add focused coverage for Amiga and at least
one non-Amiga project, then run the full test suite:

```bash
python3.11 -m unittest discover -s tests
```

## Workers own their own setup

If a step is one you can perform and verify yourself, perform it. Do not hand it back
to the operator and wait.

That includes registering a project, adding an agent, creating a chat, publishing your
runtime session, starting your watcher, and repairing your own environment. An operator
naming the work — "implement X with zcode on project Y" — is the whole instruction; the
setup it implies is yours.

For Pi workers, install the lifecycle extension once, then use the start flow in
[`collab-thread-quickstart.md`](docs/workflows/collab-thread-quickstart.md#pi-workers);
do not duplicate its registration and monitor setup by hand. A worker with no
eligible project profile supplies its profile and runtime home explicitly on that
first start; later starts restore the recorded profile.

**Genuinely operator-owned**, and worth stopping for: credentials and account
settings, legal or financial commitments, destructive actions outside normal
recovery, and product decisions for which the workers have no stated authority.
Routine merges, releases, issue closure, and bounded risk acceptance are worker
work when the objective gates and standing project policy permit them. The lane
owner and release-gate worker must discuss material trade-offs, record the
decision and preserve any follow-up. Escalate to the operator only when those
workers cannot reach or execute a safe decision without operator-only input.

The test is not "is this tedious" or "did they mention it" — it is whether you could do
it and check that it worked. If you could, it is yours.

## BB worker surface

BB is the current surface for orchestrator-managed worker threads. Before
spawning, steering, or accepting work from one, read
[`bb-workers.md`](docs/workflows/bb-workers.md); it owns the commands, isolation
hazard, loopless delegation contract, return path, and inspection rules.

A BB thread is not thereby an `agents.json` collaborator, bound llm-collab
session, receipt-bearing participant, or canonical-bus member. The orchestrator
reads its results through BB, authors any durable packet under its own
registered identity, and remains the integration point. A BB worker never
supplies another agent as `deliver.py --from`; that records the agent as the
author, not a relay. First-class relay provenance remains prospective in
[GH-604](https://github.com/pixexid/llm-collab/issues/604).

The current worker fleet runs through BB; AX is not a routine lane or a BB
transport. Contract v12's unchanged fallback predicate remains the authority
for whether `deliver.py` offers a doorbell. Whether an offered doorbell can land
is a dynamic runtime property, never a standing inference from process state.
Use the live capability checks in
[`bb-workers.md`](docs/workflows/bb-workers.md#communicate-in-both-directions).

## Adding A Project

`docs/multi-project.md` → `### Onboarding a new project`. Do not rerun
`scripts/init.py` on an existing workspace unless the intent is to reinitialize
the whole thing.

## One writer per lane

A lane has exactly one writer: one worktree, one branch, one owner. If another
writer is already active on a change, yield and coordinate through the mailbox
rather than opening a parallel lane. The mailbox is the only channel between
workers — a PR comment is not a message, and a GitHub verdict is not a substitute
for draining the inbox before you open a PR or merge.

**Write the durable packet and let dispatch wake the recipient.** For every
watcher/monitor-backed recipient — Claude, the Pi workers, and Codex once it is
bound — a matching session-autobridge target takes precedence and suppresses the
doorbell.

**`autobridge_ready: true` is send-time routability, not delivery.** It means a
dispatchable binding existed when the packet was written. Nothing in the
`deliver.py` result observes the recipient's watcher, so a post-send watcher or
transport failure is invisible to the sender: **delivery is unconfirmed until a
dispatch or acceptance receipt exists** for that packet. Never read
`autobridge_ready: true` with `ax_doorbell_required: false` as "the recipient has
it". Where no receipt exists, **preserve the packet and diagnose** — binding,
watcher, sidecar — and do not reach for AX. A ready binding suppresses the
doorbell, so in exactly that state `deliver.py` prints no AX command and there is
nothing legitimate to run. AX becomes available only when a **fresh**
`deliver.py` result prints it, which happens only after the exact binding is
absent or nondispatchable.

The recovery is concrete, so a stranded packet is never a dead end:

1. read the recipient's watcher log for its `new_message` / `autobridge_dispatch`
   / `autobridge_consumed` lifecycle, and its adapter sidecar for a live endpoint;
2. reconcile whatever the log names — a pointer to a deleted packet aborts
   enumeration before message selection, and a stale unread backlog will flush
   into a live thread on restart;
3. restart the recipient's watcher on a clean baseline;
4. re-dispatch **one fresh probe packet** and require its receipt before treating
   the channel as repaired;
5. only then consider the stranded packet — and **do not resend it by reflex**.
   `deliver.py` mints a fresh timestamped path on every send while dispatch dedup
   is keyed by message path, so a resend is a *new* packet to the runtime and can
   produce a second turn. Resend only when the original is proven to have failed
   **before** runtime acceptance and is still unread. Where acceptance is
   ambiguous, or the packet was marked read with no processed evidence, record
   the ambiguous delivery and reconcile explicitly instead.

The recipient owns its own watcher and binding, so steps 2-3 belong to it; a
sender that cannot reach it says so in the durable mailbox rather than inventing
a wake. That sequence is what actually repaired the outage below.

This is not hypothetical. On 2026-08-05 every packet to Codex reported
`autobridge_ready: true` / `ax_doorbell_required: false` while its watcher failed
on **every poll for roughly twenty hours** — a deleted-packet pointer made
`bounded_unread_messages` raise before message selection, and the deployed
sidecar token was absent so no external WS endpoint existed at all. The sender
saw success throughout. Two independent faults, zero sender-visible signal.

AX is the **fallback**, not the routine path, and it is still Codex-only: no
other worker is ever an AX ring target, and it is only ever the exact command
`deliver.py` prints — never an invented `axsend`, and never a way around a
recipient's watcher. Ring only when `deliver.py` asks for it — which is whenever
no dispatchable target resolved and the refusal was not terminal, not a fixed
list of causes you can recite. The two terminal ones, an unreadable binding and a
scope refusal, suppress the doorbell and are repairs rather than rings.

Treat a missing doorbell as information, not permission. An unbound recipient
with no AX or explicit broadcast route now returns a typed pre-write refusal;
it does not create a packet. If a recipient you expect to be bound is not, that
is the defect to fix — the recipient registers its own session, since only it
knows its exact runtime id.

## Shared Checkout Safety

This checkout may contain another lane's local work. Inspect `git status` before
switching branches, pulling, staging, or cleaning. Preserve unrelated tracked
changes and untracked files unless their owner explicitly authorizes removal.

## GitHub Autolink Safety

This repository has a GitHub autolink for the `GH-` issue prefix. Treat
`GH-123` as a real issue reference here, not as inert project shorthand.

Do not put any GitHub closing keyword immediately before a `GH-<number>`
reference in PR bodies, merge commit bodies, ordinary commit messages, or issue
comments, even inside negated prose. Use neutral wording such as `Related
GH-123`, `Related #123`, or a full issue URL when the referenced issue should
stay open.

This is repo-local policy because it has happened twice — PRs #153 and #198 each
closed an issue nobody intended to close, the second time through the `GH-`
autolink in a merge commit body, and in both cases the adjacent prose was
*negated*.

Running this repository's `bin/issue_link_check.py` against llm-collab: pass
`--gh-autolink` with `--pr` or `--sweep` so `GH-N` references are recognized. The
flag is intentionally omitted from the portable workflow docs
(`docs/workflows/commit-push-prs.md`) because other repositories may not define
the `GH-N` autolink.

## Requesting Code Review (all workers, every repository)

GitHub Codex review is automatic when a PR is opened or marked ready. **Every PR
waits for that first bot review before merge.** A clean first pass may proceed
through the remaining gates. If it reports findings, fix or disposition them,
verify the amended exact head locally, and merge without requesting a second bot
pass. Silence, elapsed time, and review tier never substitute for the first pass.

This section is worker-facing and applies in **every** repository a lane touches, not
only this one. It is reachable from every worker because this file is Required
Reading. It is distinct from the `## Code Review Rules` section below, which is read
by the reviewer when it reviews *this* repository and must be authored per repository
from that repository's own incidents.

**Tier A — you MUST ensure the automatic review starts, and request it manually
only if the automatic trigger did not start.** Any change
touching credentials, authentication or an authority decision; money, provider or
idempotency paths; **input we do not control** — network peers, another project's
data, an external API, or anything a user or third party supplies — including its
resource and deadline bounds; shared code that changes an observable contract (return or exception
shape, authority or source selection, side effects, persistence, ordering,
deadline/resource behaviour, compatibility, failure handling); concurrency, ordering,
partial state, TOCTOU or atomicity; migrations, DDL, grants or RLS; a defect family
that has already produced a finding in that repository; or tests and docs that govern
or can weaken any of the above. **Failing to ensure that first pass starts is itself
a gate violation.**

**Tier B — do not add a manual request when the automatic review started.** New feature surface or a multi-module refactor with no
Tier A contact; proof-reshaping test changes that do not cover a Tier A invariant;
normative docs for non-Tier-A behaviour.

**Tier C — do not request manually, but still wait for the automatic first pass.** Formatting, lint or mechanical changes; non-normative
prose and comments; additive tests with no gate, fixture or baseline change;
single-caller behaviour-preserving edits.

When the Tier A fallback is needed, request with `@codex review for <focus>`, naming **every** Tier A family the diff
touches, asking for the full diff through those lenses, and **stating the exact head
SHA the request is for**. The SHA is not decoration: a connector `+1` is terminal only
while the head still equals the SHA that request named, so a request without one leaves
the reaction path unsatisfiable and there is nothing to bind the verdict to. **Request
at most ONCE per PR** — automatic or manual, not once per amended head. Fix what
that single pass surfaces, verify the fixed head yourself, and merge; do **not**
re-request a review on the fixed head and do **not** loop. A remaining edge-case
finding becomes a tracked follow-up issue, not another review round.

Every **arriving finding** must be adjudicated in writing at every tier, whatever
head raised it and whatever its current resolution state — enumerate every thread,
resolved or not, because a checklist phrased over *unresolved* threads cannot see
one someone clicked Resolve on without recording anything. A push is not an
adjudication. A review is P0/P1-scoped, so it complements and never replaces the
exact-head verification model or defect-verbatim mutation proof.

Generate the request mechanically — `bin/review_request.py --pr <n> --project <id>
--tier A --contract <issue|TASK-id> --focus "..."` reads the head SHA rather than
accepting one by hand, which is how #347 came to carry a fabricated request.

**"Untrusted" means input we do not control.** Our own workspace — `State/`,
`Chats/`, `projects.json`, the checkout itself — is not an adversary: anyone who can
write there can edit `bin/` and already has code execution as this user. Bound those
reads against *accidents* (a huge directory, a hung mount, a corrupt record) and stop
there. Hardening a local tool against a hostile local filesystem has no natural floor
and will loop forever; llm-collab#306 spent eleven review rounds proving it.

Canonical detail — lane contract, per-finding routing, one bot pass per PR,
terminal signals, the lane budget, and ownership — is in
`docs/workflows/commit-push-prs.md` and `docs/workflows/review-and-handoff.md`.
Which repositories are enrolled and what the rule audit found:
`docs/multi-project.md`. Policy and rationale: `llm-collab#310`.

## Code Review Rules

Path-scoped review rules for Codex Code Review. Only rules matching the changed
files fire, and findings cite the rule that produced them. There are four: each
encodes a class that was already adjudicated in this repository and then
rediscovered at review cycle 2-3, where it forced an amendment or a retracted
CLEAN. Related GH-185.

Keep the set small. Add a rule only after the class has cost a real cycle, and
remove one that turns noisy.

### SQL text constraints and embedded NUL

Scope: `llm_collab/`

A byte length/shape predicate can still admit an embedded NUL. `length`, `GLOB`,
`LIKE`, and `substr` stop at the first NUL, so `length(k) = 64 AND k NOT GLOB
'*[^0-9a-f]*'` accepts `'a' * 64 || char(0) || <arbitrary>`. Equality, `IN`, and
`instr` see whole bytes.

Safe path: a new or revised TEXT `CHECK` family built on `length`/`GLOB`/`LIKE`/
`substr` also rejects `instr(column, char(0)) != 0`.

A shape predicate must also constrain the shape. `col GLOB '[0-9a-f]*'` matches
any string whose *first* character is hex; pair a full-string character class
with an exact length.

Exempt: released immutable migration SQL protected by checksum and fingerprint.
This rule does not ask for V1/V2 to be rewritten (see #176).

### Pin one descriptor chain for correlated reads

Scope: `llm_collab/compatibility/`, `llm_collab/daemon/`

Re-resolving an ancestor chain by pathname on each call leaves every call
internally consistent while nothing checks that two calls resolved through the
same root. Per-file identity checks say nothing about ancestor-chain identity
across calls.

Applies to authority-sensitive traversal, and to any operation batching or
correlating multiple reads under one workspace root. A one-off unrelated path
read is not a violation.

Safe path: open the root/ancestor chain once, hold the descriptors, open below
them `dir_fd`-relative, and remove root-path parameters from helpers so the seam
cannot be reintroduced. Pathname revalidation is a second layer, never the only
one.

### Bounded work fails closed and never truncates

Scope: `llm_collab/`, `bin/`, `scripts/`

A partial result that *claims to be complete* is indistinguishable from a
complete one, so a bound that silently truncates converts a resource limit into
a correctness bug. The `bin/` and `scripts/` commands enumerate the same
untrusted workspace, so they are in scope, not only the library.

Safe path: begin the budget at the earliest untrusted enumeration or parse
boundary - for a directory scan, before suffix filtering - keep it cumulative
across sources within one run, and raise on exceed so the operation aborts with
no partial state.

Exempt: a result that carries its own truncation signal. A capped list returned
alongside a `*_truncated` flag (or equivalent metadata) is distinguishable from
a complete one by construction, and its truncation is part of the contract
rather than a silent loss. The rule targets results that assert completeness,
not results that declare their own bound.

Any bounded primitive that proves the same outcome is acceptable; this rule does
not prescribe one algorithm.

### Post-execution failures must suppress retry

Scope: `llm_collab/`

Once a non-idempotent task-bearing call **reports success**, or its outcome is
already ambiguous (a timeout, where the response was lost but the operation may
have run), the operation may have happened. Every downstream failure past that
boundary — decode, shape, identity, profile — must be retry-suppressing: a typed
orphan carrying the native identity when one is recoverable, otherwise ambiguous.
A clean refusal there is indistinguishable from "nothing happened", so a caller
that suppresses retries only on the ambiguous reason duplicates a real operation:
a second thread, a second enqueue.

Clean refusals are legitimate before that success-or-ambiguity boundary —
arguments, gates, an unsupported mode rejected before the call — and on read-only
paths, which performed nothing.

Native nonzero exits are outside this rule until evidence establishes their
side-effect contract.

Safe path: route every post-execution failure through one seam per call site, and
make that seam — not just its call sites — produce the retry-suppressing surface.
Routing alone is not compliance: a seam that returns a typed reason with a null
identity is still a clean refusal.

Review this invariant by enumerating every terminal branch after the success or
ambiguity boundary and naming both its surfaced exception and durable state;
finding one correct orphan branch does not prove its siblings are safe.

Tests must prove **both** sides: that an identity-carrying failure keeps its typed
reason and identity, and that an identity-less failure is ambiguous. A test for
only one side cannot tell the fix from its over-application.

This rule exists because the class cost four review cycles in one lane — envelope
validation, decode, `send()` semantic checks, then `spawn()` with no recoverable
id — each fixed as an instance while the invariant stayed unwritten.
