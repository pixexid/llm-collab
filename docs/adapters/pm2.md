# PM2 Watcher Adapter (Optional)

PM2-backed watchers run `watch_inbox.py` as a persistent background process per agent, polling for new messages and sending desktop notifications.

PM2 is entirely optional. You can check your inbox manually at any time with:
```bash
<runtime_root>/bin/llm-collab inbox.py --me <agent_id>
```

For Amiga collab-loop waits, Claude owns ongoing PR review, bot-review, inbox-reply,
and doorbell monitoring. Codex should prefer an attended one-shot check and hand
continuing watches to Claude instead of keeping a Codex thread heartbeat alive.
If Codex must create a monitor, use one monitor per purpose, clear stale prior
monitors first, and delete or update it as soon as its purpose is served.

Manual one-shot watcher runs use the same Codex refresh defaults as PM2:

```bash
<runtime_root>/bin/llm-collab watch_inbox.py --me codex --max-polls 1 --json
```

For Codex, both PM2 and manual watcher runs default to:

- `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp`
- `LLM_COLLAB_CODEX_CDP_PORT=9223`

---

## Requirements

```bash
npm install -g pm2
```

---

## How it works

The complete deploy lifecycle is defined in the canonical
[`Session Startup` safe refresh flow](../workflows/session-startup.md#keep-the-tooling-current):
fence before advancing the runtime, reconcile removed apps, restart the current
ecosystem, verify it, and save only after verification. The manual commands below
manage individual PM2 apps; they do not replace that deploy transaction.

`pm2/ecosystem.config.cjs` reads `agents.json` dynamically and generates one PM2
app per agent where `activation.watcher_enabled: true`. The current ecosystem
file does not filter by activation type. `human` and `human_relay` entries are
normally configured with watchers disabled, but setting their flag to `true`
will currently create a watcher.

App naming: `{workspace_name}-{agent_id}` (workspace_name from `collab.config.json`)

PM2 materializes this configuration when a process is started/reloaded; changing
`agents.json` does not automatically remove or reconfigure an already-running
process. A PM2 process saved before an agent was disabled/removed can also return
after reboot even though the current ecosystem would not create it.

After roster/watcher changes, compare `<runtime_root>/bin/llm-collab pm2_watchers.py status --all`
and `pm2 list` with current `watcher_enabled: true` entries. Stop/delete stale
named processes (`<runtime_root>/bin/llm-collab pm2_watchers.py delete --agent <id>` while the ID
remains in the roster, otherwise `pm2 delete <workspace>-<agent>`). Then apply
the current ecosystem definition to every intended watcher before saving:

```bash
pm2 startOrRestart pm2/ecosystem.config.cjs --only <workspace>-<agent> --update-env
```

Repeat that command for each intended existing watcher, or omit `--only` to
apply the current ecosystem to the full enabled set. `pm2_watchers.py ensure`
only starts a missing process; when a process is already online it does not
reload changed arguments or environment. Likewise, restarting only by stored
PM2 name does not re-read the ecosystem definition. Run `pm2 save` only after
the intended processes have been started/restarted from the current ecosystem
and status matches the roster, so reboot state preserves the reconciled
configuration. Do not treat a healthy stale PM2 process as proof that current
routing policy authorizes it.

Activation cleanup uses PM2 state as a preservation boundary. Before an
activation lease claim, the runtime may audit recurring `watch_inbox.py` pollers
for the same activation identity; any PID returned by `pm2 jlist` is treated as
an authoritative registered watcher and is preserved, not signaled. Only
unregistered matching pollers may be cleaned, and then only with verified
SIGTERM/SIGKILL exit proof. If the PM2 binary is unavailable, `pm2 jlist` fails
or times out, PM2 emits invalid/non-list JSON, cleanup is report-only, or
termination cannot be verified, the activation claim refuses rather than
mutating inbox or lease state. Tests for this path must use
`LLM_COLLAB_PS_FIXTURE`; fixture cleanup simulates termination and never signals
real PIDs.

---

## Commands

```bash
# Start all watchers
<runtime_root>/bin/llm-collab pm2_watchers.py start --all

# Ensure a specific watcher is running (start if not)
<runtime_root>/bin/llm-collab pm2_watchers.py ensure --agent orchestrator

# Check status
<runtime_root>/bin/llm-collab pm2_watchers.py status --all
<runtime_root>/bin/llm-collab pm2_watchers.py status --agent orchestrator

# View logs
<runtime_root>/bin/llm-collab pm2_watchers.py logs --agent orchestrator
<runtime_root>/bin/llm-collab pm2_watchers.py logs --agent orchestrator --lines 100

# Stop
<runtime_root>/bin/llm-collab pm2_watchers.py stop --agent orchestrator
<runtime_root>/bin/llm-collab pm2_watchers.py stop --all

# Remove from PM2
<runtime_root>/bin/llm-collab pm2_watchers.py delete --all
```

---

## Notifications

`watch_inbox.py` detects the OS automatically:

| Platform | Notification method |
|----------|-------------------|
| macOS | `osascript` display notification |
| Linux | `notify-send` |
| Other | Silent (no-op) |

Notifications can be disabled globally: `"notifications_enabled": false` in `collab.config.json`.

---

## Current retry behavior (no busy queue)

The current session autobridge does **not** obtain an authoritative Codex thread
busy/idle state and does not implement a distinct busy queue. In particular:

- `agents/<agent>/inbox.json` has `unread` and `read`; it has no autobridge
  `queued` field
- the watcher does not emit `autobridge_deferred_busy`
- an attempted runtime trigger that returns nonzero emits `autobridge_failed`
  and leaves the message unread
- each later watcher pass considers unread messages again; a successful runtime
  result emits `autobridge_consumed` and moves the message to `read`

This retry shape applies to PM2-backed and one-shot/manual `watch_inbox.py`
runs. A failure is not proof that a busy runtime rejected the turn before
acceptance, so this behavior must not be described as safe busy deferral. Avoid
targeting a running operator thread.

For settled-action diagnostic events and their response procedure, use the
canonical [Session Autobridge Runbook](../workflows/session-autobridge-runbook.md#settled-diagnostic-failures).

The planned [Thread Event Runner](../workflows/thread-event-runner-rfc.md)
defines transactional busy deferral, coalescing, leases, and ambiguous-delivery
reconciliation. None of those guarantees are implemented by the current PM2
watcher.

---

## Desktop-app constraint and wake priority

PM2/watcher automation must not be treated as the controller for a desktop app.
PM2 can watch `llm-collab` inbox files and dispatch configured shell/runtime
adapters, but it cannot perform Codex Computer Use recovery.

Current safe ordering:

AX applies only to the Codex recipient, and only as a fallback. Every
watcher-backed worker — Codex included — uses its durable packet and background
watcher as the routine wake.

- never deactivate a dispatchable session autobridge in order to obtain an AX
  wake; `deliver.py` gives `autobridge_ready` precedence and suppresses
  `ax_doorbell_required` by design, and removing routine dispatch to reach a
  fallback inverts contract v12
- write the durable `llm-collab` packet and inspect the delivery result; when it
  reports `autobridge_ready: true`, the current Phase 1 route is session
  autobridge, not AX
- only when it reports `ax_doorbell_required: true`, run exactly the command
  `deliver.py` prints once, even when the recipient is busy. Do not prove the
  composer empty first: for Codex, composer content and `AXValue`
  readability/opacity are never a hold, and busy alone is not a hold either —
  the ring clears and overrides whatever is in the composer and sends. Only a
  genuine targeting/operation failure (no or ambiguous native composer target,
  a non-Codex or unrecognized profile, an AX-trust failure, a
  clear/type/submit failure, or post-submit identity loss) means hold and
  enter attended recovery. `VERIFIED` exit 0 confirms delivery;
  `QUEUED (UNCONFIRMED)` exit 0 preserves the mailbox/blocker follow-up but does
  not prove exact-thread delivery and must not be re-rung
- use attended Computer Use only as fallback/recovery when AX cannot safely
  inspect/target/send; apply the idle input gate before this screenshot/keyboard
  fallback
- PM2/heartbeat remains a bounded observation safety-fuse, not the primary wake

Why:

- Claude desktop visible threads depend on app-managed Electron storage under:
  - `~/Library/Application Support/Claude/IndexedDB/...`
  - `~/Library/Application Support/Claude/Session Storage/...`
- Claude CLI/project sessions live under:
  - `~/.claude/projects/<project-slug>/...`
- Writing the CLI/project session store does not guarantee that a new thread appears in the desktop app sidebar

Watcher policy for desktop-app agents:

PM2/heartbeat is only the bounded, provisional safety-fuse described in
`session-autobridge-runbook.md`. AX may target only Codex, and only as the
fallback `deliver.py` selects; every watcher-backed worker, Codex included, owns
its own pickup through routine exact-session dispatch.

- fallback for a Codex recipient only, and only when `deliver.py` prints it: run
  the exact command it prints
  once, even while it is busy, with one short pointer to
  the durable packet. Do not prove the composer empty first: composer content
  and `AXValue` readability/opacity are never a hold, and busy alone is not a
  hold either — the ring overrides and sends. Only a genuine targeting/operation
  failure (no or ambiguous target, a non-Codex or unrecognized profile, an
  AX-trust failure, a clear/type/submit failure, or post-submit identity loss)
  means hold and attended recovery. `VERIFIED` exit 0 confirms delivery;
  `QUEUED (UNCONFIRMED)` remains unresolved, preserves the mailbox/follow-up,
  must not be re-rung, and cannot be reported as exact-thread delivery
- recovery: if AX targets an embedded preview/web field or cannot verify the
  native composer, preserve the packet, stop sending, and use an attended Codex
  turn with Computer Use plus
  `bin/axsend-ensure tree --app <app> --editable-only` to remove/blank the
  competing field and verify the real native prompt before resuming AX
- fallback: use Computer Use to send only when AX remains unavailable/unsafe;
  apply the Computer Use idle input gate and one-line pointer rule
- never convert one AX targeting incident into a standing mailbox-only or
  AX-disabled policy
- unsafe: claim a PM2 watcher created a new app-visible desktop thread
- unsafe by default: synthesize sidebar visibility by writing app cache/index files directly
- unsafe: use `claude -p`, `claude --resume`, or `~/.claude/projects` as proof
  that the visible desktop thread changed
- unsafe: ask the operator to wake an agent or paste the bridge prompt before
  AX plus attended Computer Use/app-control recovery has been exhausted

If desktop visibility is needed, the recommended flow is:

1. write the task/message to `Chats/` with `deliver.py`
2. ring the recipient's registered app via AX once, even if it is busy, with one
   short sender-tagged pointer to the durable packet. Do not prove the composer
   empty first: for Codex, composer content and `AXValue` readability/opacity
   are never a hold, and busy alone is not a hold either — the ring overrides
   and sends. Only a genuine targeting/operation failure means hold and
   attended recovery. Record `VERIFIED` exit
   0 as confirmed delivery. Record `QUEUED (UNCONFIRMED)` exit 0 as unresolved,
   preserve the mailbox/blocker follow-up, never re-ring it, and do not claim
   exact-thread delivery
3. the recipient drains its unread inbox and acts; it rings back on handoff
4. if AX targets the wrong editable surface or cannot verify delivery or
   identity, run the attended Computer Use recovery above. Resume AX only
   after the real composer's **target** identity is resolved and unambiguous;
   an opaque or unresolvable target stays on the attended path. Use Computer
   Use send only as the bounded fallback
5. only if the ring is blocked or a running worker's response is expected, create
   a bounded provisional safety-fuse heartbeat
6. while the target is running, the heartbeat observes only; delete it when the
   response is recorded, blocked, timed out, or no longer needed

---

## Disposable retry test

Use a disposable runtime adapter/session for retry validation. Do not target an
active operator thread and do not treat this as a busy-deferral test.

Current test shape:

1. register a disposable autobridge session with a bounded test adapter
2. deliver one message to that exact disposable target
3. make the adapter return nonzero on the first watcher pass
4. confirm `autobridge_failed` and that the message remains in `unread`
5. make the adapter return success on a later watcher pass
6. confirm `autobridge_consumed`, `unread: []`, and the message in `read`

This proves failure retry and eventual consumption only. Authoritative Codex
busy detection, coalescing, and no-duplicate delivery remain future runner
integration gates.

Useful inspection points:

- `agents/codex/inbox.json`
- `Logs/watchers/codex.pm2.out-1.log`
- `State/session_autobridge/events/<session>.jsonl` (diagnostic event evidence)
- `State/session_autobridge/events/wake/<session>.jsonl` (Pi wake-only stream)

---

## Survive system reboots

```bash
pm2 startup    # follow the printed instructions
pm2 save       # save current process list
```

After this, PM2 and all running watchers restart automatically on reboot.

---

## Log locations

Apps defined by `pm2/ecosystem.config.cjs` write to
`Logs/watchers/{agent}.pm2.{out,err}.log`. Apps started ad hoc without explicit
`out_file` and `error_file` settings use PM2's default `~/.pm2/logs/` directory,
so inspect both locations when accounting for disk usage.

These files are gitignored.

### Rotation and retention

Use one `pm2-logrotate` instance for both locations. The module follows each
active PM2 app's resolved output and error paths, including explicit ecosystem
paths and PM2 defaults:

```bash
pm2 install pm2-logrotate
pm2 set pm2-logrotate:max_size 10M
pm2 set pm2-logrotate:retain 7
pm2 set pm2-logrotate:compress true
pm2 set pm2-logrotate:rotateInterval '0 0 * * *'
```

The retention policy is the current file plus seven gzip-compressed generations
per PM2 log, with a 10 MiB size trigger and a daily rotation. Ordinary logs keep
about one week of history; a noisy process may rotate sooner, deliberately
bounding a crash loop instead of guaranteeing seven days at any write rate.

Existing logs need an operator-owned disposition before rotation is enabled.
Archive any history needed for diagnosis, especially unique crash-loop evidence,
outside the live PM2 paths; do not blindly delete or truncate it. The module
follows active app paths, so separately inspect orphaned files left by deleted
PM2 entries.

## Codex app-server delivery sidecar

PM2 also manages the Codex delivery transport, not just inbox watchers.

`bin/_session_autobridge.py` delivers to a Codex worker over the App Server
(`initialize` → `thread/resume` → `turn/start`). `discover_codex_app_server()`
only resolves a target launched with `--listen ws://`. The ChatGPT desktop app
runs its own app-server on `--listen stdio://`, which no external process can
connect to, so without this sidecar the delivery path can never find an endpoint
and every packet falls back to the AX doorbell.

The sidecar shares the desktop app's `CODEX_HOME`, so it reaches the same
threads and the visible app keeps running.

The `turn/start` input is deliberately only the short packet ring used by AX,
for example `[from claude] Read latest codex packet in CHAT-...: packet.md`.
The packet body and bootstrap instructions stay in the durable mailbox; the App
Server still creates a normal visible turn because it has no passive ring API.

### Enabling it

The token file's presence is the enable switch — there is no config flag:

```bash
mkdir -p .secrets
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > .secrets/codex_app_server_ws_token
chmod 600 .secrets/codex_app_server_ws_token
```

`.secrets/` is gitignored. Mode `600` matters: the token authorizes turns on the
operator's real Codex account.

Overrides, all optional:

| variable | default |
|---|---|
| `LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE` | `.secrets/codex_app_server_ws_token` |
| `LLM_COLLAB_CODEX_BIN` | `/Applications/ChatGPT.app/Contents/Resources/codex` |
| `LLM_COLLAB_CODEX_HOME` | `~/.codex` |
| `LLM_COLLAB_CODEX_APP_SERVER_PORT` | `8767` |

The listener binds `127.0.0.1` only.

### Lifecycle

Addressed as `codex-appserver` through the normal manager, and included in
`--all` whenever the token file and the Codex binary both exist:

```bash
<runtime_root>/bin/llm-collab pm2_watchers.py start --agent codex-appserver
<runtime_root>/bin/llm-collab pm2_watchers.py status --agent codex-appserver
# NOTE: `restart` reuses PM2's stored definition and does NOT re-read this config,
# so changing CODEX_HOME, the port, the token path, or the binary needs a reload:
pm2 startOrRestart pm2/ecosystem.config.cjs --only <workspace>-codex-appserver
<runtime_root>/bin/llm-collab pm2_watchers.py logs --agent codex-appserver
<runtime_root>/bin/llm-collab pm2_watchers.py stop --agent codex-appserver
```

`status` reports `[sidecar] target=codex-appserver (no AX surface)` for it, and
deliberately not an `[ax]` line: that prefix is the per-agent AX capability
contract consumers parse, and a transport sidecar has no Accessibility surface.
That is the point of it.

Persist the process list so it survives a reboot:

```bash
pm2 save
```

### Verifying and using it

```bash
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:8767/readyz)" = 200
<runtime_root>/bin/llm-collab pm2_watchers.py ensure \
  --agent codex-appserver --runtime-home <exact-canonical-runtime-home>
<runtime_root>/bin/llm-collab pm2_watchers.py status --agent codex-appserver
```

Only an exact HTTP 200 is ready; redirects and HTTP errors are refused. Before
`ensure` succeeds for a binding, it also authenticates a WebSocket `initialize`
and requires the returned `codexHome` to equal the exact canonical runtime home
that registration will store.

Delivery itself is exercised by the autobridge dispatch path, which reports
`adapter: codex_app_server` with `returncode: 0` once a session declares the
exact runtime home. (A dedicated observation/control CLI —
`status`/`tail`/`send`/`steer`/`interrupt` — is queued separately and is not
required to verify this transport.)

A session must declare the exact runtime home for delivery to resolve:

```bash
python bin/session_autobridge.py register --session <id> --agent codex \
  --project <project> --chat <chat> --repo-target <repo> \
  --mode auto-read --status active --wake-strategy runtime_trigger \
  --runtime-family codex_app --runtime-session-id <native-thread-id> \
  --runtime-home ~/.codex
```

`--mode auto-read` is **required**, not cosmetic. `--mode` defaults to `manual`,
and `resolve_effective_action()` selects `manual_noop` *before* it considers
`wake_strategy=runtime_trigger`. A session registered without it still looks
dispatchable to `deliver.py` — which suppresses the AX fallback — while the
watcher marks each packet processed without ever calling the App Server. The
result is silently dropped messages.

The native thread id must be the worker's own exact id, self-reported. Do not use
`publish-current` for this: it refuses `codex_app`, `claude_app`, and
`gemini_cli` precisely because disk discovery is heuristic and can resolve a
stale session from an unrelated project.
