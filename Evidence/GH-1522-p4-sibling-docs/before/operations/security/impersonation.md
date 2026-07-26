# View-as / impersonation operations runbook

Runtime source: GH-496 / TASK-8E65CE.

## V1 policy

- V1 ships **View as** only: read-only shadowing for operator triage.
- Supabase auth sessions are not swapped. The real operator remains the authenticated principal; View-as state is a short-lived server-validated claim and cookie.
- `admin` is the current Amiga owner-equivalent for initiating View-as, reviewing sessions, and force-revoking sessions. A separate true owner/entitlement role is a future role-system lane.
- Reachable targets in the current role model are `client` and `staff`. `staff` covers crew-lead-equivalent behavior for v1 because Amiga does not have a distinct `crew-lead` auth/profile role today.
- `admin` targets are forbidden.

## Runtime controls

- `/app/security/impersonation` is the admin-only session review surface.
- Active View-as sessions render an amber banner and a persistent footer chip showing the target identity, read-only status, reason, session slug, expiry, and exit affordance.
- Sessions are valid for at most 60 minutes, expire after 15 minutes of inactivity, and are tied to the initiating operator bearer session.
- Mutation routes must reject writes under active View-as with `impersonation_read_only` and record an attempted-write event.
- Exiting or force-revoking a session closes the audit row, clears the cookie, and creates an in-app disclosure notification for the target user.

## Bot disposition

- `amiga-operator-cmo` must pause tools while an operator has an active View-as session. It can explain the audit/read-only policy from this runbook.
- `amiga-concierge` stays silent by default about the feature's existence. If a customer asks whether their account was viewed, it should hand off to a human rather than expose internal security mechanics.

## Follow-up gaps

- Add a true owner/entitlement role if product needs admin-vs-owner separation.
- Add a distinct crew-lead role/entitlement if product needs crew-lead targets to differ from staff targets.
- Expand customer-facing disclosure channels beyond in-app notification only if policy requires email/SMS.
