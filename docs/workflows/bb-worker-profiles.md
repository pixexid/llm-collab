# BB Worker Profiles

Status: Phase 2 routing policy, revision 2. This is not a runtime cutover or an
executable profile registry.

## Contract

Worker-authored control prose and identifiers — code comments and identifiers,
commit messages, task notes, handoffs, and documentation prose — are
English-only. This binds the worker's **voice**, not every byte in an artifact
or any particular worker. Bind the constraint to the control language it
protects, not to the whole artifact or the actor most likely to drift: an
artifact-wide rule turns subject matter into a violation, while an actor-scoped
rule silently exempts everyone else.

Load-bearing project content is protected, not merely tolerated. Product and
localized strings, test data, and fixtures that exercise non-ASCII behavior
remain in their required languages and scripts; never translate, delete, or
weaken them to satisfy this rule.

That scope has its own provenance: on the day the GLM trait migrated, a non-GLM
model emitted a stray CJK character into worker-authored control prose in this
workspace. One typo is not a trend and adds no other model to the exclusion
list; it is the existence proof that the constraint belongs to the worker's
voice rather than one model profile.

Each execution assignment gets one BB thread and one frozen
`(provider, model, reasoning_level)` triple. The field and CLI flag use BB's
native names: `reasoning_level` and `--reasoning-level`. A different model,
escalation, or independent review gets a new assignment and thread; never
substitute a model silently or switch one into an existing worker session.

Keep these identities separate:

```text
stable agent identity
  -> versioned worker profile
  -> exact BB thread/environment binding
  -> immutable task-execution snapshot
```

Worker profiles do not belong in the conversation-binding key and do not reuse
`capability_profile_id`. Existing agent IDs remain stable.

Before starting a worker, query the machine that will run it. Use its environment
when one already exists:

```bash
bb provider list --environment <environment-id> --json
bb provider models <provider-id> --environment <environment-id> --json
```

Use `--machine <id-or-name>` instead when no environment exists. With neither
selector BB intentionally queries the primary machine, which may not be the
execution host.

A Phase 2 selector must require the exact `provider`, `model`, and
`reasoning_level` to be present and refuse as `profile_unavailable` otherwise;
it must not fall back. Two paths can start an authoring assignment, and they are
deliberately governed by different rules. On the inbound activation path, an
arriving packet supplies the assignment with no human in the loop. BB bootstrap
resolves the profile from its own policy and admits the assignment only when
that profile belongs to the qualified set defined by
`AUTHORING_QUALIFIED_PROFILES` in `llm_collab/bb_bootstrap.py`; otherwise it
refuses as `bb_bootstrap_profile_unavailable`, fail closed. A partial or
malformed activation marker refuses as the distinct
`bb_bootstrap_malformed_activation` before execution.

The explicitly selected path is deliberately not gated on qualification. Its
`plan_spawn` seam in `llm_collab/spawn_gate.py`, reached through
`bin/bb_spawn.py`, enforces the Contract v15 hard model exclusions and the
isolation, exact base-SHA, registry, and scope checks. It also refuses every
non-supervisor Fable spawn and every Claude Code worker/reviewer triple except
exact `claude-code / claude-opus-5 / medium`. This is a decision, not an
oversight: an orchestrator decided
both to start the specific lane and which exact provider, model, and reasoning
level it runs on, and that lane's output passes through the review controls in
[`commit-push-prs.md`](commit-push-prs.md). The native `bb thread spawn` command
is not this assignment-spawn seam and remains forbidden as an alternate path.

On the explicitly selected path, the orchestrator must name the exact provider,
model, and reasoning level in the assignment, prefer a measured profile, and
record the executed triple by reading it back from the execution event, never
from a declared default. The qualified set is defined by
`AUTHORING_QUALIFIED_PROFILES` in `llm_collab/bb_bootstrap.py`; this contract
states the rule and the check without treating a runtime value as authority.
There is no per-packet profile selection on the inbound path. `pi` is a
multi-vendor provider, so its model IDs retain their vendor prefix.

Every sanctioned spawn therefore supplies all three CLI flags explicitly. Its
first turn is profile-only and contains no task content. Only after the BB
execution record proves the exact triple may the orchestrator send the frozen
task-bearing brief to that same thread. Requested flags, project defaults,
profile names, chips, and worker self-reports are not execution evidence. Query
the live provider catalog at activation time; do not copy it into policy.

## Routing tiers

