<!-- CONTRACT_VERSION: 5 -->
# AGENTS.md

## This file is the source of truth

This file plus `docs/workflows/` is the canonical worker contract. Memory files,
skills, project notes and branch-local docs must **point at it, never restate it** — a
restated command is a cached copy that goes stale without telling anyone.

Not theoretical: on 2026-07-25 all eight agent memory files still taught the
`deliver.py` invocation **without `--repo-targets`**, the exact command that had just
silently dropped 27 packets over eleven hours, and several taught `--chat last`, which
addresses the wrong lane once a second project is active.

`python bin/contract_drift.py --agent <you>` reports your own stale copies;
`session_bootstrap.py` runs it for you and prints the contract version at session start.

### Recent contract changes

Read these if your last session predates them.

- **v5 (2026-07-28)** — the merge gate gets a reachable terminal state and Tier A
  gets a contract-before-branch gate; several habits formed under v4 are now
  violations. A Tier A lane writes a one-page **lane contract** (authority
  boundary, commit point, retry behavior, non-goals —
  `docs/workflows/lane-contract.md`) **before the first branch**; reviews verify
  that contract instead of discovering it. Findings route **per-finding at
  arrival**: a finding that violates the lane contract blocks; any other finding
  is adjudicated in writing and defers to a follow-up issue while the lane ships
  — deferral is no longer cap-time-only (GH-162). **One external reviewer per
  head**: a requested connector review is the external gate for that head, and
  running a second independent model review on it consumes cycle budget without
  adding signal. The cap default becomes **merge-with-followups**;
  `descope`/`split` must now record what is being un-shipped and why merging is
  unsafe. Lanes carry a wall-clock budget — 3 amended heads or 4 hours in
  review-fix escalates to the operator as a merge-or-kill decision. Review
  requests are generated mechanically (`bin/review_request.py`); a hand-typed
  SHA is a process defect (one was fabricated and retracted on #347). At most
  **two active implementation lanes**; capped-PR fragments go to triage, not
  straight onto the board. If your session predates this, discard cached
  cap-default, deferral, reviewer-count, and request-format rules.
- **v4 (2026-07-26)** — the merge gate is rewritten and the old rules are unsafe to
  cache. The silence fallback is **deleted**: no elapsed time is ever a terminal
  signal, and nothing ripens by waiting. A review request must name the exact head
  SHA; one *initial* request per candidate final head, with a single request-anchored
  re-trigger as the only exempted recovery. A connector review **body** listing no
  findings is not a clean verdict — the findings are inline threads. Bind a finding to
  a head through `pullRequestReview.commit.oid` (falling back to
  `originalCommit.oid`), **never** the mutable `comment.commit.oid`. Adjudicate every
  **arriving finding** whatever head raised it and whatever its current resolution
  state — enumerate every thread, resolved or not, because a checklist phrased over
  *unresolved* threads cannot see one that someone clicked Resolve on without
  recording anything; a push is not an adjudication, and a written disposition must
  identify the thread *and* be validated by a human. A
  reaction counts only on the latest unedited request artifact. If your session
  predates this, discard any cached copy of the old fallback, reaction lifecycle,
  request shape, or authority rules.
- **v3 (2026-07-26)** — workers own their own setup: project registration, agent
  entries, chats, session registration, watchers and environment repair are worker work,
  not operator work. See "Workers own their own setup" for what genuinely is the
  operator's.
- **v2 (2026-07-25)** — Codex review is **manual only**: nothing arrives unless
  requested, and the Tier A/B/C rule below decides when you must ask. `--repo-targets`
  is effectively mandatory on `deliver.py`. New: `docs/workflows/collab-thread-quickstart.md`
  for the end-to-end collab path, and `bin/codex_stream.py` to watch a peer's thread live.
- **v1** — everything before that.


This repository is the shared `llm-collab` coordination runtime. It is not the
Amiga workspace, the Nuvyr workspace, or any other product repository.

## Required Reading

Before changing shared tooling or operating a project lane, read:

- `README.md`
- `docs/multi-project.md`
- `docs/workflows/session-startup.md`
- `docs/workflows/collab-thread-quickstart.md` — starting and running a collab
  thread end to end
- `docs/workflows/task-intake-and-delegation.md`
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

**Genuinely operator-owned**, and worth stopping for: anything irreversible or
outward-facing (merging, publishing, contacting someone), credentials and account
settings, accepting a risk, and scope or priority trade-offs. Those need a decision, not
a pair of hands.

The test is not "is this tedious" or "did they mention it" — it is whether you could do
it and check that it worked. If you could, it is yours.

## Adding A Project

For an existing workspace, update `projects.json` directly. Do not rerun
`scripts/init.py` unless the intent is to reinitialize the whole workspace.

1. Register a unique `id`, display name, repositories, base branch, preflight,
   and GitHub configuration. Add project-specific `ui_ux` and `db`
   configuration only when applicable.
2. Create local state at `{project_state_root}/{project_id}/`; keep real project
   state outside this public Git checkout.
3. Add repository-level `AGENTS.md` and worker guidance to the product repo.
   Bind examples and commands to the exact checkout and `--project <id>`.
4. For a GitHub-backed project, materialize and validate the project queue:

   ```bash
   bin/llm-collab project_issue_queue.py reconcile --project <id> --write
   bin/llm-collab project_issue_queue.py validate --project <id>
   ```

   Projects without GitHub integration can use the local task board without a
   GitHub-backed issue queue.

5. Create a representative project-scoped chat and task, sync its contract,
   and validate it before activating a worker:

   ```bash
   bin/llm-collab task_contract.py sync --task TASK-... --write
   bin/llm-collab task_contract.py validate --task TASK-... --stage assignment
   ```

6. Confirm that the task, queue, generated guidance, and runtime state contain
   no paths, database refs, tool surfaces, or policies from another project.

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

Two incidents established this as repo-local policy:

- PR #153 placed negated non-resolution wording adjacent to issue #135 and
  GitHub changed issue #135 to closed when the PR merged.
- PR #198 repeated the same class through the `GH-` autolink; the merge commit
  body put a closing keyword adjacent to the autolinked reference for issue 91,
  and GitHub changed GH-91 to closed.

## Requesting Code Review (all workers, every repository)

Codex code review is **manual only**. Automatic review is off account-wide, so a
review happens when a worker asks for it and never otherwise. Do **not** wait for a
bot review that nobody requested.

This section is worker-facing and applies in **every** repository a lane touches, not
only this one. It is reachable from every worker because this file is Required
Reading. It is distinct from the `## Code Review Rules` section below, which is read
by the reviewer when it reviews *this* repository and must be authored per repository
from that repository's own incidents.

**Tier A — you MUST request a review on the candidate final head.** Any change
touching credentials, authentication or an authority decision; money, provider or
idempotency paths; **input we do not control** — network peers, another project's
data, an external API, or anything a user or third party supplies — including its
resource and deadline bounds; shared code that changes an observable contract (return or exception
shape, authority or source selection, side effects, persistence, ordering,
deadline/resource behaviour, compatibility, failure handling); concurrency, ordering,
partial state, TOCTOU or atomicity; migrations, DDL, grants or RLS; a defect family
that has already produced a finding in that repository; or tests and docs that govern
or can weaken any of the above. **Failing to request is itself a gate violation.**

**Tier B — your discretion.** New feature surface or a multi-module refactor with no
Tier A contact; proof-reshaping test changes that do not cover a Tier A invariant;
normative docs for non-Tier-A behaviour.

**Tier C — do not request.** Formatting, lint or mechanical changes; non-normative
prose and comments; additive tests with no gate, fixture or baseline change;
single-caller behaviour-preserving edits.

Request with `@codex review for <focus>`, naming **every** Tier A family the diff
touches, asking for the full diff through those lenses, and **stating the exact head
SHA the request is for**. The SHA is not decoration: a connector `+1` is terminal only
while the head still equals the SHA that request named, so a request without one leaves
the reaction path unsatisfiable and there is nothing to bind the verdict to. Issue **one initial request per
candidate final head** — an amendment stales the review and needs a new request. That
limit is on *initial* requests; the single request-anchored re-trigger in
`docs/workflows/commit-push-prs.md` is an explicit exemption and the only recovery for
a request the connector silently dropped. Any
finding that arrives must be adjudicated in writing at every tier. A review is
P0/P1-scoped, so it complements and never replaces independent exact-head
verification and defect-verbatim mutation proof.

**Tier A opens with a lane contract.** Before the first branch, write one page —
template in `docs/workflows/lane-contract.md` — naming the authority boundary,
the commit point, retry behavior, and explicit non-goals, in the task or linked
issue. The review request then asks the reviewer to verify the diff *against
that contract*, and findings are routed per-finding at arrival: a finding that
violates the lane contract blocks and is fixed; a finding about a guarantee the
lane never promised defers to a follow-up issue and the lane still ships —
deferral is the default for that class, not a cap-time privilege (this restores
the GH-162 defer-first rule); a finding showing the feature would be wrong *as
specified* amends the contract once (`contract-clarified`), then blocks.
Classification is written and auditable; a deferred finding that was in fact
contract-violating is a gate violation attributable to the classifier.

**One external reviewer per head.** When a connector review has been requested
on a head, that review is the external gate for that head. The independent
exact-head obligation is discharged by the lane's required local verification
and CI — not by a second independent model review, which consumes cycle budget
without adding signal and must not be run. The mandatory pre-PR cold full-diff
review in `docs/workflows/commit-push-prs.md` is unchanged: it happens once,
before the first PR-ready head, and is where the lane contract itself gets
challenged.

**Generate review requests mechanically.** Use `bin/review_request.py --pr <n>
--focus "..."`: it reads the head SHA from GitHub and the local checkout,
refuses on mismatch, and enforces one initial request per head plus the single
exempted re-trigger. It has no option to pass a SHA by hand — a hand-typed SHA
is how #347 came to contain a fabricated, later-retracted request.

**"Untrusted" means input we do not control.** Our own workspace — `State/`,
`Chats/`, `projects.json`, the checkout itself — is not an adversary: anyone who can
write there can edit `bin/` and already has code execution as this user. Bound those
reads against *accidents* (a huge directory, a hung mount, a corrupt record) and stop
there. Hardening a local tool against a hostile local filesystem has no natural floor
and will loop forever; llm-collab#306 spent eleven review rounds proving it.

Canonical detail, including the terminal-signal rules and ownership:
`docs/workflows/commit-push-prs.md` and `docs/workflows/review-and-handoff.md`.
Policy and rationale: `llm-collab#310`.

## Code Review Rules

Path-scoped review rules for Codex Code Review. Only rules matching the changed
files fire, and findings cite the rule that produced them. This is a seed set of
three: each encodes a class that was already adjudicated in this repository and
then rediscovered at review cycle 2-3, where it forced an amendment or a
retracted CLEAN. Related GH-185.

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
