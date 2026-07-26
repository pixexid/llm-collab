# Amiga Operator CMO

Canonical spec:
- `/Users/pixexid/Projects/OpenClaw_bots/bots/amiga-operator-cmo/spec.md`

Identity bundle:
- `/Users/pixexid/Projects/OpenClaw_bots/bots/amiga-operator-cmo/identity`

Config example:
- `/Users/pixexid/Projects/OpenClaw_bots/bots/amiga-operator-cmo/config.example.json5`

This bot is the private internal Amiga assistant for operations, owner support, and CMO-style strategy.

## Bot impact (GH-419 / GH-440)

- **Status: IMPACTED** by the customer complaint / issue intake lane.
- Design contract: `/Users/pixexid/Projects/amiga/design/surfaces/customer-complaint-intake.md` (§"Boundary contract", §"Bot impact").
- Canonical spec section: `/Users/pixexid/Projects/OpenClaw_bots/bots/amiga-operator-cmo/spec.md` → "Customer Drafts".
- Summary: the operator-cmo bot recognizes customer-submitted *drafts pending operator review* as distinct from operator-filed Incidents, answers operator queries about pending drafts on a booking, and must not auto-promote a draft to an Incident or auto-dismiss it (operator-only decisions). Severity `critical` stays operator-only.
- Runtime visibility note: `/app/incidents` now gives staff/admin a read-only submitted-report queue/detail view backed by `GET /api/operations/customer-reports`; bot guidance should treat this as visibility only, not decision persistence or Incident creation.

## Urgent report escalation (GH-457)

- **Status: IMPACTED.** Submitting an urgent customer report (`urgency = today` or an injury topic) now fires an internal operator/on-call escalation **email** at submit time, in addition to landing the report in the `/app/incidents` queue.
- Canonical spec section: `/Users/pixexid/Projects/OpenClaw_bots/bots/amiga-operator-cmo/spec.md` → "Urgent Report Escalation".
- Summary for the bot: if an operator asks whether an urgent report was escalated, the honest answer is that an escalation email is sent to the on-call address (`OPERATOR_ESCALATION_EMAIL`) and its dispatch state is tracked on the report row (`escalation_status` = pending · sent · failed · skipped). Escalation is non-blocking — a failed or unconfigured escalation never blocks the customer's submission, and non-urgent reports are `skipped` by design (not an error).
- The escalation is internal operator mail only. There is no customer-facing paging, no SLA timer, and no operator queue decision change in this lane; the bot must not tell a customer their report was "escalated" or imply a guaranteed response time.

## Feedback workspace and Feedback-to-Incident boundary (GH-778)

- **Status: IMPACTED.** `/app/feedback` is the signed app workspace for per-visit customer feedback, with Needs attention / New / All / Closed views, operator triage actions, and a detail drawer.
- Design contract: `/Users/pixexid/Projects/amiga/design/surfaces/feedback.md`.
- Summary for the bot: Feedback is customer-authored visit signal (`score`, optional comment, optional follow-up request). Operators can acknowledge it, work follow-up, close it, or open an Incident from the feedback when it needs a durable case record.
- Boundary: Feedback -> Incident is one-way. A Feedback row may carry a linked-incident chip after escalation, but Incidents do not spawn customer feedback. Do not imply automatic Incident creation, reverse Incident-to-Feedback flow, or hidden customer tracking.
- Follow-up caveat: staff-present and client-name enrichment on Feedback rows/drawer is tracked separately in GH-793 / TASK-513B56; per-staff/per-client rollup host wiring is the S1 follow-up.

## Inbox workspace intake triage (GH-777 / GH-920)