These tiers are failure-mode lanes, not one quality ranking. The 2026-08-07
pilot used one byte-identical, read-only source-audit task per model.

| Reach for | BB model | Measured result | Disqualifying boundary |
|---|---|---|---|
| Fast, exact source analysis | `codex / gpt-5.6-luna` | 136s; 7/7 citations exact; correct judgment; clean output | Authoring and reasoning-level-specific behavior are unmeasured. Analysis only; not a sole gate. |
| Deep source analysis or a competing diagnosis | `pi / kimi-coding/k3` | 324s; about 15/15 citations exact; deepest analysis; alone caught a subtle wrong-fix direction and checked `agents.json` for empirical proof | Authoring is measured and qualified at `high` only (GH-596/GH-705); other reasoning levels remain unmeasured. Not a sole gate. |
| Hard-excluded | `pi / zai/glm-5.2` | 508s; reasoning and conclusions correct | Citation coordinates drifted by 150 and 11 lines; see the [profile-specific risk](#glm-52-reasoning-language-drift), [re-trial criteria](https://github.com/pixexid/llm-collab/issues/755), and canonical [exclusion rule](bb-workers.md#spawn-in-an-isolated-worktree). |
| Hard-excluded | `pi / meta/muse-spark-1.2-contributor` | About 120s; citations and judgment were initially correct | Output degenerated mid-answer: repetition, a corrupted glyph, emoji burst, and one unreadable item; see the canonical [exclusion rule](bb-workers.md#spawn-in-an-isolated-worktree). |

<a id="glm-52-reasoning-language-drift"></a>

**`zai/glm-5.2` — reasoning-language drift.** GLM models can drift into Chinese
in internal reasoning even while visible output stays English. The
[fleet control-prose rule](#contract) applies to this profile. A worker on this
profile should think in English and switch back immediately on noticing drift.

Provenance: amiga `ZCODE.md` "Language Discipline", carried while zcode ran as a
separate app harness. Migrated to fleet scope 2026-08-10 because it is a property
of the model wherever it runs.

[GH-755](https://github.com/pixexid/llm-collab/issues/755) defines the re-trial
grading criteria and makes reasoning-language drift a pass/fail gate alongside
source-coordinate fidelity.

The timings are provisional: single runs confound capability with capacity and
usage limits. The observed failure modes are actionable—load does not explain a
150-line citation drift or degenerate glyphs. This pilot measured analysis, not
authoring. A later three-run authoring bake-off (GH-596) qualified exactly
`pi / kimi-coding/k3 / high`, which GH-705 admits on the bootstrap path; every
other coordinate here remains analysis-only.

Prospective policy: no unmeasured or text-unstable model may own a gate, money
path, authority path, or implementation lane. The measured hard exclusions
above are active, and the bb bootstrap now enforces the fail-closed half for
implementation lanes (GH-596): `classify_first_delivery_assignment` in
`llm_collab/bb_bootstrap.py` reads a first delivery's activation markers
(`activation`/`worktree`/`branch`, carried through `bin/watch_inbox.py`
`_bb_first_packets` preserving presence) and `execute_bb_bootstrap_plan` acts at
the profile-resolution seam, before any ledger write or spawn. Only a packet
with **no** activation marker of any kind may take the read-only launch on
`SLICE_1A_PROFILE` (`pi / kimi-coding/k3 / high`). A complete, well-formed
writer-lane identity (`activation: true` plus a canonical-absolute `worktree`
and a non-blank `branch`) is admitted only for that exact qualified profile;
other resolved profiles refuse as `bb_bootstrap_profile_unavailable`. Any
**partial or malformed** marker (an
activation marker without `activation: true`, a blank/non-string worktree or
branch, or a worktree that is relative/home-relative or not in canonical lexical
form) refuses as the distinct `bb_bootstrap_malformed_activation`, never
launched, per the schema's requirement that a malformed activation marker fail
closed before execution rather than be treated as an ordinary message. The
`to`/target-agent match and worktree existence remain the activation-authority
lane, which holds the claiming-target context this classifier does not. The
classification keys on the markers, never the packet body: a guard bound to how
a caller phrased the prompt is the wrong proxy, and a writing delegation cannot
grant a lane without those markers. Work-type → profile routing and authoring
evaluation remain Phase 2; an explicitly selected writing lane through
`bin/bb_spawn.py` operates under the orchestrator review controls in
[`bb-workers.md`](bb-workers.md), not a claimed property of the model.
