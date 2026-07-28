# Lane Contract (Tier A)

One page, written **before the first branch**, living in the task or linked
issue. Reviews verify the diff against this contract; they do not discover it.
The gate and the finding-routing rules are canonical in
[`commit-push-prs.md`](commit-push-prs.md) → "Lane contract" and "Per-finding
disposition at arrival"; this file is only the template and one worked example.

## Template

```markdown
### Lane
<task/issue id + one-sentence outcome>

### Authority boundary
<the one component that owns the decision or state this lane changes — a
canonical store, a sole writer, a named module — and what every other
component may assume>

### Commit point
<the single operation after which the change is durable and visible>

### Retry behavior
<what a retry may repeat; what it must never repeat>

### Non-goals
<guarantees this lane explicitly does not provide; findings about these defer>
```

## Rules of thumb

- If the authority boundary cannot provide the guarantee the lane promises
  (exactly-once, atomicity, ordering), that is the lane's first finding, and
  it is resolved on this page — by choosing a boundary that can — not at
  review cycle five.
- If the page does not fit on one page, the lane is two lanes. Split at
  intake, where splitting is cheap.
- Non-goals are load-bearing: they are what makes out-of-contract findings
  deferrable instead of blocking. Write them honestly.

## Worked example (the #345/#347 family, as it should have started)

```markdown
### Lane
GH-343: wake Pi from exact durable unread packets.

### Authority boundary
The canonical session/inbox store owns unread state and consumption. Readers
select and consume through one store transaction; no filesystem scan and no
reader-side lock is authoritative.

### Commit point
One store transaction that marks the packet consumed and records the wake
attempt. Stdout/wake emission happens only after that commit returns.

### Retry behavior
A retried wake re-selects from the store and never re-emits a consumed
packet. A failed wake leaves the packet unread and may be retried.

### Non-goals
Cross-project routing, lease extension, multi-session fan-out, reader-side
exactly-once emulation.
```
