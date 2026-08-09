# GH-159 — Threat model and capability/migration gates for activation leases

Design only. This document authorizes no implementation, no lease-behavior change, no
production activation, and no PR against the lease authority. It discharges the six
acceptance criteria of GH-159 as a design contract that a later implementation lane can
be reviewed against. It contains no secret material of any kind — per criterion 4, it
describes where secrets live without ever containing one.

Scope boundary (from the issue): this is not part of Amiga GH-1571's frozen cooperative
binding contract and does not reopen PR #158. It is shaped inside the GH-85 standalone
bus program.

Evidence base: `bin/_activation_lease.py`, `bin/session_autobridge.py`,
`bin/_session_autobridge.py`, `llm_collab/daemon/server.py`,
`tests/test_activation_lease.py`, `docs/standalone-agent-session-bus-plan.md`, and
GH-85 / GH-94 / GH-159 as of `origin/main` `66b1f2a`.

## 0. What GH-94 settled, and what it left to this design

The issue and the activation both treat "the #94 integration decision" as open. It is
not. GH-94 closed on 2026-08-08 as `superseded-by-bb`.

**What #94 settled:**

- The planned successor that would have consumed and retired the v2 activation lease —
  the canonical Codex-app delivery provider (`llm_collab/canonical/codex_delivery.py`
  wired behind the #92/#93 provider boundary) — is dead work. The Codex desktop app is
  no longer a worker surface; bb hosts workers. Nobody is building a second lease/fence
  authority. The coordination risk the issue worried about ("the standalone bus does
  not create a second competing authority") has no live counterparty.
- #94's one normative contribution survives as a requirement, not an implementation:
  *never run two authoritative lease/fence systems for the same target.* That
  constraint is adopted verbatim into criterion 6 below.
- bb fills the runtime-adapter slot of GH-85 (the #562 ruling); llm-collab keeps bus
  semantics — canonical messages, identity, routing, and its mutation gates. The v2
  activation lease is one of those gates, so hardening it remains llm-collab work
  rather than bb work.

**What #94 left open (and this document must therefore decide):**

- The actual migration/retirement mechanics for the v2 activation lease were never
  designed against current main. #94's cutover checklist (freeze, inventory, pause old
  writer, one new owner under lock, reconcile, quiet period, retire) was written for a
  provider successor that no longer exists. Criterion 6 re-targets those mechanics at
  the real destination: the same lease storage engine moved behind the single
  `llm-collabd` authority.
- Whether the lease's mutation surface survives the bb migration at all. See Open
  question O1 — that is a sequencing decision this document names but cannot settle
  with the evidence available.

## 1. Criterion 1 — Threat model: cooperative binding versus hostile same-account impersonation

### 1.1 What the current system is

`bin/_activation_lease.py` is the sole lease/fence authority. A lease record binds a
label tuple (project/chat/task/worktree/branch/agent, hashed to `lease_key`) plus
`owner_session_id`, `owner_runtime_session_id`, `owner_pid`, a monotonic
`fence_token`, and a TTL. `claim_lease` / `assert_lease` / `release_lease` /
`with_lease_fence` all route through `validate_lease_and_claimant`, which trusts:

- `owner_session_id` — a registered session record (copyable; `lease-show` prints it);
- `claimant_runtime_id` — a runtime/session label compared by string equality
  (copyable; exposed by `lease-show` and by the runtime env vars
  `CODEX_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` / etc.);
- `owner_pid` — checked by `os.kill(pid, 0)` liveness only. The caller *supplies* the
  pid; nothing proves the caller *is* that process. `PermissionError` is treated as
  alive.

The idempotent reclaim path inside `claim_lease` is the weakest door: when the existing
lease has no recorded pid, `runtime_only_reclaim` lets any claimant presenting the same
`owner_session_id` and same runtime label re-claim at the same fence. Copied labels
alone satisfy it.

`flock` gives mutual exclusion and the fence token rejects stale generations; both are
sequencing, not authentication. The persisted `caller_capability_binding` seam
(`CallerCapabilityBindingV1`, scheme `injected_verifier_sha256`) exists but is
default-disabled: no CLI or runtime path can produce a `CallerCapabilityVerification`,
and the type is deliberately unconstructible outside a verifier.

### 1.2 Cooperative identity binding (in scope, kept as-is)

All processes are honest but fallible: two lanes collide on a worktree, a stale watcher
asserts after its session died, a reclaimed lease refreshes a TTL. Labels, fences,
locks, TTL, and liveness already handle this. **This design changes none of it.** The
cooperative contract — including Amiga GH-1571's frozen semantics — remains exactly
what it is today. Capability proof is additive: it is checked *after* the existing
label/fence validation, never instead of it.

### 1.3 Hostile same-account impersonation (in scope, the gap to close)

The adversary is a process running **under the same UID**, **without** kernel,
debugger, or credential-store privileges, that can:

- read everything `lease-show` prints and read the lease JSON files directly (the
  state root is same-UID-readable — anything stored there is public to this
  adversary);
- read another process's argv (`ps`) and environment (`/proc/<pid>/environ` on Linux,
  `ps eww` on macOS) — so argv and env are also public to this adversary;
- invoke the lease CLI or call the library with copied labels, copied session id,
  copied runtime id, and the owner's *live* pid (the liveness check passes — it is the
  owner's real pid);
- replay anything it can observe and race claims;
- occupy a recycled PID after the real owner exits.

**Required property:** such a process must not be able to claim over, assert as,
release, or reclaim a lease it does not own. Copied labels plus a copied live PID must
fail closed.

### 1.4 Deliberately out of scope

- **Root, kernel, or same-UID with debug privileges** (`ptrace`, `task_for_pid`,
  `DYLD_INSERT_LIBRARIES` into the owner, reading the owner's memory). A same-UID
  debugger *is* the owner for any userspace scheme; no design at this layer survives
  it, and pretending otherwise would be dishonest.
- **Availability / denial of service.** A same-UID process can already `kill` the
  owner, starve the flock, or delete the state root. We protect *unauthorized
  mutation*, not uptime.
- **Network adversaries.** The verifier channel is a same-user local Unix socket; the
  existing daemon already refuses non-same-UID peers before parsing.
- **Compromise of `llm-collabd` itself**, and compromise of the runtime vendor's own
  channels (what the agent runtime does with its session is the runtime's problem).
- **Amiga GH-1571's cooperative contract** and PR #158.

## 2. Criterion 2 — Selected capability, rejected alternatives, and the cost

### 2.1 Selection

**A caller-held, per-lease 256-bit random capability, generated by and existing only in
the owning process's memory, proven by HMAC-SHA-256 challenge–response over the
`llm-collabd` same-user Unix control socket. Only `sha256(capability)` is ever
persisted, bound to `(lease_key, fence_token)` — exactly the existing
`CallerCapabilityBindingV1` / `injected_verifier_sha256` seam.**

Concretely, at the design level:

1. The claimant generates 256 bits from the OS CSPRNG inside its own process. The value
   is never written to disk, argv, env, logs, or any packet.
2. At claim (or rebind), the claimant sends the *digest* `sha256(capability)` over the
   daemon socket. The daemon records `CallerCapabilityBindingV1{version: 1, scheme:
   "injected_verifier_sha256", lease_key, fence_token, proof_digest}` — the persisted
   shape that `bin/_activation_lease.py` already validates today.
3. Every claim-over, assert, release, and reclaim request then carries a proof: the
   daemon issues a fresh single-use random challenge per request; the caller answers
   `HMAC-SHA-256(capability, challenge ‖ canonical-request)` where the canonical
   request includes `lease_key`, the current `fence_token`, the operation, and a
   daemon-supplied nonce. The daemon verifies against the stored digest. The raw
   capability never crosses the wire; a captured response is valid for exactly one
   request.
4. Kernel peer credentials on the socket (the daemon's existing `peer_uid`, extended
   where the platform supplies a peer pid) bind the channel as **defense in depth**,
   never as the root of trust.

Why this cannot be recreated by copying CLI strings: the proof input exists only in
one process's memory. Nothing the adversary of §1.3 can read — `lease-show`, lease
JSON, argv, env, logs — contains it or anything that derives it. The persisted digest
is a one-way commitment; the per-request challenge makes observed responses
non-reusable.

### 2.2 Rejected alternatives

**Rejected: OS attestation alone (kernel peer pid + process start time, or pidfd).**
A Unix-socket peer pid is kernel-supplied and unforgeable, which genuinely fixes the
self-asserted-pid flaw. Rejected as the *sole* root for three reasons: (i) no pidfd
equivalent exists on macOS, so the scheme diverges per platform at exactly the
trust-critical point; (ii) it has no continuity anchor — when the owner restarts
mid-lease, the restarted true owner and an attacker both present fresh pids, and the
reclaim path (the one criterion 3 warns is easiest to attack) cannot tell them apart;
(iii) the issue's own analysis already adjudicated it insufficient alone. We adopt the
strong half (kernel peer credentials on the channel) as defense in depth.

**Rejected: inherited anonymous fd + digest (the issue comment's rank-1 transport).**
The digest-binding half is what §2.1 keeps; the fd-inheritance transport is rejected
because it requires the verifier to be the *spawning parent* of every worker. Under
the bb migration that is false: bb spawns and resumes workers, llm-collab does not.
And on Linux a same-UID process can re-open another process's pipe/socket fds via
`/proc/<pid>/fd/` — the handle is copyable by exactly the in-scope adversary.

**Rejected: a secret stored anywhere durable** (lease file, sidecar file, keychain
item readable without UI, env var, CLI flag). All are readable by the §1.3 adversary.
This is the failure mode the whole design exists to avoid; the digest-only persistence
rule is what makes the existing seam honest.

### 2.3 What it costs

- **Lease mutation requires a running `llm-collabd`.** The daemon is default-off
  today; capability-bound leases make it the operational floor for mutation (read-only
  diagnostics stay daemon-free). This aligns with GH-85's one-daemon target but is a
  real deployment change.
- **Capability-bound mutation must run in the long-lived owner process.** A one-shot
  `lease-assert` / `lease-release` CLI invocation cannot hold the capability, so those
  verbs lose mutation authority over capability-bound leases and become diagnostics.
  Operator break-glass recovery is the existing takeover path (new fence, new
  binding), which is already the recovery story for a dead owner.
- **Restart loses the capability by design.** Owner restart within a lease term goes
  through reclaim-with-proof if the same process image holds the capability, else
  takeover at fence+1. This is the price of memory-only storage and it is paid
  deliberately: persistence is what would make the capability copyable.
- **The daemon grows per-request challenge state** (single-use, TTL-bound challenges)
  and one extra round trip per lease mutation. Lease mutations are rare (claim,
  per-dispatch assert, release), so the cost is noise.
- **No per-runtime divergence.** One verifier (the daemon), one scheme, one persisted
  shape. Runtimes differ only in *which process* is the holder — see Open question O3.

## 3. Criterion 3 — Binding claim/assert/release/reclaim to the authority

All four operations keep every existing check (registered live session, bound-session
identity match, label equality, fence equality, TTL, flock serialization). Capability
proof is an additional gate evaluated inside the same critical section, and **runtime
ID or PID alone is never accepted as proof of caller ownership** — both remain
required *identity metadata* for collision detection and diagnostics, and neither is
ever *sufficient*.

| Operation | Today's claimant proof | Added gate |
|---|---|---|
| `claim_lease` (fresh) | labels + live session + (runtime id ‖ live pid) | capability established: digest recorded in `caller_capability_binding` at the granted fence |
| `assert_lease` / `with_lease_fence` | labels + fence + (runtime id ‖ live pid) | valid HMAC proof against the stored binding at the current fence, over a fresh challenge |
| `release_lease` | same as assert | same as assert — release is a mutation and gets the full gate |
| idempotent reclaim (same session + same runtime, the `runtime_only_reclaim` path) | **copied labels alone suffice today** | valid HMAC proof against the **stored** binding at the **current** fence; proof refreshes the TTL, exactly the operation the path performs now |
| takeover claim (`--takeover`, expired or provably dead owner) | labels + live session | new capability, new digest, fence **+1**; the old binding is replaced and the old capability is permanently dead |

Fail-closed rules (extending the seam's existing refusal vocabulary):

- a lease carrying `caller_capability_binding` that receives a request **without** a
  proof refuses (`caller_capability_binding_required`) — omitting the optional
  parameter cannot downgrade a bound lease to label-only authority;
- a proof that verifies against no stored binding, a wrong lease key, or a stale fence
  refuses (`caller_capability_binding_mismatch` / `stale_fence_token`) and mutates
  nothing;
- verification happens under the per-identity claim lock, inside the same bounded
  scan budget, so a refused proof leaves no partial state — consistent with the
  repository's bounded-work-fails-closed rule.

Note what this does to the §1.3 adversary concretely: it can copy session id, runtime
id, and the owner's live pid; it cannot produce `HMAC-SHA-256(capability, …)`; every
mutation refuses. The reclaim door — today open to copied labels — now requires the
one thing copying cannot obtain.

## 4. Criterion 4 — Where the capability lives, and how it never reaches a surface

- **Lives:** exclusively in the owning process's memory (a Python object held by the
  long-lived session owner — the watcher/dispatch process that already claims leases
  in-process via `bin/_session_autobridge.py`). Lifetime = process lifetime.
- **Persisted:** only `sha256(capability)` inside the lease record's
  `caller_capability_binding` — the shape `save_lease` /
  `_validate_stored_capability_binding` already handle. The digest is a one-way
  commitment over 256 bits of entropy; publishing it is safe, and `lease-show` may
  display a digest prefix for diagnostics.
- **On the wire (daemon socket):** the digest once at claim/rebind; per-request HMAC
  responses thereafter. The raw capability never transits even the trusted channel,
  so a daemon-log leak cannot reveal it. Challenges and responses are single-use;
  logging a response leaks nothing reusable. Daemon logs record reason codes and
  digest prefixes only.
- **Never in argv** (would appear in `ps`), **never in env** (same-UID readable via
  `/proc/<pid>/environ`), **never in Markdown, receipts, packets, or durable
  artifacts** — including this document, which describes the scheme without
  containing any example secret, token, or key. Test fixtures must use conspicuously
  synthetic values generated at test time, never checked-in constants that a reader
  could mistake for real material.
- **Hygiene regression** (part of criterion 5's suite) scans rendered `lease-show`
  output and captured logs for the fixture capability byte string and asserts its
  absence — the check fails if any surface ever prints the secret.

## 5. Criterion 5 — Adversarial regressions (specified, not written)

Each test is named with what it must assert. Fixtures use the existing private
`_injected_capability_verification` seam for pre-daemon coverage and add
daemon-socket fixtures when the daemon slice lands; no production code is authorized
here.

1. **`test_copied_labels_cannot_assert_release_or_reclaim`** — adversary fixture
   presents the true owner's registered session id, copied runtime id, and the owner's
   *live* pid (liveness genuinely passes) against a capability-bound lease, with no
   proof. Assert: every mutation verb refuses with the capability refusal reason; the
   lease file is byte-identical afterward; the fence is unchanged.
2. **`test_runtime_only_reclaim_requires_capability`** — lease claimed with no
   recorded pid (today's weakest door). Adversary re-claims with the same session id
   and same copied runtime id. Assert: the idempotent `runtime_only_reclaim` branch is
   NOT taken; refusal; fence unchanged; stored binding unchanged. The same request
   *with* a valid proof succeeds and refreshes the TTL (proves the gate kills the
   attack without killing the legitimate path).
3. **`test_replayed_proof_refused`** — capture a valid `(challenge, response)` pair
   from a completed assert; replay it against a new request. Assert: refusal; each
   challenge is single-use and TTL-bound; the replay mutates nothing.
4. **`test_stale_capability_after_takeover_refused`** — owner A bound; B takes over
   (dead-owner path) with a new capability at fence+1. Assert: A's assert and release
   with the old capability refuse (`caller_capability_binding_mismatch`); the stored
   binding carries the new fence; A's *labels* still match and are irrelevant.
5. **`test_capability_not_transferable_across_leases`** — proof valid for lease X
   presented for lease Y (same project, different identity). Assert: refusal; the
   binding's `lease_key` field is what pins it.
6. **`test_omitted_proof_cannot_downgrade_bound_lease`** — caller simply omits the
   optional `caller_capability_verification` argument (today's default code path).
   Assert: refusal, not silent label-only success. This proves the fail-closed rule
   of §3 and guards the optional-parameter shape forever.
7. **`test_no_secret_on_any_surface`** — with a fixture capability, render
   `lease-show` (text and `--json`), capture daemon and CLI logs across
   claim/assert/release/reclaim/refusals, and dump the lease JSON. Assert: the
   fixture byte string appears nowhere; the digest (or its prefix) may appear.
8. **`test_pid_recycling_does_not_grant_authority`** — owner exits; a fixture process
   occupies the recycled pid and presents all copied labels. Assert: every mutation
   refuses without a proof. (Covers the §1.3 PID-recycle item that liveness checks
   cannot see.)

## 6. Criterion 6 — Migration and retirement: one authority at a time

Invariant (adopted from #94): **never two authoritative lease/fence writers for the
same target.** Exactly one of the legacy CLI file authority or the daemon capability
authority may mutate at any moment, and the transition is one-way and
operator-supervised.

- **M0 (today):** the file-based CLI authority is sole. The capability seam is inert
  by construction. Nothing to migrate.
- **M1 (dark landing):** daemon lease endpoints land behind the daemon's existing
  default-off / feature-declaration gate. They serve no production traffic. The CLI
  authority remains sole. This is the same dark-landing pattern P1a–P1d used.
- **M2 (cutover, one-way, operator-supervised):** re-targeting #94's dead checklist at
  the real successor:
  1. **Freeze:** legacy CLI mutation verbs begin failing closed with a
     migration-required refusal, implemented in the same single chokepoint as
     `_refuse_if_legacy_present` (the GH-160 pattern) so read paths stay read-only.
     From this instant the CLI is no longer an authority — this is what makes "one at
     a time" enforceable rather than aspirational.
  2. **Inventory:** bounded scan of every project's `activation_leases/` (the existing
     scan-budget machinery), recording hash-only provenance per the P1d pattern.
  3. **Adopt:** the daemon takes over the *same storage engine* — the
     `_activation_lease.py` flock/fence/TTL mechanics unchanged — as its in-process
     storage layer. One writer, one fence sequence; the fence never resets.
  4. **Verify:** prove the frozen CLI refuses a mutation while the daemon accepts one;
     reconcile against the inventory; quiet period; then retire the CLI mutation verbs
     to diagnostics.
- **A lease held across the transition:** an unexpired lease is imported as
  **provenance, not authority** — it grants no mutation rights by itself. Its recorded
  owner re-establishes authority through a one-time **migration claim**: the daemon
  requires the matching labels, a live bound session record, and (where the platform
  supplies it — see O2) a kernel peer pid matching the recorded `owner_pid`, then
  binds a fresh capability digest at **fence+1** (never a reset, never a parallel
  record). A migration claim is a cooperative ceremony during a bounded,
  operator-supervised window; the residual risk that a §1.3 adversary wins the race
  against the true owner during that window is the same exposure the system has
  shipped since the v2 lease existed, and it ends permanently at the first
  capability-bound fence. If the owner does not re-establish before TTL expiry, the
  lease lapses and any new claim is capability-only. Because TTLs are short (default
  one hour), the mixed-state window is bounded by construction. **Never co-write:**
  before the freeze the daemon is dark; after the freeze the CLI is frozen; there is
  no instant at which both can mutate.
- **Rollback:** restore the pre-cutover inventory backup and lift the freeze by
  operator reversal. A lease already capability-bound keeps its fence; on rollback it
  reverts to label authority only as a whole-workspace state restore, never
  per-lease, so no dual-authority window opens in reverse either.

## 7. Open questions (named, not defaulted)

- **O1 — Does the lease's mutation surface survive the bb migration?** The v2
  activation lease today gates the autobridge dispatch path (runtime trigger, UI
  refresh) for workers. With #94 superseded-by-bb and the #562 pilot making bb the
  runtime adapter, that path may retire, shrinking the lease's protected surface —
  possibly to nothing. If so, the correct sequencing is *retire-first* and this
  hardening never ships. The evidence available (the pilot is pre-go/no-go) does not
  settle it. This design is written so the hardening is worthwhile in either branch:
  the capability seam and the one-authority migration gates apply to whatever lease
  surface remains.
- **O2 — Peer-pid portability.** The daemon's `peer_uid` proves uid only. Kernel peer
  *pid* is available on Linux (`SO_PEERCRED`) and on macOS/BSD (peer-pid socket
  options), but the daemon does not implement it today and this lane did not verify
  per-platform behavior. Whether the migration claim of §6 *hard-requires* a peer-pid
  match or accepts uid + live-session as sufficient for the bounded ceremony is
  undecided pending a platform verification pass. The capability proof itself is
  unaffected either way.
- **O3 — Holder identity under bb.** §2 assumes a single long-lived owner process
  holds the capability. For bb-hosted threads that resume across restarts, the
  natural holder may be the watcher rather than the worker, and capability continuity
  across a bb resume is unproven. Choosing the holder is the one bootstrap decision
  this document deliberately leaves to the implementation lane, because it depends on
  the #562 pilot's outcome rather than on anything decidable from the current tree.

Related GH-159, GH-85, GH-94.
