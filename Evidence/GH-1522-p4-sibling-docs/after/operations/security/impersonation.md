# View-as / impersonation — REMOVED (sunset 2026-07-16)

**There is no View-as feature in Amiga. This file is a sunset record, not a runbook.**

## The sunset

- View-as (read-only operator shadowing, GH-496 / TASK-8E65CE) was **never delivered as a working product surface** — the entry point was a no-op.
- It was **removed on 2026-07-16 by operator decision** (umbrella GH-1518): client surfaces in GH-1519, the server/API and the authorization branch in GH-1520, this documentation and the bot pointers in GH-1522, and the database tables in GH-1521.
- **It is not coming back without a new lane.**

## What that means operationally

- There is no `/app/security/impersonation` surface, no View-as banner or footer chip, no session cookie, and no `impersonation_read_only` refusal. There is nothing to start, review, revoke, or exit.
- Effective identity is always the **authenticated operator**. No path substitutes one user for another.
- Neither bot has a View-as rail: `amiga-operator-cmo` has no tool-pause rule for it, and `amiga-concierge` has nothing to stay silent about.
- If an operator needs to see what a customer sees, that is a **new product decision** — not a dormant capability waiting to be re-enabled.

## Historical record that survives on purpose

Customers whose accounts were viewed before the sunset still have their `impersonation_disclosure` notification in `app_notifications`. Those rows stay readable — a customer is entitled to the disclosure that their account was viewed. The notification **type** is retained for that reason alone; **nothing produces it any more**.