- **Status: IMPACTED.** `/app/inbox` is the signed app workspace for newly-created booking intake that still needs operator review. It stays top-level in the app IA, separate from Operations and Bookings.
- Design contract: `/Users/pixexid/Projects/amiga/design/src/surfaces/operations/inbox/InboxSurface.tsx` plus `/Users/pixexid/Projects/amiga/docs/ui_ux/DESIGN.md`.
- Summary for the bot: Inbox answers "what just came in" for newly-created booking intake, sorted newest-first by created time, with recurring-generated instances filtered out. Operators can open the linked booking, acknowledge one row, or mark the visible unacknowledged intake reviewed. Acknowledgement is recorded as a `booking_events` row with `event_type = operator_acknowledged`; do not infer review from `bookings.operational_status`, because auto-routed bookings can already be `route_assigned` while still awaiting operator review.
- Boundary: Inbox is an operator intake triage surface, not a customer-facing status surface and not the service-date timeline. Bookings remains the source for scheduled service dates. Do not tell customers that they can track whether an intake was reviewed.
- Role scope (GH-986 / GH-997 / GH-994): the org-wide intake queue is **admin/operator only** — that is the surface `amiga-operator-cmo` reasons about. Staff no longer have `/app/inbox`; staff notifications live in the app bell and read-only `/app/updates` history. The future "Inbox = messages" concept is tracked separately. Do not assume staff see any booking-intake Inbox.
- Source fidelity: valid persisted `bookings.intake_channel` values (`website`, `phone`, `concierge`, `referral`) are the first source of truth for Inbox source labels. Website self-serve rows persist `website`; operator/concierge on-behalf rows persist `concierge`; legacy `NULL` rows still fall back to customer phone -> `phone`, otherwise `website`. `referral` remains reserved until a referral-intake producer exists.
- Runtime note: the sidebar Inbox unacknowledged badge/count uses the same missing-`operator_acknowledged` predicate as the admin/operator list. Staff have no Inbox badge/list after GH-997.

## Staff notifications and flag-resolution loop (GH-997)

- **Status: IMPACTED.** Staff communications now use `app_notifications` for the bell, not Inbox. The staff notification matrix is defined in `/Users/pixexid/Projects/amiga/design/surfaces/staff-comms.md`.
- Bot guidance: if an operator asks what staff were told, answer from the matrix: assignment published sends a staff bell update plus email, schedule changed / flag resolved / task-completed awareness are bell notifications, and staff marking a task in progress stays silent. Internal ops email also goes to `OPERATOR_ESCALATION_EMAIL` when a booking is rescheduled or a task assignment is updated, so operators have an email audit of those changes.
- Flag loop: when staff flag a task for review, operators receive an in-app `staff_update` notification. When an operator resolves the flag, the flagging staff member receives a `Flag resolved` bell notification, and resolution state is stamped on `tasks.details.reviewFlag`.
- Staff Updates (GH-994): staff now have `/app/updates`, a read-only notification-history page backed by the same recipient-scoped `app_notifications` channel as the bell. It replays supervisor notes, schedule changes, and other staff-addressed notifications for the signed-in staff member only, with mark-read controls. It is not compose/reply/threading and is not a staff Inbox.
- Boundary: staff task notification deep links go to `/app/my-day?taskId=<taskId>`, and `/app/bookings/$bookingId` is guarded away from staff. Do not describe staff Inbox as live; real staff/operator/client message threads are a separate future epic.

## Staff Recent work — own work record (GH-987)

- **Status: staff-facing, not an operator surface.** Staff now have a read-only work record at `/app/recent-work` (sidebar item `Recent work`, after My Day; My Day also links to it from a footer tap-through). It lists the staff member's own completed/cancelled tasks (`scheduled_date < today`, 14-day windows, `Load older`), each opening a read-only detail drawer to revisit the checklist and any notes they left. No mutations exist on this surface.
- Boundary for the bot: this is the *staff* "what I have done" view, scoped to the staff member's own assigned tasks via the existing `assignedToMe` task-assignment scope — it is NOT an operator/admin reporting surface and never shows org-wide work. Do not reason about it as an operator dashboard or promise per-staff rollups here; operator-side staff history/reporting remains separate.
- Design contract: `/Users/pixexid/Projects/amiga/design/surfaces/staff-recent-work.md` plus `/Users/pixexid/Projects/amiga/docs/ui_ux/DESIGN.md` §16/§10.
- Flag outcomes: Recent work now distinguishes never-flagged, pending, and resolved tasks. The card shows `Flag pending` for an open review flag, `Flag resolved` for a resolved review flag, or no chip when never flagged; the drawer shows the original flag context plus the supervisor resolution note when present.
- Client feedback visibility: Recent work now shows submitted feedback for the staff member's own completed/cancelled jobs only: score, optional comment, follow-up-request marker, submitted date, and current feedback status. This remains read-only and assignee-scoped; do not describe it as org-wide feedback access, operator triage, or a customer-facing reply channel.
- Deferred (do not present as live): staff message channel / real Inbox messages (GH-1000).

