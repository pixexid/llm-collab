<!-- CONTRACT_VERSION: 5 -->
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

### Recent contract changes

Contract v5 (2026-07-28) rewrote the merge gate and the Tier A entry conditions.
If your last session predates it, discard cached cap-default, deferral,
reviewer-count and request-format rules, and read
[`commit-push-prs.md`](docs/workflows/commit-push-prs.md), which is canonical for
all of it:

- a Tier A lane writes a one-page **lane contract** before the first branch, and
  the review verifies the diff against it;
- findings route **per-finding** at arrival — contract violations and regressions
  block, pre-existing issues and broadenings defer in writing;
- **One external reviewer per head**: the requested connector review is that
  head's external gate, and a second model review must not be run;
- the cap default is **merge-with-followups**; 3 amended heads or 4 hours in
  review-fix forces a recorded merge-with-followups-or-close decision;
- review requests are generated mechanically by `bin/review_request.py`; a
  hand-typed SHA is a process defect;
- at most **two active implementation lanes**.

Earlier versions are in the git history of this file. Nothing below depends on
reading them.

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
a request the connector silently dropped.

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

Canonical detail — lane contract, per-finding routing, one reviewer per head,
terminal signals, the lane budget, and ownership — is in
`docs/workflows/commit-push-prs.md` and `docs/workflows/review-and-handoff.md`.
Which repositories are enrolled and what the rule audit found:
`docs/multi-project.md`. Policy and rationale: `llm-collab#310`.

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
