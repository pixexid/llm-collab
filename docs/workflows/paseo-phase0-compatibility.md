# Paseo Phase 0 compatibility evidence

This is the Phase 0 evidence record for Related #458. It is intentionally not
an adapter design or a production runtime change.

## Scope and setup

The live observations used Paseo CLI/daemon `0.2.5` from the installed app,
rebased against the current `origin/main` baseline
`78af2f6c752e4db7c882f3c5a1e9117faa9efb28`. Every agent was a fresh
Paseo-managed test agent in a disposable repository. The final provider run
used Paseo provider `pi`; it did not reuse, register, or mutate an
LLM-Collab/Pi worker.

Use `scripts/paseo_phase0_probe.py` to create a temporary home and repository,
select a free loopback port, export `PASEO_HOME`, and start a foreground daemon
with relay/MCP/web UI disabled. Cleanup owns only that exact temporary process
group and temporary directory. The existing desktop-managed daemon on
`127.0.0.1:6767` is outside this workflow. The environment variable matters:
passing only a `--home` option was observed to attach to the desktop daemon.

The committed fixture is redacted and version-labelled:
`tests/fixtures/paseo_phase0_v0_2_5.json`.

## Measured behavior

- `inspect` accepts a full UUID and returns uppercase-key JSON (`Id`, `Provider`,
  `Status`, `PendingPermissions`, and related fields). `ls --json` returns a
  separate lowercase-key shape. The installed CLI also resolves a unique short
  prefix, but refuses an ambiguous `c` prefix with `INSPECT_FAILED`; a future
  adapter must require the exact full ID and fail closed on anything else.
- The observed lifecycle states were `running` and `idle`, each with an empty
  `PendingPermissions` array. `permit ls --json` also returned `[]`; no positive
  permission prompt was observed in the provider run. The parser accepts the
  installed permission shape (`PendingPermissions` non-empty) and classifies
  unknown shapes as `unknown`, so Phase 0 makes no unsupported permission claim.
- While the Pi agent was running a long shell turn, a second
  `send --no-wait --json` returned `status: "sent"`. The resulting timeline
  showed the original command did not complete before the replacement marker;
  the original marker was absent and the replacement marker completed. This is
  interrupt/replace behavior, not a proven queue guarantee.
- `send --no-wait --json` returned only `agentId`, `status`, and `message`.
  A normal waited send returned `status: "completed"`, but no run ID, message
  ID, or timeline event ID. The fixture therefore records no stable exact
  per-send correlation. A future adapter cannot safely reconcile an external
  event to a later timeline item from these responses alone.

## Failure boundary and decision

Daemon refusal, missing-agent lookup, and agent-creation failure are provably
before submission. A `sent` response is only best-effort hand-off evidence;
timeout or disconnect after a send may be after acceptance. Native
`completed` is not a canonical external-event acknowledgement.

Decision: proceed to a future adapter seam only as a synchronous, exact-ID,
fail-closed, best-effort hand-off until a stable correlation/ack mechanism is
proven. Do not retry an ambiguous post-send failure automatically. Phase 0
adds no adapter, watcher, queue, routing, binding, manifest, registry, or
canonical-write code.

## Reproducible checks

```bash
python3.11 scripts/paseo_phase0_probe.py
python3.11 -m unittest tests.test_paseo_phase0_probe
python3.11 -m unittest discover -s tests
git diff --check
```