## Staff My Day tomorrow glance (GH-993)

- **Status: staff-facing, not an operator surface.** Staff My Day now shows one read-only secondary line under the greeting when the staff member has assigned work tomorrow, e.g. `Tomorrow: 1 task - first stop 8:00 AM`.
- Boundary for the bot: this is a calm peek for the staff member's own assignee-scoped tomorrow tasks only. It is not a tomorrow task list, route preview, operator staffing report, or org-wide schedule summary.
- Data contract: the app computes the count and first window server-side from the staff member's own assignee task ids for tomorrow's PT business date, excluding done/cancelled tasks, and only on the My Day path. Zero or absent upcoming work renders no line.

## Resources / operations standards (GH-470)

- **Status: IMPACTED.** The admin Resources workspace at `/design/resources` is now the canonical rendered view of the Amiga Clean operations-standards bundle (product system, color code, room SOPs, laundry / stain protocol, safety / PPE, SDS structure, exception log shape, training quiz with answers, rollout plan, external source references).
- Canonical sibling semantics doc: `/Users/pixexid/Projects/amiga_house_cleaning_company_docs/operations/operations_standards.md`. The bot answers operator questions about the standard kit, the color code, room SOPs, stains, safety rules, PPE, SDS folder structure, exception-log shape, training quiz items, rollout plan, and external source citations against this doc.
- Surface spec: `/Users/pixexid/Projects/amiga/design/surfaces/resources.md`.
- Lane scope update: the read-only admin Resources surface remains anchored at `/design/resources`, and GH-503 adds the admin-only Resources operations API for list / create / detail / patch / publish / archive / restore / version history on the GH-502 schema. The bot may acknowledge that live resource rows and audit history now exist for admin runtime management, but it must not promise uploads, SDS attachments, freshness tracking, link-health probes, or a rendered `/app` management UI until those downstream lanes ship. The bot must not surface the staff field guide or interactive scored quiz as live admin features — those are deferred to GH-471. The bot must not present operator vocabulary (exception log, retired-product list, SDS structure, source citations) as customer-facing content — the customer-side tone variant is deferred to GH-472 and routes through `amiga-concierge`.
- Practical answers the bot should give cleanly:
  - "Where do we record off-kit product usage?" → Exception log section in `/design/resources`; admin runtime management can persist resource rows and version history through the GH-503 Resources API once the management UI consumes it. Uploads, SDS attachments, link-health probes, and freshness tracking remain downstream.
  - "What cloth on this zone?" → Color code section; cloth follows the room or zone, not the product.
  - "Is bleach in the daily caddy?" → No — owner-approved exception only; daily kit avoids odor / overspray / mixing risk.
  - "What is the bathroom SOP?" → Room SOPs section; Purple sticker + purple cloth (if available) for non-toilet; **disposable wipe for toilet (no red cloth)**; Blue sticker + glass cloth for mirrors; ten-step procedure.
  - "Which sticker goes with which floor product?" → Yellow = Zep Neutral pH (tile / vinyl / VCT / sealed concrete); Brown = Bona hardwood / sensitive (sealed hardwood / laminate / LVP). Mop-pad color is NOT controlled — Amiga uses a flat mop or standard mop pad in any pad color.
  - "Is there a red cloth?" → No. Red is the toilet *product* sticker only; the toilet workflow is disposable wipes.
  - "Which sticker is on the stone cleaner?" → Operator-intended Tan / Gold, but the current 10-sticker pack does not include Tan / Gold. Mark Stone as "sticker pending" and reassign Pink or Mint as a temporary substitute until a Tan / Gold sticker is sourced.
  - "Did we cite a source for X?" → Sources section; external links are visible in the admin Resources surface, and freshness tracking remains deferred.

## Editable Resources management cross-reference (GH-473)

