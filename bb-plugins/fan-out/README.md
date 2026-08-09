# bb-plugin-fan-out

Custom bb plugin — the **orchestration/fan-out** plugin from GH-630.

## Definition (this README owns it)

The plugin exposes one capability: `bb fan-out run` starts a bounded number of
**read-only agent turns**, each with a separately provisioned BB managed
worktree, and reports success only when every requested agent completes.

Isolation is part of the result contract: every returned agent must have a
distinct environment id and a distinct non-empty branch name. The plugin
asserts both sets are the requested size; a duplicate is failure.

Fail closed is part of the result contract: a spawn error, failed thread,
missing/empty result output, timeout, unusable environment, or any other agent
failure rejects the whole run and names the agent. There is no partial-success
result and no list containing holes. A successful result contains exactly one
non-empty output for every requested agent, and its `count` equals the requested
count. Exit 2 preserves `bb_spawn.py`'s do-not-retry classification and native
thread identity; callers must not retry that fan-out automatically.

The command does not queue, dispatch, retry, persist tasks, or veto other BB
spawns. BB plugin handlers cannot veto a spawn; this plugin only owns its own
fan-out command.

## Usage

The operator installs/configures the plugin separately; this repository change
does not install, enable, reload, or configure it on a live server.

```text
bb fan-out run --project ID --count N --prompt TEXT \
  --provider PROVIDER --model MODEL --reasoning-level LEVEL \
  --base-sha 40_HEX_SHA [--repo-target ID] \
  [--permission-mode accept-edits|auto|full]
```

Every child spawn is routed through this repository's canonical
[`bin/bb_spawn.py`](../../bin/bb_spawn.py) adapter, never directly through the
SDK. It applies the repository's profile exclusions, requires and verifies the
exact base SHA, and records the assignment. The plugin resolves the configured
absolute `python3.11` and `bb_spawn.py` paths before launching that child.
The canonical adapter also supplies the execution profile provenance that BB
validates against its execution event; this plugin adds no second provenance
mechanism.

`--base-sha` must be the exact 40-hex SHA that the canonical adapter verifies.
The prompt is the read-only assignment; the plugin additionally retrieves and
requires non-empty thread output, then proves the worktree is available, clean,
still on its original base SHA, and still on its returned branch. Workspace
status requests carry the run deadline and abort signal. Choose the least-permissive provider
permission mode that supports the assignment (`accept-edits` by default).

The authoritative API surface is the generated
[`types/bb-plugin-sdk.d.ts`](types/bb-plugin-sdk.d.ts), produced by `bb plugin
types`; the implementation uses only documented waiting, stopping, and
workspace-status surfaces from that SDK.

## Verification

From this directory:

```text
bb plugin types --check
npm run typecheck
npm test
```

The repository-wide gate remains:

```text
python3.11 bin/verify.py
```
