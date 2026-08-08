# BB Worker Profiles

Status: Phase 2 routing policy, revision 2. This is not a runtime cutover or an
executable profile registry.

## Contract

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
it must not fall back. This refusal is policy intent, not a currently
implemented selector. `pi` is a multi-vendor provider, so its model IDs retain
their vendor prefix, such as `kimi-coding/k3` or `zai/glm-5.2`.

## Routing tiers

These tiers are failure-mode lanes, not one quality ranking. The 2026-08-07
pilot used one byte-identical, read-only source-audit task per model.

| Reach for | BB model | Measured result | Disqualifying boundary |
|---|---|---|---|
| Fast, exact source analysis | `codex / gpt-5.6-luna` | 136s; 7/7 citations exact; correct judgment; clean output | Authoring and reasoning-level-specific behavior are unmeasured. Analysis only; not a sole gate. |
| Deep source analysis or a competing diagnosis | `pi / kimi-coding/k3` | 324s; about 15/15 citations exact; deepest analysis; alone caught a subtle wrong-fix direction and checked `agents.json` for empirical proof | Authoring and reasoning-level-specific behavior are unmeasured. Analysis only; not a sole gate. |
| Hard-excluded | `pi / zai/glm-5.2` | 508s; reasoning and conclusions correct | Citation coordinates drifted by 150 and 11 lines; see the canonical [exclusion rule](bb-workers.md#spawn-in-an-isolated-worktree). |
| Hard-excluded | `pi / meta/muse-spark-1.2-contributor` | About 120s; citations and judgment were initially correct | Output degenerated mid-answer: repetition, a corrupted glyph, emoji burst, and one unreadable item; see the canonical [exclusion rule](bb-workers.md#spawn-in-an-isolated-worktree). |

The timings are provisional: single runs confound capability with capacity and
usage limits. The observed failure modes are actionable—load does not explain a
150-line citation drift or degenerate glyphs. This pilot measured analysis, not
authoring, so it places no model on an implementation lane.

Prospective policy: no unmeasured or text-unstable model may own a gate, money
path, authority path, or implementation lane. This broader policy is not
enforced today; only the measured hard exclusions above are active. BB
bootstrap currently passes every first packet body to the hard-coded
`SLICE_1A_PROFILE` (`pi / kimi-coding/k3 / high`) regardless of work type; see
`llm_collab/bb_client.py:120-122` and `bin/_session_autobridge.py:1700-1712`.
An implementation delegation can therefore start the analysis-only K3 profile.
The Phase 2 selector must enforce this policy; until then, the non-excluded
measured models' advisory-only status is intent, not an activation control.

## Named profile candidates

Only non-excluded issue-approved Phase 2 names remain candidates. A live catalog
match proves availability, not capability.

| Profile revision | Frozen candidate triple | Current status |
|---|---|---|
| `manager-opus-v1` | `claude-code / claude-opus-5[1m] / medium` | Unmeasured; evaluation only. Do not use as a gate or authority owner. |
| `architect-fable-v1` | `claude-code / claude-fable-5 / xhigh` | Unmeasured; evaluation only. |
| `engineer-sol-v1` | `codex / gpt-5.6-sol / high` | Unmeasured; no implementation assignment yet. |
| `utility-luna-v1` | `codex / gpt-5.6-luna / medium` | Model measured for read-only analysis; reasoning-level-specific and authoring behavior unmeasured. |
| `research-kimi-v1` | `pi / kimi-coding/k3 / high` | Model measured for read-only analysis; reasoning-level-specific and authoring behavior unmeasured. |

## Live catalog snapshot

Observed on 2026-08-07. Everything not identified as measured above is
**unmeasured**.

- `codex`: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`,
  `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`
- `claude-code`: `claude-fable-5`, `claude-opus-5[1m]`,
  `claude-opus-4-8[1m]`, `claude-opus-4-7[1m]`, `claude-sonnet-5`,
  `claude-haiku-4-5-20251001`
- `pi`: `kimi-coding/k3`, `kimi-coding/kimi-for-coding`,
  `kimi-coding/kimi-for-coding-highspeed`, `kimi-coding/k3-256k`,
  `openai-codex/gpt-5.3-codex-spark`, `openai-codex/gpt-5.4`,
  `openai-codex/gpt-5.4-mini`, `openai-codex/gpt-5.5`,
  `openai-codex/gpt-5.6-luna`, `openai-codex/gpt-5.6-sol`,
  `openai-codex/gpt-5.6-terra`, `zai/glm-4.5-air`, `zai/glm-4.7`,
  `zai/glm-5-turbo`, `zai/glm-5.1`, `zai/glm-5.2`,
  `zai/glm-5v-turbo`, `zai/glm-5.2-highspeed`,
  `meta/muse-spark-1.2-contributor`
- `acp-cursor`: provider available, no selectable models returned

Re-query before every activation. This snapshot documents the decision evidence;
it is not entitlement authority.