- **Status: IMPACTED as pointer-only.** The admin-only sandbox management route now exists at `/design/resources/manage`.
- Surface spec: `/Users/pixexid/Projects/amiga/design/surfaces/resources-manage.md`.
- Summary for the bot: if an operator asks where editable Resources management is being designed, point to `/design/resources/manage`. It supports sandbox-local create / edit / pending review / publish confirmation / validation states against the Resources fixture projection. GH-503 now supplies the admin-only API that a future `/app` management surface can consume; the design sandbox itself still does not write to Supabase, upload SDS files, or run link-health probes.
- Boundary: continue answering operational standards questions from `/design/resources` and `operations/operations_standards.md`. Treat `/design/resources/manage` as a design sandbox until the runtime `/app` management surface consumes the GH-503 API.

## Client Resources guide cross-reference (GH-472 / GH-488)

- **Status: NOT IMPACTED beyond pointer/cross-reference.** The customer-facing Resources guide ships at `/app/cleaning-guide` and is owned by `amiga-concierge`; the operator-cmo bot's own KB is unchanged.
- Pointer: if an operator asks "where do customers see what we use", point at `/app/cleaning-guide` (accepted design spec `/Users/pixexid/Projects/amiga/design/surfaces/client-resources.md`) and the concierge rails at `/Users/pixexid/Projects/OpenClaw_bots/bots/amiga-concierge/spec.md` → "Client Resources Rails".
- Boundary: the operator-cmo bot continues to answer operator questions against the admin Resources surface (`/design/resources`) and the sibling semantics doc (`amiga_house_cleaning_company_docs/operations/operations_standards.md`). It must NOT present the operator vocabulary (exception log, SDS internals, source citations, retired-status, sticker-to-cloth taxonomy, mop-pad rules) as customer-facing content — that lane belongs to the concierge.

## Booking change-request queue expansion (GH-481)

- **Status: IMPACTED.** The operator change-request queue now includes recurring-booking request types `cadence_change`, `skip_visit`, and `end_series` alongside the existing service/add-on note flows.
- Runtime boundary: the bot can summarize these as customer requests pending review on a booking, call out the linked booking/task context, and distinguish review states (`pending`, `approved`, `rejected`, `applied`) for operator questions.
- Decision rail: the bot must not claim that cadence changes or end-series decisions automatically rewrote the recurring schedule in this lane. Those decisions remain review records until an operator performs any follow-up schedule work separately. `skip_visit` may only point to the existing linked-task skip/cancel path when a task already exists for that visit.

## View-as — REMOVED (sunset 2026-07-16)

- **Status: NOT IMPACTED — the feature does not exist.** View-as (GH-496) was removed by operator decision under umbrella GH-1518. There are no View-as sessions to start, detect, pause for, or exit.
- **There is NO tool-pause rail.** The bot must never pause tools for View-as, never emit `Paused while you're viewing as <Name>`, and never reason about an active session.
- **Never tell an operator they can view as a customer or staff member.** No such capability ships. If asked, say it does not exist rather than describing it as unavailable, disabled, or coming back.
- Sunset record: `/Users/pixexid/Projects/amiga_house_cleaning_company_docs/operations/security/impersonation.md`.

## Recurring series identity (GH-924, Phase 1 — display-layer)

- **Status: IMPACTED.** A recurring purchase is ONE booking: the parent booking id (e.g. `B-1699`) is the only customer-visible identity for the whole series. Instance rows exist in the data model today (`recurrence_parent_booking_id`) but their booking numbers are suppressed everywhere customer- or roster-facing; the Operations Bookings roster renders one SeriesCard per series, anchored in the next upcoming visit's date group, with an expandable visit ledger.
- Bot guidance: when an operator asks about a recurring booking, answer with the parent id and visit ordinals ("B-1699, visit #03 on May 19"), never an instance booking number. Per-visit actions (skip, reschedule) target the visit; series actions (end series, cadence change) target the parent. Phase 2 (GH-907) replaces ordinals with task ids `T-1699-01…`.
- Spec: `design/surfaces/operations-bookings.md` § "Recurring series card (GH-924 Phase 1)" in the app repo.

## Staff assignment operability (GH-929, 2026-06-12)

