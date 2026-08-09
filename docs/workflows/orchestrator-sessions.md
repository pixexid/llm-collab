# Orchestrator Sessions

## Goal

Keep an orchestrator session observable, economical, and recoverable across a
session boundary. This document owns the orchestrator watcher set, succession,
supervisor relationship, model routing, and bb-update procedure.

It does not own worker provisioning, profile availability, delegation, lane
limits, or PR gates. Use
[`bb-workers.md`](bb-workers.md),
[`bb-worker-profiles.md`](bb-worker-profiles.md),
[`task-intake-and-delegation.md`](task-intake-and-delegation.md), and
[`commit-push-prs.md`](commit-push-prs.md) for those procedures.

## Standard watcher set

Start all three watchers at the beginning of every BB orchestrator session as
persistent `Monitor` calls. Substitute `<PROJECT_ID>`, `<OWNER/REPO>`, `<REPO>`,
`<COLLAB_PROJECT_ID>`, and `$SD` (the session scratchpad directory). Each
template initializes `bb_cmd` from the exact project's configured
`bb.executable`, using the same argv-preserving form as
[`BB Workers`](bb-workers.md#spawn-in-an-isolated-worktree); every native BB
call in that template uses the resulting command array.

The watchers report changes; they do not decide that work is complete.

- A worker leaving `active` means **go look**. `idle` never means finished:
  read both `thread output` and `thread log`, then inspect the promised artifact
  and terminal action. `output` can be stale while a turn is active, and a
  wedged active lane produces no transition event.
- A startable issue excludes work blocked on an external actor, parked by a
  decision, and epics. A drained queue produces a status line, not permission to
  invent work. Queue and lane activation remain owned by
  [`Task Intake And Delegation`](task-intake-and-delegation.md).
- Stop all three watchers with `TaskStop` when flagging handoff. A heartbeat
  outliving its orchestrator can argue for starting work nobody selected.

Every watcher emits a liveness line every 20 cycles so a dead watcher is
distinguishable from a quiet repository.

Each watcher also refreshes one externally visible marker on every completed
cycle:

```text
{project_state_root}/llm-collab/watchers/<name>.alive
```

`<name>` is `worker-lifecycle`, `pr-artifacts`, or `heartbeat`. A watcher that
dies otherwise leaves a silent session that looks exactly like a quiet
repository; a stale marker is the only externally visible difference. Create
the directory when the watcher starts and touch the marker immediately before
the cycle's sleep. If the loop wedges before completing its checks, the marker
therefore stops advancing.

### Worker lifecycle

```bash
cd <REPO>
bb_cmd=()
while IFS= read -r -d '' token; do bb_cmd+=("$token"); done < <(jq -j '.projects[] | select(.id=="<COLLAB_PROJECT_ID>") | (.bb.executable // ["bb"])[] | ., "\u0000"' projects.json)
[ "${#bb_cmd[@]}" -gt 0 ] || { echo "REFUSED: missing bb.executable" >&2; exit 1; }
D=$SD/bbstat2
W=$(python3.11 -c "import sys; sys.path.insert(0,'bin'); from _helpers import project_state_dir; print(project_state_dir('llm-collab') / 'watchers')")
mkdir -p "$D"
mkdir -p "$W"
CYC=0
while true; do
  CYC=$((CYC+1)); [ $((CYC % 20)) -eq 0 ] && echo "WATCHER LIVE (worker-lifecycle) cycle $CYC"
  "${bb_cmd[@]}" thread list --project <PROJECT_ID> --include-hidden --json 2>/dev/null \
    | python3.11 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
rows = d if isinstance(d,list) else d.get('threads',[])
for t in rows:
    print(t.get('id'), t.get('status'), (t.get('title') or '')[:40].replace(' ','_'))
" > "$D/now.txt" 2>/dev/null
  while read -r id st title; do
    [ -z "$id" ] && continue
    prev=$(cat "$D/$id" 2>/dev/null)
    if [ "$st" != "$prev" ]; then
      echo "$st" > "$D/$id"
      if [ -n "$prev" ] && [ "$prev" = "active" ]; then
        echo "WORKER LEFT ACTIVE $id ($title): active -> $st — go look (thread output AND log); idle does not mean finished"
      fi
    fi
  done < "$D/now.txt"
  touch "$W/worker-lifecycle.alive"
  sleep 40
done
```

`--include-hidden` keeps probe threads observable. The watcher records
transitions, so steady state does not notify repeatedly.

### PR connector artifacts

```bash
cd <REPO>
bb_cmd=()
while IFS= read -r -d '' token; do bb_cmd+=("$token"); done < <(jq -j '.projects[] | select(.id=="<COLLAB_PROJECT_ID>") | (.bb.executable // ["bb"])[] | ., "\u0000"' projects.json)
[ "${#bb_cmd[@]}" -gt 0 ] || { echo "REFUSED: missing bb.executable" >&2; exit 1; }
D=$SD/prsig
W=$(python3.11 -c "import sys; sys.path.insert(0,'bin'); from _helpers import project_state_dir; print(project_state_dir('llm-collab') / 'watchers')")
mkdir -p "$D"
mkdir -p "$W"
CYC=0
while true; do
  CYC=$((CYC+1)); [ $((CYC % 20)) -eq 0 ] && echo "WATCHER LIVE (pr-artifacts) cycle $CYC"
  for pr in $(gh pr list --repo <OWNER/REPO> --state open --json number --jq '.[].number' 2>/dev/null); do
    sig=$(python3.11 bin/pr_watch.py --repo <OWNER/REPO> --pr "$pr" --once 2>/dev/null | tr -d ' \n')
    [ -z "$sig" ] && { echo "PR #$pr watcher got EMPTY signature — pr_watch.py may be failing, investigate"; continue; }
    if [ ! -f "$D/$pr" ]; then echo "$sig" > "$D/$pr"; echo "PR #$pr armed (baseline captured)"; continue; fi
    if [ "$sig" != "$(cat "$D/$pr")" ]; then
      echo "$sig" > "$D/$pr"
      echo "PR #$pr TIMELINE CHANGED — inspect the complete reviewed artifact set at head $(gh pr view "$pr" --repo <OWNER/REPO> --json headRefOid --jq .headRefOid 2>/dev/null | cut -c1-7)"
    fi
  done
  touch "$W/pr-artifacts.alive"
  sleep 45
done
```

An empty signature is an alert, never a quiet continuation. `pr_watch.py --once`
covers the timeline, reactions, and check runs; in particular, a clean connector
pass can be reaction-only. The meaning of connector artifacts and the required
post-merge recheck remain canonical in
[`Commit, Push, And PR Workflow`](commit-push-prs.md#pr-review-wait-gate).

### Guarded heartbeat and bb currency

The heartbeat performs one installed-version-versus-pin comparison per cycle.
It alerts on mismatch **and** when either side of the comparison cannot be read;
a broken probe must not make the repository look quiet.

```bash
cd <REPO>
bb_cmd=()
while IFS= read -r -d '' token; do bb_cmd+=("$token"); done < <(jq -j '.projects[] | select(.id=="<COLLAB_PROJECT_ID>") | (.bb.executable // ["bb"])[] | ., "\u0000"' projects.json)
[ "${#bb_cmd[@]}" -gt 0 ] || { echo "REFUSED: missing bb.executable" >&2; exit 1; }
W=$(python3.11 -c "import sys; sys.path.insert(0,'bin'); from _helpers import project_state_dir; print(project_state_dir('llm-collab') / 'watchers')")
mkdir -p "$W"
CYC=0
while true; do
  CYC=$((CYC+1)); [ $((CYC % 20)) -eq 0 ] && echo "WATCHER LIVE (heartbeat) cycle $CYC"
  pin=$(python3.11 -c "import sys; sys.path.insert(0,'.'); from llm_collab.bb_client import PINNED_BB_VERSION; print(PINNED_BB_VERSION)" 2>/dev/null)
  cur=$("${bb_cmd[@]}" settings version --json 2>/dev/null | python3.11 -c "import sys,json; print(json.load(sys.stdin).get('currentVersion',''))" 2>/dev/null)
  if [ -z "$pin" ] || [ -z "$cur" ]; then
    echo "BB VERSION CHECK FAILED (pin='$pin' installed='$cur') — the check is broken, not necessarily bb; later quiet cycles prove nothing until this is fixed"
  elif [ "$pin" != "$cur" ]; then
    echo "BB VERSION MISMATCH pin=$pin installed=$cur — bin/bb_spawn.py will refuse bb_version_mismatch; run the bb-update procedure before starting lanes"
  fi
  prs=$(gh pr list --repo <OWNER/REPO> --state open --json number --jq 'length' 2>/dev/null || echo "?")
  lanes=$("${bb_cmd[@]}" thread list --project <PROJECT_ID> --json 2>/dev/null | python3.11 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('?'); raise SystemExit
rows=d if isinstance(d,list) else d.get('threads',[])
print(len([t for t in rows if t.get('status')=='active']))
" 2>/dev/null || echo "?")
  issues=$(gh issue list --repo <OWNER/REPO> --state open --json number --jq 'length' 2>/dev/null || echo "?")
  echo "HEARTBEAT openPRs=$prs activeLanes=$lanes openIssues=$issues — if lanes<2 AND a startable issue exists (not blocked-on-external, not parked-by-decision, not an epic) start it; a drained queue is a status, not an order; never invent work"
  touch "$W/heartbeat.alive"
  sleep 600
done
```

This states a comparison rule, not a current version. `PINNED_BB_VERSION` in
`llm_collab/bb_client.py` is pin authority; `settings version --json` is the
live installed-value check.

## Verification traps

- **Audit all four connector artifact classes:** review threads, review bodies,
  issue/PR comments, and reactions. A finding can live only in a review body,
  where thread enumeration cannot see it; a clean pass can be reaction-only,
  with no review object or comment. Use `python3.11 bin/pr_watch.py`, whose
  signature covers the timeline, reactions, and check runs; a
  `gh pr view --json reviews,comments` poller cannot prove either outcome.
- **A declared default is not an executed value.** Read the executed provider,
  model, and reasoning level from the execution event.
- **Bind verification to its checkout.** Print `pwd` and
  `git rev-parse HEAD` inside the suite directory, in the same shell invocation
  as the test run.
- **Gate autolink safety.** Make the prohibited-pattern check exit nonzero before
  publication; a warning on its own line prints and proceeds.
- **Re-check once after merge.** The connector can re-pass an amended head
  asynchronously; inspect the complete reviewed artifact set and adjudicate any
  late finding under the canonical PR workflow.

## Succession protocol

The per-generation handoff lives at
`<operator-checkout>/scratchpad/orchestrator-handoff.md`. It carries only state
that a successor cannot recover from the repository and GitHub:

- handoff status and last-updated time;
- live BB threads, lanes, and PRs that need continuation;
- **Environment deltas and operator announcements**: tool or CLI changes,
  quota events, untracked configuration changes, machine changes, and operator
  statements that can invalidate a lane;
- open decisions and genuine external blockers.

The environment-deltas section is mandatory. If nothing changed, say that
explicitly; an empty section is a claim that there were no deltas, not an
omission. Do not copy this document's procedure into the handoff.

### Predecessor

1. Reconcile the live-thread, PR, decision, blocker, and environment-delta
   state in the handoff.
2. Stop every watcher in this document with `TaskStop`.
3. Flag the handoff in the file.
4. Ping the supervisor session. Writing the file without that ping is not a
   handoff.

### Successor bootstrap

1. Start through the deployed `current_runtime.py` launcher, read `AGENTS.md`
   and all Required Reading, and load Ponytail. The canonical startup command
   and runtime-root rule live in [`Session Startup`](session-startup.md).
2. Read the handoff in full, beginning with Environment deltas and operator
   announcements.
3. Check environment currency before the first spawn. Repository currency is
   not tool currency: compare the live installed bb version with
   `PINNED_BB_VERSION` as the heartbeat does, then verify every additional tool,
   quota, configuration, or machine delta named by the handoff. A mismatch here
   is planned recovery; the same mismatch at the first refused spawn is an
   outage.
4. Ping the supervisor session that the successor is online.
5. Start the watcher set above.
6. Recover live work through the canonical BB, delegation, and PR workflows
   linked at the top of this document.

If anything looks inconsistent during bootstrap, read the predecessor
session's tail before improvising. A contradiction is evidence to recover, not
permission to guess.

## Supervisor arrangement

The supervisor preserves continuity across orchestrator generations and routes
operator-owned decisions; the orchestrator owns technical execution and
verification.

- Route business priority, irreversible choices, credentials/account changes,
  and product decisions without stated authority to the supervisor. Keep
  technical scope, implementation choices, bounded recovery, and the normal
  review/release flow with the orchestrator. The broader escalation boundary is
  canonical in [`AGENTS.md`](../../AGENTS.md#workers-own-their-own-setup).
- Spend expensive-model tokens on orchestration, independent review, and
  judgment. Delegate bounded work that a cheaper eligible worker can perform;
  evaluation candidates use their own execution tokens.
- The supervisor is the mandatory handoff signal path, not a second queue or
  task store. Lane ownership, caps, activation, and terminal evidence remain in
  the linked canonical workflows.

## Model routing policy

Operator policy for orchestrator-selected work:

- Route complex authoring to `k3` or `sol`. When a live `k3` attempt reports a
  quota refusal, use `sol`, never `luna`, for that complex authoring lane.
- Use `luna` as the daily driver for work within its measured boundary.
- Request maximum reasoning effort for `luna` and `glm-5.2`. Contract v15's
  hard exclusions still apply to every text-bearing assignment, so this effort
  setting does not authorize an excluded model; only new measurement and a
  contract change can lift an exclusion.
- Apply the current qualification and exclusion rules from
  [`BB Worker Profiles`](bb-worker-profiles.md) and
  [`BB Workers`](bb-workers.md#spawn-in-an-isolated-worktree). Do not copy the
  current qualified-set membership into a handoff or this policy. Its authority
  is `AUTHORING_QUALIFIED_PROFILES` in `llm_collab/bb_bootstrap.py`; inspect that
  value for the rule-enforced set and query the execution machine as the profile
  workflow requires for live availability.
- Treat quota as live provider state, never as a carried assertion. Establish it
  from the latest execution on the selected profile and inspect `output` and
  `log` for a provider quota or permission refusal before rerouting.
- Key each model-evaluation row by the executed
  `(provider, model, reasoning_level)` triple read from the execution event, not
  requested argv or a declared default. The artifact definition and source are
  owned by
  [`bb-plugins/exec-tracking/README.md`](../../bb-plugins/exec-tracking/README.md).
  Store the bb version observed for that execution with the row so comparisons
  across CLI versions remain interpretable.

This section states routing rules and checks. It intentionally asserts no
current bb version, qualified-set membership, model availability, or quota
state.

## bb-update procedure

The exact pin is a safety control. Do not loosen it to make an unobserved bb
release pass.

1. Stop new BB lane starts and compare the installed version with
   `PINNED_BB_VERSION`. If the probe itself fails, repair the probe before using
   any quiet result.
2. Read the four load-bearing CLI properties named at the top of
   `llm_collab/bb_client.py`. Re-observe **all four** against the live installed
   CLI before changing the pin; prior observations and release notes are not
   substitutes.
3. Re-record `tests/fixtures/bb/settings_version.json` from the live configured
   CLI's `settings version --json` output. Do not hand-edit a file whose purpose
   is to preserve a recording.
4. Inspect every version-mismatch test before moving the pin. A wrong-version
   literal must not equal the new pin; otherwise the test becomes a match while
   claiming to prove refusal.
5. Sweep `AGENTS.md`, `docs/`, fixtures, and tests for commands whose semantics
   changed and version-stamped claims that must be re-verified. Update or record
   a disposition for each affected claim; never carry a live observation
   forward as an assertion about the new release.
6. Author the pin, fixture, test, and necessary documentation changes in the
   orchestrator thread. This change cannot be delegated through the version gate
   it unblocks. Use the normal one-writer and PR workflow, including the required
   verification and first automatic review; this is not an exception to those
   gates.
7. Run `bb plugin reload exec-tracking`, launch one controlled probe, and require
   a new recorded row in the configured project's executed-triple artifact. A
   running plugin does not follow a changed checkout, and a running status alone
   does not prove recording.
8. Record the observed bb version beside every evaluation row created during the
   update or its qualification probes.

The current pin is read from source, the installed version is read live, the
qualified set is read from its code authority, model availability is queried on
the execution machine, quota is established by a live execution result, and the
executed triple is read from its execution event. None is inferred from this
document.
