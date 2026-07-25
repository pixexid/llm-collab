# Starting And Running A Collab Thread

## Goal

Everything a worker needs to join a collaboration, exchange packets, and get work
reviewed — in the order you actually need it, with the failure modes that have
really happened.

`session-startup.md` covers the *environment*. This covers the *collaboration*.

## 1. Bootstrap

```bash
python bin/session_bootstrap.py --agent <agent_id>
```

Starts your inbox watcher and prints your identity, current project, and recent
mail. Run it once per session, before anything else.

## 2. Know your three coordinates

Every packet is addressed by **project**, **chat**, and **agent**. Get these wrong
and the message goes nowhere useful:

- `project_id` — from `projects.json`. Must be registered; an unregistered project
  is refused rather than guessed.
- `chat_id` — the conversation (`CHAT-XXXXXXXX`). One work stream per chat.
- `agent_id` — you, and your counterpart.

A worker often runs several concurrent sessions in different chats. **Reply in the
chat the request arrived on**; do not default every reply to the thread you happen
to be watching.

## 3. Read your inbox

```bash
python bin/inbox.py --me <agent_id> --project <project_id> --chat <CHAT-ID> --limit 5
```

Scope it. `--chat` matches a substring, and an unscoped read across a busy
workspace will surface another project's traffic.

## 4. Send a packet

```bash
python bin/deliver.py \
  --project <project_id> --chat <CHAT-ID> \
  --from <you> --to <them> \
  --repo-targets <repo-id> \
  --title "..." --body-file /tmp/message.md
```

**`--repo-targets` is not optional in practice.** If the recipient's session
declares a repo scope and your packet declares none, the packet is written to the
mailbox and then **refused at dispatch** — the recipient never sees it.

This is not hypothetical: on 2026-07-25 it silently dropped **27 consecutive
packets** over eleven hours, and the lane only kept working because GitHub PR
comments were carrying the conversation. `deliver.py` now prints a loud
`DURABLE WRITE OK — RUNTIME DISPATCH REFUSED` banner naming both scopes when this
happens, but the fix is to declare the scope.

Prefer `--body-file` over inline text: long bodies and shell quoting do not mix.

## 5. Verify it actually dispatched

`autobridge_ready: true` means a target session was resolved. It is **not** proof
of delivery. Confirm:

```bash
pm2 logs llm-collab-<recipient> --lines 5 --nostream | grep -E "new_message|refused"
```

`new_message` means it dispatched. `autobridge_repo_scope_refused` means step 4
went wrong.

## 6. Watch a peer work, live

```bash
python bin/codex_stream.py --agent <agent_id> --project <project_id> \
  --chat <CHAT-ID> --seconds 90
```

Streams that worker's real thread — its reasoning, the commands it runs, and its
reply as it types. This is the fastest way to see whether a peer is working,
stalled, or waiting on you, and it is read-only: it answers no server request and
sends no input.

`--chat last` selects the newest binding. `--raw` prints every notification as
JSON. `--seconds` bounds the run.

## 7. Getting reviewed

**Codex code review is manual only.** Automatic review is off; nothing arrives
unless someone asks. **Do not wait for a review nobody requested.**

Whether you must request one is decided by the Tier A/B/C rule in
[`AGENTS.md` → Requesting Code Review](../../AGENTS.md#requesting-code-review-all-workers-every-repository).
Short version: credentials, authority, money, input we do not control, shared
contract changes, concurrency and partial state, migrations, and anything that can
weaken proof of those — **must** be requested. Formatting, comments and additive
tests must not.

Request once, on the head you believe is final:

```
@codex review for <every Tier A family the diff touches>
```

An amendment stales the review; request again on the new final head. Any finding
that arrives is adjudicated in writing whatever the tier.

## 8. When something looks wrong

| Symptom | Cause | Fix |
|---|---|---|
| Peer never replies; their thread is idle | packet refused at dispatch | check the watcher log (step 5); re-send with `--repo-targets` |
| `deliver.py` prints `DURABLE WRITE OK — RUNTIME DISPATCH REFUSED` | packet scope not a subset of the recipient's | declare the right `--repo-targets` |
| `blocker: the recipient's binding could not be READ` | the binding file exists but is unreadable | repair or remove that binding; nothing will wake them until then |
| Waiting a long time for a bot review | nobody requested one | see step 7; auto review is off |
| `codex_stream` refuses with "not registered in projects.json" | project not registered, or wrong id | register it, or correct the id |
| `codex_stream` refuses naming two different homes | binding and session disagree on `CODEX_HOME` | re-register the session; the pair is torn |
| Your reply reached the wrong session | replied in a different chat than the request | reply in the originating chat |

## 9. Ending cleanly

Leave the lane so the next worker can pick it up:

- every request you received is answered, or has a written status
- work in flight is on a branch and pushed, never an uncommitted working tree —
  a clean-tree sweep will discard it
- anything you decided that is not obvious from the diff is written down in the
  packet or the PR, not only in your own memory

## See also

- `session-startup.md` — environment, tooling currency, preflight
- `commit-push-prs.md` — branch, commit, PR and merge gates
- `review-and-handoff.md` — review flow and terminal signals
- `../multi-project.md` — projects, repos, and boundaries