- Summary for the bot: admins assign or reassign cleaning staff from three equivalent entry points, all backed by the same staff directory: (1) the Operations → Tasks queue — every row carries an `Assign` (unassigned) or owner-chip/`Reassign` affordance that opens a quick-assign panel; (2) the task detail rail's Assignment tab; (3) a booking drawer's Execution tab — each task shows its assignees and an Assign/Reassign link that deep-links to `?view=tasks&taskId=<id>` with the rail open at Assignment.
- The picker lists ALL active staff grouped by crew (code + region). Workforce fit shows as visible hints (`Guest` for non-core staff, `No profile` when the eligibility row is missing) — eligibility never silently hides anyone. Inactive staff and `test_local` fixtures are excluded.
- Toggles save immediately (replace-all `set_assignees`); admins may assign guest staff to any region on demand.
- Dispatch → run staffing: selecting a run shows "Run staffing" with explicit verbs — `Assign staff` moves every open stop on the run to the chosen cleaner and updates each task's assignees (staff notified); `Return run to pool (unassigned)` clears staff from the run's open tasks and leaves the run schedulable. Route↔task assignee truth is synced server-side in both directions (assign-task syncs run staff into `task_assignments`; run-staff reassignment cascades to non-terminal tasks).

## Service Notes social draft review (Lane C2 — GH-1493, 2026-07-16)

- **Status: IMPACTED.** The Service Notes pipeline (`/app/service-notes/notes`) now carries a per-channel social-review panel on the **published**-note detail, opened via `?noteId=<uuid>&panel=social`. The panel is authoritative only for a published note — the URL normalizes `panel=social` away for any non-published or since-archived/corrected note.
- The admin reviews the deterministic three-channel set (Google Business / Instagram / LinkedIn) that Lane C1 generates: each draft shows its server-owned metadata (revision, decision actor/time, price mode, canonical URL, narrative, source fingerprint) as read-only, with editable allowlisted copy (post text, hashtags, image brief).
- Edits are validated client-side as a mirror of the shipped server authority — UTF-8 byte bounds, the per-channel word/sentence profile, and hashtag shape/count/deduplication — so Save is blocked with an accessible reason instead of a server 400; the server remains the sole authority.
- Per-channel **approve / reject / reopen** and an atomic **approve-all** are available. Approval is review-only: it never schedules, publishes, sends, or otherwise delivers anything. Downstream external delivery remains the deferred Lane D (self-hosted Postiz) and is out of scope here.
- **Fail-closed on a terminal 403.** A refused mutation (403 from `requireOperationalWriter` — an account without write access) locks the panel: every mutation control (approve/reject/reopen/approve-all, the edit fields, save/discard) disables, the drafts stay readable, and it shows *"Your account can't make changes here. These drafts are read-only for you."* The lock is recorded against the refused authority, so it survives closing/reopening the panel and a plain Refresh, and clears only when the acting authority actually changes. Retrying with the same authority would be refused again, so the panel never re-offers the action. The server remains the authority. **Bot behavior: a 403 here means the account lacks write access — say that plainly. Never tell an operator to exit a View-as mode: View-as was removed in the GH-1518 sunset and does not exist.**
- **The empty state offers NO generation (2026-07-16).** C2 is **review-only**. A published note with no current set shows a truthful non-action empty state — "Drafts aren't generated from this screen" — with **no button and no link**. Do not tell an operator they can generate social drafts from the review panel, or from anywhere else in the app: **no shipped UI can generate them today.** The only trigger is `POST operations.service-notes.social.generate`. (The editorial hub's "Generate draft" is a different thing — it generates the *story brief*, not social drafts.) Historically, the affordance was split out because the server contract could not prove settlement; GH-1515 now supplies that backend authority, while the visible Generate control remains deferred to P4. Approval/scheduling/publication are unaffected: nothing is scheduled, published, or sent.
- The surface adds no new schema, approval authority, generation, or delivery/Postiz call. Review/approve/reject/reopen/edit are pure C1-consumer actions, and they are the ONLY server-mutating paths it introduces — the split-out generation affordance is not one of them (see above).
- **Backend generation authority exists, but the affordance remains deferred (GH-1515).** The app now
  has an admin-authenticated, read-only generation-status contract with bounded
  `claimed | running | succeeded | failed | blocked | lease_expired | provider_outcome_unknown` states and a server-derived
  `safeToStart` decision. This does not add a bot tool, provider call, scheduling, delivery, publication,
  or a visible Generate control. Until the separate P4 UI child ships, do not tell an operator that the
  Service Notes review panel can generate social drafts.
