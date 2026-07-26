# API Services Map

This is the consolidated map of external services used by the quote/booking system. It lists purpose, endpoints, and where each service is used.

## Auth + Persistence (Implemented)

### Supabase Auth
- **Use:** Google OAuth and email/password auth in booking + authenticated dashboard flows (`/app/*`)
- **Client SDK:** `@supabase/supabase-js`
- **Public env vars:** `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`
- **Notes:** Session token is sent as bearer auth to internal save/book endpoints

### Service Notes Social Review Endpoints (GH-1491 C1)
- **Use:** Read the current deterministic three-channel Lane B draft set and apply Amiga-owned social
  review transitions. This is private review authority only; it does not schedule, call Postiz or any
  social provider, deliver, or publish.
- **Browser Endpoints:** `GET /api/operations/service-notes/social/review?noteId=<uuid>`,
  `POST /api/operations/service-notes/social/review`
- **Auth/cache:** `Authorization: Bearer <supabase access token>`; admin only; origin checked; all
  responses are `private, no-store`. GET uses the `service_note_editorial_reads` limiter; POST uses
  `service_note_lifecycle`.
- **GET response:** `{ source, drafts }`, where source is bounded to
  `{ noteId, slug, title, publishedAt, currentFingerprint }` and drafts are the current non-invalidated
  `google_business_profile`, `instagram`, and `linkedin` rows in that order. Each draft includes review
  state/revision, editable copy, server-owned price/URL/narrative/disclosure authority, decision
  provenance, timestamps, and explicit stale/invalidation disposition. Missing current set is 404.
- **POST payloads:** exact allowlists only:
  - `{ action:"edit", idempotencyKey, queueId, expectedRevision, postText, hashtags, imageBrief }`
  - `{ action:"approve"|"reject"|"reopen", idempotencyKey, queueId, expectedRevision }`
  - `{ action:"approve_set", idempotencyKey, targets:[{queueId,expectedRevision} x3] }`
  - every `expectedRevision` is a PostgreSQL `integer` contract: an exact integer from 1 through
    2147483647; larger JavaScript safe integers fail with 400 before persistence
- **POST response:** 200 `{ status:"applied"|"replayed", operationId, drafts }` for an applied or
  still-current deterministic replay. 409 conflict codes are `idempotency_conflict`,
  `superseded_replay`, `stale_revision`, `stale_source`, `draft_invalidated`, `illegal_transition`,
  and `incomplete_draft_set`. An `approve_set` row conflict also returns the bounded offending
  `channel` (`google_business_profile`, `instagram`, or `linkedin`) without returning copy/content.
  Superseded replay returns recorded prior/outcome state, revision, decision provenance, and exact
  changed-field names only, never reconstructed obsolete copy. Unsupported fields and malformed
  identifiers are 400.
- **Retry preflight:** before current-revision, invalidation/staleness, lifecycle, or copy checks, the
  service performs an authenticated read-only operation lookup. Only the same actor and exact
  canonical request fingerprint can proceed directly to database replay; a reused key with any
  different target, revision, action, or editable payload is `idempotency_conflict`. A newly absent
  operation still runs every current-state and complete copy-safety gate.
- **Database authority:** caller-scoped RPCs lock rows in canonical GBP → Instagram → LinkedIn order,
  require expected revisions and current source fingerprints, store one immutable globally unique-key
  parent operation plus immutable per-row audit children, bind the parent to each locked queue row's
  actual pre-mutation revision/state, bind each child to the parent's requested revision and exact
  action-specific state/field outcome, and re-run the complete Lane B final-copy validator across post
  text, hashtags, and image brief before edit/approval persistence. Provenance conflicts include
  bounded hyphen/underscore/camel and compact hashtag compounds after Unicode format characters are
  removed.

### Saved Quotes Endpoint
- **Use:** Fetch authenticated saved quote history
- **Browser Endpoint:** `GET /api/quotes/list`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token via Supabase, loads quotes + quote_addons for the authenticated profile

### Quote Save Endpoint
- **Use:** Persist authenticated quote drafts
- **Browser Endpoint:** `POST /api/quotes/save`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token via Supabase, upserts profile/property, inserts quote and quote_addons
- **Auth-flow behavior:** save-intent quotes created before an authenticated
  session exists, including email-confirm sign-up and OAuth redirect flows, are
  queued in browser storage and retried through this endpoint on the next
  authenticated session. Failed retries keep the pending local quote instead of
  clearing it.

### Booking Create Endpoint
- **Use:** Persist full booking + quote snapshot + optional walkthrough files
- **Browser Endpoint:** `POST /api/bookings/create` (multipart/form-data)
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token via Supabase, resolves Google place coordinates before persistence, then calls the service-role-only `create_booking_atomic` RPC to atomically upsert the client profile/property, ensure the canonical `client_accounts` row for the booked customer, insert the quote + quote_addons, create the parent booking with `bookings.intake_channel = 'website'`, and record the initial `booking_created` event. Post-commit work remains outside that RPC: Stripe manual-capture authorization, walkthrough uploads to Supabase Storage bucket `booking-walkthroughs`, capacity holds, operational tasks/scheduling, recurrence children, reminder notifications (`booking_reminder_24h`, `booking_reminder_2h`) via `upsert_booking_reminders`, and confirmation notification enqueueing. SetupIntent duplicate protection uses a partial unique index on provisional `bookings.payment_reference = setup_intent:*`; same-process serialization still protects local concurrent submissions.
- **Walkthrough media contract:** move-in/out bookings still require at least one image or video before booking confirmation. Post-construction bookings show the same walkthrough upload control, but uploads are optional; zero-file post-construction bookings can still complete. Uploaded walkthrough media is stored in the private Supabase `booking-walkthroughs` bucket and recorded through `walkthrough_media_uploaded` booking events.

### Booking List Endpoint
- **Use:** Fetch authenticated booking history for dashboard
- **Browser Endpoint:** `GET /api/bookings/list`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Query contract:** `archived=exclude|include|only` (`exclude` default)
- **Server behavior:** validates token via Supabase, loads bookings (schedule/payment) with quote-linked address snapshot and reminder summary (`pendingCount`, `nextScheduledAt`) from `notifications`, excludes archived closed bookings from the default response, and only returns archived bookings when the caller opts into archive/history access explicitly
- **Inbox intake source:** the list contract includes nullable `intake_channel` for operator Inbox source fidelity. Valid persisted values are `website`, `phone`, `concierge`, and `referral`; legacy `NULL` rows continue to derive source from the existing customer-phone heuristic.
- **Resolved crew/team (GH-750 PR-C):** for admin/staff callers the list contract resolves a `team` per booking (`TeamCode | null`, one of `SAN1|NOP1|MIP1|SOB1|COA1`). Resolution (`loadBookingTeamCodes`, `src/server/booking-team-resolution.ts`): the booking task's route run `route_runs.team_id` team when set, else the assigned staffer's explicit-`team_members`-then-zone `resolveTeamCode` (D7). `null` is the honest unstaffed state. The operator surface renders it as a `TeamBadge` (list card header + drawer identity row) and drives the `Needs crew` facet (`team === null`, terminal bookings excluded). Render-layer `TeamCode` only — **no `teams.short_code` column**.
- **Real per-booking crew membership (GH-818):** the list contract also carries `crew` (`CrewLegendTeam | null`) for admin/staff — resolved via `loadCrewLegendTeamsByCode` (`src/server/crew-legend.ts`) over the distinct resolved team codes (bounded; skipped when none). `{ code, fullName, region, lead, members[] }`. It drives the bookings `TeamBadge` hover/focus tooltip (real members, lead marked; "No assigned members" when empty) — DESIGN §8 parity with Dispatch; `null` when the booking has no resolved team.
- **Current operator UX:** `/app/bookings` keeps archived closed bookings out of the main workspace and exposes them only through a dedicated `Archived` filter; archive/restore actions appear for cancelled and completed bookings
- **Operations Bookings drawer context:** the Operations booking list contract also reads `quotes.price_total` from the existing linked quote record so the drawer Overview can show a real `Quoted total` beside `Authorized hold`; absent totals are omitted rather than rendered as `$0`

### Booking Detail + Draft Completion Endpoint
- **Use:** Fetch booking detail and complete intake-created booking drafts from booking detail
- **Browser Endpoint:** `GET /api/bookings/:bookingId`, `POST /api/bookings/:bookingId`, `POST /api/bookings/:bookingId/cancel`, `POST /api/bookings/:bookingId/reschedule`, `POST /api/bookings/:bookingId/archive`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior (`GET`):** validates token + booking access, returns booking detail, archive metadata, availability assessment snapshot, operational estimate, notifications, timeline, linked task details, staff/admin-only `client` contact detail, `reminderHooks` derived from the persisted schedule window + estimated elapsed minutes, staff/admin-only `walkthroughMedia` signed URLs for `walkthrough_media_uploaded` booking events, and staff/admin-only `route` detail when one of the booking's tasks is attached to a route stop
- **Walkthrough media read (`GET`, staff/admin):** `booking.walkthroughMedia` is `null` when no walkthrough media exists. When media exists, entries include file metadata plus short-lived signed URLs minted from the private `booking-walkthroughs` bucket for operator/staff viewing in the booking detail gallery. Customer projections do not expose these signed URLs.
- **Route detail (`GET`, staff/admin):** `booking.route` is `null` for unrouted bookings. Routed bookings include `runId`, numeric `runNo`, route run `status`, `teamCode`, real crew legend data (`fullName`, `region`, `lead`, `members`), this booking stop's `stopEtaAt`/`arrivedAt`, and the route's next uncompleted stop ETA. Customer projections do not expose this operator route context.
- **Server behavior (`POST`):** staff/admin only; validates the draft-completion payload for unscheduled intake-created drafts, reuses shared booking availability evaluation, persists cadence/date/window onto the existing booking + quote records, creates the standard capacity hold/task/reminder/confirmation records only after schedule save succeeds, and keeps the existing payment authorization path on the same booking record
- **Server behavior (`POST /cancel`):** staff/admin only; durably cancels eligible `pending`/`confirmed` bookings from booking detail before downstream cleanup runs, clears active booking tasks, route stops plus empty auto route runs, capacity holds, and pending notifications on a best-effort basis, records a structured `booking_cancelled` audit event, enqueues the `booking_cancelled` customer email notification, and auto-voids authorized Stripe holds when possible while returning an operator warning when manual follow-up is still required
- **Server behavior (`POST /reschedule`):** staff/admin only; validates the new date/window against availability, updates the booking and linked operational task/route state, records a structured `booking_rescheduled` audit event with previous and next schedule values, persists the recomputed availability as a `booking_availability_evaluated` event with `trigger = "reevaluation"`, enqueues the `booking_rescheduled` customer email notification on the existing booking/channel/template idempotency key, refreshes booking reminders best-effort, and sends operator awareness email for date changes, first scheduling of an unscheduled booking, and arrival-window-only changes.
- **Server behavior (`POST /archive`):** staff/admin only; archives or unarchives cancelled or completed bookings without deleting them, requires linked tasks to be done/cancelled and route-stop remnants to be cleared before archive, records `booking_archived` / `booking_unarchived` audit events, and keeps direct booking detail fetches available even when the booking is hidden from default list/history responses
- **Current operator UX:** booking detail shows archive state and uses toast-backed archive/restore actions for cancelled and completed bookings
- **Notes:** this endpoint is for completing a previously created booking draft in place; it must not create a duplicate booking or invent cadence defaults

### Booking Payment Authorize Endpoint
- **Use:** Start the customer payment step for either the public booking flow or an existing customer-owned booking
- **Browser Endpoint:** `POST /api/bookings/payment/authorize`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior (existing booking):** validates token + booking ownership, checks the booking is still eligible for payment, creates a Stripe `Customer` + `SetupIntent` through the fetch-based REST wrapper, stamps binding metadata for the authenticated profile + booking id, and records `payment_setup_intent_created`
- **Server behavior (public quote flow):** validates token + quote payload, creates a Stripe `Customer` + `SetupIntent`, stamps binding metadata for the authenticated profile + quote fingerprint, and returns the setup client secret plus the browser publishable key without creating the booking yet
- **Response:** inline setup payload (`setupIntentClientSecret`, `setupIntentId`, `publishableKey`, `amount`) for the Stripe Elements flow
- **Customer booking-confirmation retry (GH-716):** the customer `/app/bookings/$bookingId` surface renders a `Retry payment` PillBtn anchored under step 1 of the status tracker when the customer-visible payment is in attention and the booking is still payment-eligible (`pending`/`confirmed`). It reuses this existing `{ bookingId }` path verbatim — no new endpoint — and opens the existing `CustomerPaymentSetupDialog` on the returned `SetupIntent`. If authorization returns a non-OK response before the dialog opens, the customer view shows the standard payment-save failure message inline on the booking detail page.

### Booking Payment Confirm Endpoint
- **Use:** Confirm either a legacy hosted Checkout session or a newly completed inline setup step for an existing booking
- **Browser Endpoint:** `POST /api/bookings/payment/confirm`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior (legacy hosted path):** validates token + booking ownership, verifies the Stripe Checkout session, updates booking payment fields (`payment_status`, `payment_provider`, `payment_reference`), and records `payment_authorized`, `payment_captured`, or `payment_failed`
- **Server behavior (inline path):** validates token + booking ownership, verifies the confirmed `SetupIntent`, enforces that the setup metadata still matches the authenticated profile + intended booking, creates a manual-capture Stripe `PaymentIntent` for the booking from the saved payment method, updates booking payment fields, and records `payment_setup_confirmed` plus the resulting booking event

### Account Communication Preferences Endpoint (GH-838 / 826c)
- **Use:** Read and update the signed-in customer's marketing communication preferences from `/app/account` → `Marketing & promotions`.
- **Browser Endpoint:** `GET /api/account/communication-preferences`, `POST /api/account/communication-preferences`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Payload (`POST`):** `{ field: "marketing_email" | "marketing_sms", optIn: boolean }`
- **Server behavior:** validates the caller and uses the service-role client to read/write `communication_preferences`. Marketing email and marketing SMS default OFF. Marketing SMS opt-in requires a normalized phone number on the profile and records proof in `marketing_sms_consents`.
- **Consent isolation:** marketing SMS proof must never be written to `sms_consents`; the transactional SMS account state from GH-834 reads `sms_consents` without a marketing source filter, so marketing proof has its own table and cannot enable booking/service texts.
- **Current sender state:** no marketing email/SMS sender is wired. The endpoint records a future preference only; it does not dispatch marketing messages.

### Stripe Webhook Endpoint
- **Use:** Receive async Stripe payment lifecycle events from Stripe servers
- **Worker Endpoint:** `POST /api/stripe/webhook`
- **Auth:** Stripe signature verification (`Stripe-Signature` header)
- **Server behavior:** verifies signed payload with `STRIPE_WEBHOOK_SECRET`, maps supported event types (`checkout.session.completed`, `payment_intent.amount_capturable_updated`, `payment_intent.succeeded`, `payment_intent.canceled`, `payment_intent.payment_failed`) into booking payment lifecycle updates, and appends webhook-sourced booking events for auditability
- **Server env:** `STRIPE_WEBHOOK_SECRET` (required for webhook verification)

### Operations Tasks Endpoint
- **Use:** Fetch staff/admin task queue and task summary cards; mutate operational task assignment, checklist, and explicit completion state
- **Browser Endpoint:** `GET /api/operations/tasks`, `POST /api/operations/tasks`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role, loads tasks from `tasks` + `task_assignments`, and returns delivery-free `reminderHooks` for scheduled/due tasks so ops surfaces can show leave-soon, arrival-window, service-start, and wrap-up readiness without sending notifications
- **Client freshness:** staff My Day task/checklist mutations invalidate both My Day and Operations task state in the current session. Admin Operations Tasks also auto-refreshes while the page is visible so completed checklist/task state appears without a manual reload in separate admin sessions.
- **Assignment side effects:** admin `set_assignees` saves remain replace-all, but the server diffs previous assignees before replacing rows. Newly assigned booking-linked staff receive `staff_assignment_published` through the staff bell and a recipient-addressed staff email row in `notifications`; unchanged assignees are not re-notified. Staff notification links target `/app/my-day?taskId=<taskId>` so My Day can open the assigned task. Each successful assignment save also creates an operator-facing `staff_update` app notification as confirmation.
- **Completion actions:** checklist mutations that derive a task to `done` run the booking terminal-completion check. Admins may also `POST` action `complete_task` with `{ action: "complete_task", taskId, notes? }` to explicitly mark a task `done` even when no checklist items exist or an operator override is required. Staff cannot call `complete_task`.
- **Completion side effect:** a single completed task does not send service-complete mail by itself; completion requires every non-cancelled booking task to be `done` and payment to be terminal (`captured`/`refunded`) unless the quote total is zero. When those guards pass, the API marks the booking `completed`, records `booking_completed`, enqueues `service_complete` plus delayed `review_request`, and completes attached route runs only after all non-cancelled stops/tasks are complete. Explicit completions also store operator attribution in task details and the `task_completed` audit payload, then create operator/staff `staff_update` app notifications for completion awareness. Retry repair of an already explicit-completed task does not duplicate those awareness notifications.
- **Completion response:** `complete_task` returns `{ ok, taskId, status: "done", bookingId, bookingStatus, bookingCompletion }` so operator UI can distinguish "task completed" from "booking also completed".
- **Display handle:** each task carries `taskNo` from `tasks.task_no`, a bare bigint sequence intended for display as `T-<number>` by clients. The prefix is render-only; the API returns the numeric value.

### Operations Clients Endpoint
- **Use:** Fetch admin client detail/list data, persist client/property mutations, and create booking drafts from baseline-backed client properties
- **Browser Endpoint:** `GET /api/operations/clients`, `POST /api/operations/clients`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior (`GET`):** validates token + requires `admin` role, then returns either the client list or a specific client detail payload with property `servicePlanBaseline` state
- **Server behavior (`POST`):** validates token + requires `admin` role, then supports direct client/property mutations, wizard `create_client_with_booking_draft`, and `create_booking_draft_from_property`, which seeds a new booking draft from the saved property baseline, property address context, and stored pricing snapshot before routing operators to `/app/bookings/$bookingId`.
- **Existing-email booking-ready behavior:** the wizard booking-draft path reuses an existing client profile/account when the submitted email is already known. If Supabase auth owns the email but the app profile shell is missing, the server attaches the missing lightweight profile/account shell before writing the property and draft. Existing clients are preserved on rollback; only newly created artifacts are removed.

### Operations Staff Endpoints
- **Use:** Fetch the admin-only staff directory, invite new staff/admin users, edit staff settings, and deactivate staff accounts
- **Browser Endpoints:** `GET /api/operations/staff`, `POST /api/operations/staff`, `GET /api/operations/staff/:staffId`, `PATCH /api/operations/staff/:staffId`, `DELETE /api/operations/staff/:staffId/deactivate`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior (`GET /api/operations/staff`):** validates token + requires `admin` role, loads `profiles` plus `staff_profiles`, folds in same-day date blocks from `staff_unavailability`, and returns the operational directory with role, employment status, zone assignments, capacity pill state, and active-task counts
- **Server behavior (`POST /api/operations/staff`):** validates token + requires `admin` role, sends a Supabase admin invite with a reset-password redirect, creates/updates the linked `profiles` row, and persists staff settings in `staff_profiles`
- **Server behavior (`GET /api/operations/staff/:staffId`):** validates token + requires `admin` role, returns full staff detail including preferred weekly schedule and current active task assignments
- **Server behavior (`PATCH /api/operations/staff/:staffId`):** validates token + requires `admin` role, updates `profiles`, upserts `staff_profiles`, replaces the staff member's date-level `staff_unavailability` set, and syncs the auth user metadata
- **Server behavior (`DELETE /api/operations/staff/:staffId/deactivate`):** validates token + requires `admin` role, marks staff as inactive in `staff_profiles`, updates auth metadata, and rejects deactivation for admin accounts
- **Notes:** `/app/staff` is the admin-only surface for this contract; staff settings now drive scheduling eligibility through zone coverage, preferred schedule windows, and active/inactive employment state

### Operations Teams Endpoints
- **Use:** Manage explicit crew/teams (GH-750) from the admin-only Staff > Teams tab — create/edit teams, assign/remove members, set exactly one lead per team, and toggle active/inactive
- **Browser Endpoints:** `GET /api/operations/teams`, `POST /api/operations/teams`, `PATCH /api/operations/teams/:teamId`, `POST /api/operations/teams/:teamId/members`, `PATCH /api/operations/teams/:teamId/members`, `DELETE /api/operations/teams/:teamId/members?profileId=<id>`
- **Auth:** `Authorization: Bearer <supabase access token>`; every endpoint validates token + requires `admin` role (rate-limited under `operations_teams`)
- **Server behavior (`GET`):** returns all `teams` (id, code, name, zone_id, active) with their `team_members` (profile_id, role) grouped per team
- **Server behavior (`POST /api/operations/teams`):** creates a team (`code` unique, `name`, optional `zone_id` in the canonical zone set, `active` default true); 409 on duplicate code
- **Server behavior (`PATCH /api/operations/teams/:teamId`):** partial update of code/name/zone_id/active (archive = `active:false`, restore = `active:true`)
- **Server behavior (`POST .../members`):** adds a member (`profileId`, `role` member|lead); promoting to lead first demotes any existing lead so the one-lead-per-team partial-unique index always holds
- **Server behavior (`PATCH .../members`):** sets a member's role (member|lead) with the same lead-demotion guarantee
- **Server behavior (`DELETE .../members?profileId`):** removes a member from the team
- **Notes:** crew is OPTIONAL on a run — `route_runs.team_id` stays nullable and solo runs keep using `route_runs.staff_profile_id`; these endpoints never alter solo-run behavior. No new DDL (tables shipped in GH-750 PR-A).

### Operations Routes Endpoint
- **Use:** Fetch staff/admin route runs, scheduler events/resources, and map stops
- **Browser Endpoint:** `GET /api/operations/routes`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role, loads `route_runs` + `route_run_stops`, joins task/property context, and returns:
  - `runs` (summary/list metrics)
  - `scheduler.resources` (staff resource grouping)
  - `scheduler.events` (Amiga-owned scheduler event payload for timeline rendering, including recurrence fields when available)
  - `mapStops` (route stop map payload with geocoordinates when available)
  - Crew/team code projections resolve from explicit `teams`/`team_members` membership first, then fall back to the existing `staff_profiles.zone_ids` mapping while Staff > Teams operator management is still rolling out. Solo runs continue to use `route_runs.staff_profile_id`; crew runs may also set nullable `route_runs.team_id`.
  - **Crew membership (GH-750 PR-C):** each run also carries `crew` (`CrewLegendTeam | null`) — the run team's real membership (`teams` + `team_members` joined to member `profiles.full_name`, built by `loadCrewLegendTeamsByCode`, `src/server/crew-legend.ts`): `{ code, fullName, region, lead, members[] }`. It drives the Dispatch `CrewLegendTooltip` (full name · region · members, lead marked). `null` for solo/unstaffed runs — no fabricated crew.
  - UI adaptation now happens inside the route-board dispatch calendar wrapper; the API contract itself uses Amiga-owned scheduler keys (`id`, `title`, `startTime`, `endTime`, `resourceId`, `routeRunId`, etc.)
  - Compatibility fallback: if recurrence DB columns are missing, recurrence values return `null` and route loading remains functional
  - **Display handle:** each route run carries `runNo` from `route_runs.run_no`, a bare bigint sequence intended for display as `R-<number>` by clients. The prefix is render-only; the API returns the numeric value.
- **Deep-link contract:** operator notifications and dashboard activity should target concrete route state with `/app/operations?view=dispatch&selectedRunId=<routeRunId>` plus optional `dispatchExceptionType`/`dispatchExceptionSummary` when opening an exception context.

### Operations Route Update Endpoint
- **Use:** Persist scheduler drag/resize updates for route runs
- **Browser Endpoint:** `POST /api/operations/routes/update`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role, enforces assignment constraints for staff, validates assigned profile role (`staff/admin`), blocks same-staff time conflicts for planned/in-progress routes, and supports two scheduler mutation modes:
  - `operation: "update_run"` updates a route run (series edits included via `recurrence_rule` / `recurrence_exception`)
  - `operation: "upsert_occurrence_override"` upserts a single recurrence instance override and appends/updates the parent run `recurrence_exception`
- **Create-run behavior:** `operation: "create_run"` is admin-only and creates an empty planned manual run. The response includes the newly allocated numeric `runNo` so the UI can merge/select the run without waiting for a route-list reload.
- **Compatibility behavior:** if recurrence columns are not migrated yet, recurrence writes are rejected with a migration-required response while non-recurrence schedule edits still work

### Operations Route Optimize Endpoint
- **Use:** Optimize stop order + ETA/travel estimates for a route run
- **Browser Endpoint:** `POST /api/operations/routes/optimize`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role, enforces assignment constraints for staff, loads route stops/tasks/properties, then:
  - keeps manual stops (`is_manual`) fixed
  - requires at least 2 geocoded non-manual stops
  - optimizes movable stops and rewrites `stop_order`, `eta_at`, `start_at`, `end_at`, `travel_minutes`, and `distance_miles`
  - updates `route_runs.start_at`, `route_runs.end_at`, and `route_runs.estimated_total_minutes`
  - returns warning feedback (missing coordinates, service-window risk, overtime risk)

### Operations Route Unassign Task Endpoint (GH-751)
- **Use:** Remove a solo route/assignment for a booking task back to the unscheduled pool, recording a real bounce-back signal
- **Browser Endpoint:** `POST /api/operations/routes/unassign-task`
- **Auth:** `Authorization: Bearer <supabase access token>`; **admin only**
- **Payload:** `{ taskId: string, reason?: string | null }` (reason optional, ≤500 chars)
- **Server behavior:** validates token + requires `admin` role, rate-limited (`operations_routes_unassign_task`); resolves the task's booking, removes the task's route stop, clears the solo assignee(s), resets booking `operational_status` to `task_created`, and records a text-only `assignment_bounced_back` booking event with payload `{ previousRouteRunId, previousStopId, previousStaffProfileIds, reason, actorProfileId, bouncedBackAt }`. Returns `404` if the task is missing, `409` (`TaskNotAssignedError`) if the task has no route stop. This is the only producer of bounce-back state — it is never inferred from `operational_status`, and the reassignment delete-then-reinsert path does NOT emit it.
- **Consumers:** bookings list view-model derives `bouncedBack`/`bouncedBackReason` from the latest `assignment_bounced_back` event; the Operations Bookings card renders the `Bounced back` chip and the drawer Overview renders the reason banner; the booking detail Audit timeline includes the event.

### Booking Assign Crew Endpoint (GH-1161)
- **Use:** Assign or reassign a booking's execution task to a crew from a booking-scoped workflow.
- **Browser Endpoint:** `POST /api/bookings/:bookingId/assign-crew`
- **Auth:** `Authorization: Bearer <supabase access token>`; **admin only**
- **Payload:** `{ teamId: string, staffProfileId?: string | null }`
- **Server behavior:** validates token + requires `admin` role, rate-limited (`bookings_assign_crew`); validates the active crew and optional member; ensures a booking-linked cleaning task exists through the existing task-create path; creates or updates one dispatch `route_runs` row for that task/date and links it with `route_run_stops`. Replays with the same booking/team/date return the existing run. Response: `{ success, taskId, runId, teamId, assignmentStatus, runLink }`.

### Operations Payment Capture Endpoint
- **Use:** Capture previously authorized booking deposits
- **Browser Endpoint:** `POST /api/operations/payments/capture`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role, verifies booking/payment state (`authorized`), resolves Stripe payment intent from booking payment reference, captures payment intent, updates booking payment fields, and records `payment_captured` booking event
- **Completion side effect:** after a successful capture, the API runs the same terminal-completion check used by task completion. If all non-cancelled tasks are already `done`, this is the payment-side join that marks the booking `completed`, records `booking_completed`, and enqueues the completion/review notifications.

### Operations Payment Void Endpoint
- **Use:** Void previously authorized booking deposits
- **Browser Endpoint:** `POST /api/operations/payments/void`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role, verifies booking/payment state (`authorized`), resolves Stripe payment intent from booking payment reference, cancels payment intent, updates booking payment fields, and records `payment_voided` booking event

### Operations Change Requests Endpoint
- **Use:** Fetch staff/admin change-request review queue
- **Browser Endpoint:** `GET /api/operations/change-requests`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role, loads `task_change_requests` with task/profile context
- **Notification target:** customer-submitted change-request admin notifications currently deep-link to the specific booking card with `/app/operations?view=bookings&bookingId=<bookingId>`. The change-request queue itself does not yet expose a supported `requestId` search target.

### Operations Customer Reports Endpoint
- **Use:** Fetch submitted customer issue-report drafts for the staff/admin Incidents workspace and persist operator draft decisions
- **Browser Endpoint:** `GET /api/operations/customer-reports`, `GET /api/operations/customer-reports?id=<reportId>`, `GET /api/operations/customer-reports?bookingId=<bookingId>`, `POST /api/operations/customer-reports/:draftId/decide`, `PATCH /api/operations/customer-reports/:draftId/convert-category`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role for reads and conversion, reads submitted `customer_complaint_drafts` through the service-role client, returns newest-first draft summaries, single detail payloads with service-role-minted attachment signed URLs, per-booking pending counts, and operator decision fields from the GH-497 persistence lane
- **Notification target:** urgent report escalation email and incident app notifications use the supported `/app/incidents` operator queue target. Do not emit unsupported incident/report detail query params until the app route consumes them.
- **Decision behavior:** `POST .../decide` supports `kind=promote` and `kind=dismiss`; promotion creates/returns a linked Incident idempotently, while dismiss is manager-tier/admin-only and requires a non-empty reason. `PATCH .../convert-category` updates only operator-side category/severity while the draft is undecided and never mutates customer wording.
- **Current operator UX:** `/app/incidents` is the staff/admin Incidents workspace. The Drafts tab preserves the submitted-report queue intent, exposes promote/dismiss/convert actions, and keeps customer wording inside the quoted customer body plus customer-urgency chip. Booking detail continues to show a compact pending customer-report count.
- **Notes:** customer-facing concierge flows must not expose operator-only Incident terminology; severity `critical` is operator-only and is never inherited from customer urgency.

### Operations Incidents Endpoints
- **Use:** Manage internal staff/admin incident records, related bookings/tasks/routes/customer reports, metadata-only attachments, and audit activity
- **Browser Endpoints:** `GET /api/operations/incidents`, `POST /api/operations/incidents`, `GET /api/operations/incidents/:incidentId`, `PATCH /api/operations/incidents/:incidentId`, `POST /api/operations/incidents/:incidentId/status`, `POST /api/operations/incidents/:incidentId/links`, `DELETE /api/operations/incidents/:incidentId/links/:linkId`, `POST /api/operations/incidents/:incidentId/attachments`, `DELETE /api/operations/incidents/:incidentId/attachments/:attachmentId`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `staff/admin` role, uses the service-role client for incident reads/writes, enforces the interim manager gate through `admin` for terminal resolve/dismiss actions and attachment removal, and returns structured `403` payloads for unauthorized staff actions
- **Activity contract:** `incident_activity.payload` is fixed per event type: `created` records category/severity/status/summary plus link and attachment counts; `status_changed` records from/to status plus optional reason/resolution/dismissal text; `assignee_changed` records prior/next assignee profile IDs; `summary_edited` records only a SHA-256 hash of the previous summary; attachment events record attachment metadata; link events record link kind and target
- **Notes:** attachments are metadata-only in this slice; storage buckets, signed URLs, notifications, and full cross-surface linked-incident drawer coverage remain separate follow-ups. Customer-facing concierge flows must not expose operator-only incident terminology.

### Operations Calendar Export Endpoint
- **Use:** Prepare calendar-ready export payloads for admin-created bookings and standalone client-linked ops tasks
- **Browser Endpoint:** `GET /api/operations/calendar-export?kind=booking|task&id=<uuid>&format=json|ics`
- **Auth:** `Authorization: Bearer <supabase access token>`
- **Server behavior:** validates token + requires `admin` role, loads the target booking/task plus linked client/property context, rejects missing targets with `404`, rejects not-calendar-ready targets with `409`, persists/update `calendar_export_records` only after the export payload is proven valid, and returns either:
  - `format=json`: normalized event payload (`title`, `status`, `startAt`, `endAt`, `allDay`, `timeZone`, `location`, `notes`, `client`, `property`) plus Google Calendar URL
  - `format=ics`: Apple/import-compatible `text/calendar` output with stable `event_uid`
- **Notes:** this slice intentionally stops at export-ready hooks; it does not add Google/Apple OAuth sync, token storage, webhook reconciliation, or external polling

### Customer Booking Calendar Export Endpoint (GH-715)
- **Use:** Let a signed-in client add **their own** booking to Google Calendar / download an `.ics`, from the customer booking-confirmation surface (`/app/bookings/$bookingId`). This is the customer-facing counterpart to the operator endpoint above; the customer surface does NOT use the operator-only `canViewBookingCalendarExport` gate.
- **Browser Endpoint:** `GET /api/bookings/$bookingId/calendar-export?format=json|ics`
- **Auth:** `Authorization: Bearer <supabase access token>` via `requireAuthenticatedUser` (any authenticated user — NOT operator/admin-scoped).
- **Ownership:** enforced in-handler like `bookings.$bookingId.change-request` — the booking's `profile_id` must equal the caller's user id; a mismatch (or missing booking) returns `404 "Booking not found."`. Unauthenticated → `401`. The endpoint never inspects `role`, so the operator/admin export path is unaffected.
- **Server behavior:** loads only customer-safe booking columns (id, profile_id, service/status, scheduled date + window, time zone, and address-only property fields), builds the event by reusing the shared `calendar-export` helpers with `audience: "customer"`, and **performs no DB write** (no `calendar_export_records` mutation). Not-calendar-ready (no scheduled date) → `409`. Returns either:
  - `format=json`: customer-safe subset — `links.googleCalendarUrl` + `event` (`title`, `startAt`, `endAt`, `allDay`, `timeZone`, `location`). No client contact, payment, or operations detail.
  - `format=ics`: `text/calendar` attachment with the same stable `event_uid` as the operator export (so customer/operator exports of one booking dedup in the calendar).
- **Customer-safe payload contract:** the `audience: "customer"` notes builder omits gate code, entry instructions, access notes, parking notes, and client email/phone, and drops the client name from the event title. These operator-only lines are never emitted to the customer export.
- **Rate limit:** `bookings_calendar_export` (default 60/window), keyed on the caller's user id.

## Address Intake + Property Enrichment

### Google Places Autocomplete (New)
- **Use:** Address typeahead and selection
- **Browser Endpoint:** `POST /api/places`
- **Browser Details Endpoint:** `GET /api/places/:placeId`
- **Upstream Endpoint (server-side only):** `POST https://places.googleapis.com/v1/places:autocomplete`
- **Notes:** Use session tokens and regional biasing for higher-quality suggestions. Restrict suggestions to our coverage area using `operations/service_areas.md` as the source of truth.
- **Security:** Google API key is stored as Worker secret (`GOOGLE_PLACES_API_KEY`) and never exposed client-side. The browser proxy requires an allowed browser origin, a valid short-lived signed request token (see _Public Proxy Signed-Request Verification_ below), and is protected by the `PUBLIC_PROXY_PLACES_RATE_LIMIT` Cloudflare Workers Rate Limiting binding, with the existing per-IP in-memory floor as a local fallback.
- **Decision:** Selected provider for address autocomplete

### RentCast Property Records
- **Use:** Property data lookup after address selection (beds/baths/sqft)
- **Browser Endpoint:** `GET /api/rentcast?address=...`
- **Upstream Endpoint (server-side only):** `GET https://api.rentcast.io/v1/properties`
- **Auth:** `X-Api-Key` header (server-side only)
- **Notes:** Search by full address string. Prefill is partial-by-design: only fields actually returned by RentCast are applied. If RentCast returns only `squareFootage`, only sqft is updated; beds/baths stay manual. Multi-family records can also be incomplete or ambiguous at the per-unit level.
- **Security:** RentCast API key is stored as Worker secret (`RENTCAST_API_KEY`) and never exposed client-side. The browser proxy requires an allowed browser origin, a valid short-lived signed request token (see _Public Proxy Signed-Request Verification_ below), and is protected by the `PUBLIC_PROXY_RENTCAST_RATE_LIMIT` Cloudflare Workers Rate Limiting binding, with the existing per-IP in-memory floor as a local fallback.

### Public Proxy Signed-Request Verification (GH-725)
- **Applies to:** `POST /api/places`, `GET /api/places/:placeId`, `GET /api/rentcast`.
- **Issuer:** `GET /api/proxy-credential` mints a short-lived HMAC-SHA256 token server-side (secret `PUBLIC_PROXY_SIGNING_SECRET`, never exposed client-side). The issuer is itself browser-origin-gated and rate-limited (`proxy_credential`, default 120/window).
- **Token:** signs route-uniform claims `{ aud: "amiga-public-proxy", iat, jti }` only — no request body, query, or path is signed, so GET and POST canonicalize identically and one token is valid for all three proxy routes within its lifetime. TTL is 600 seconds.
- **Transport:** the app shell replays the token on every proxy call via the `X-Amiga-Proxy-Token` request header (added to the CORS preflight `Access-Control-Allow-Headers`).
- **Gate order on each proxy route:** allowed browser origin → valid signed token → rate limit. A missing, tampered, expired, or wrong-secret token returns `401 { code: "proxy_signature_invalid" }`; the client transparently re-mints once and retries.
- **Threat model (honest scope):** this is a replay-window / server-mediated-freshness control. It collapses replay of a captured proxy request past its TTL and forces calls through a server-side mint step. It is **not** anti-automation: a determined mint-then-call script is still bounded only by the browser-origin gate and rate limiting. Bot-challenge defenses (e.g. Cloudflare Turnstile / proof-of-work) are a separate follow-up.
- **Replay stance:** stateless; no nonce/single-use store (no DB). In-TTL replay is bounded by the short TTL plus the existing rate limiter.
- **Bot impact:** `amiga-concierge` directly (public quote/address lookup feeds concierge/customer quote guidance); `amiga-operator-cmo` indirectly (admin intake uses the same proxy routes). Neither bot needs to handle the token — it is minted and replayed by the app shell.

### Cloudflare WAF / Turnstile Anti-Automation Decision (GH-726)
- **Decision:** staged WAF-first posture. Do **not** add Turnstile to the public quote flow immediately without abuse data.
- **Runbook:** `cloudflare_waf_runbook.md` is the GH-760 execution reference for expressions, thresholds, monitoring evidence, account permissions, and rollback.
- **Phase 1:** create a non-enforcing Cloudflare baseline for `POST /api/places`, `GET /api/places/:placeId`, `GET /api/rentcast`, and `GET /api/proxy-credential`. Including `/api/proxy-credential` is required because it is the mint choke point for mint-then-call automation.
- **Plan-tier caveat:** pure WAF/rate-limit `log` actions are Enterprise-dependent. On Free/Pro/Business, Phase 1 must use Security Analytics/Security Events saved filters and optional Logpush if available instead of Managed Challenge, because Managed Challenge is customer-visible enforcement.
- **Initial posture:** no app code and no customer UX impact. Collect WAF event/Security Analytics/Logpush evidence by IP, ASN, bot-score where licensed, path, method, user-agent, and action outcome; tune against real customer quote/address lookup and admin-intake traffic before any enforcement.
- **Phase 2:** promote to Managed Challenge only if Phase-1 evidence shows material distributed/headless automation that the app limiter misses and false positives are understood.
- **Phase 3:** defer Turnstile until Phase-1/2 data proves it is necessary. If implemented, gate only the `/api/proxy-credential` mint, never each Places/RentCast request or per-keystroke autocomplete. The server must validate Turnstile tokens with Siteverify, use Cloudflare test keys in dev/test, store the real secret server-side, and fail open with the existing limiter floor if Siteverify is unavailable so the quote flow does not break.
- **Admin-intake caveat:** admin intake uses the same public proxy routes, and WAF cannot see the app's signed token. Log-only mode and tuning are mandatory before enforcement; known operator egress allowlisting should be considered if available.
- **Bot impact:** `amiga-concierge` directly because the public quote/address lookup path feeds customer guidance; `amiga-operator-cmo` indirectly because admin intake shares the same proxies.

### Google Address Validation
- **Use:** Detect likely missing unit/subpremise data on US addresses after address selection
- **Browser Endpoint:** `POST /api/address-validation`
- **Upstream Endpoint (server-side only):** `POST https://addressvalidation.googleapis.com/v1:validateAddress`
- **Notes:** Used after Google Places selection or typed-address blur to detect likely missing apartment/unit numbers before quote/admin-intake can continue. Address Validation does not invent the unit number; it flags that the address likely needs one. This is the authoritative missing-unit signal; raw Google Places `premise` / `subpremise` types are not treated as sufficient on their own because Places can label standalone houses and partial records too broadly.
- **Enablement:** The Google Cloud project behind the server key must have `addressvalidation.googleapis.com` enabled or the endpoint will return `403 PERMISSION_DENIED`.
- **Security:** Address Validation uses a server-side Google key (`GOOGLE_ADDRESS_VALIDATION_API_KEY`, falling back to `GOOGLE_PLACES_API_KEY`) and never exposes the credential client-side.

### Mapbox Geocoding (Alternative to Google Places)
- **Use:** Address autocomplete + forward geocoding
- **Endpoint:** `GET https://api.mapbox.com/search/geocode/v6/forward?q={search_text}`
- **Notes:** `autocomplete=true` by default; requires `access_token`

---

## Payments

### Stripe
- **Use:** Primary payments, deposits, cards on file
- **Payment Intents:** Create/confirm payments
- **Setup Intents:** Save payment methods for future charges
- **Current integration:** inline Stripe Elements flow for the public booking flow and customer portal retries (`SetupIntent` in browser, manual-capture `PaymentIntent` on the server after setup succeeds, with setup-intent ownership binding and same-process duplicate-submit serialization on the booking create path)
- **Compatibility:** hosted Checkout confirmation remains supported for in-flight legacy sessions
- **Current integration:** server-side webhook processing for payment state reconciliation (`/api/stripe/webhook`)
- **Server env:** `STRIPE_SECRET_KEY` (required), `STRIPE_WEBHOOK_SECRET` (required for webhooks), `STRIPE_API_VERSION` (optional; default `2024-06-20`)

### Square (Alternative)
- **Use:** Payments if Square is preferred
- **Payments API:** `POST /v2/payments` (requires payment token from Square Web Payments SDK)

---

## Messaging (SMS)

### Twilio Programmable Messaging
- **Use:** Quote confirmations, reminders, reschedules, status updates
- **Messages API:** `POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json`

---

## Email

### AWS SES (SMTP)
- **Use:** Transactional emails (quote summary, booking confirmation, receipts)
- **Booking confirmation links:** customer booking-confirmation emails use `/app/bookings?bookingId=<bookingId>` so the client bookings list can focus the specific booking card.
- **Notes:** SMTP credentials are region-specific

### MXroute (SMTP)
- **Use:** Transactional emails from custom domains (if using existing MXroute)
- **Notes:** SMTP server is the account’s server name; ports 465/587/25/2525

---

## Scheduling

### Calendly Scheduling API
- **Use:** Create bookings on behalf of customers from our UI
- **Key Endpoints:**
  - `GET /event_types`
  - `GET /event_type_available_times`
  - `POST /invitees`
- **Base URL:** `https://api.calendly.com`
- **Auth:** Personal Access Token (PAT) or OAuth

### Cal.com API (Alternative)
- **Use:** Programmatic booking from our UI
- **Endpoint:** `POST /v2/bookings`
- **Auth:** `Authorization: Bearer <token>` + `cal-api-version` header

### Scheduler UI Layer (Implemented Baseline)
- **Primary choice:** Syncfusion Scheduler (React)
- **Use:** Staff/admin scheduling UI for route runs and task calendars
- **Current state:** `/app/routes` renders `TimelineDay`, `TimelineWeek`, and `Agenda` views with live data + map split synchronization
- **Current state:** event cards include status-based styling and compact stop/time context
- **Current state:** recurrence series edits and single-occurrence overrides are enabled through scheduler edit flow
- **Current state:** route optimization trigger is available in `/app/routes` and persists optimized stop order + ETA/travel fields
- **Current limitation:** recurrence writes require recurrence DB migration to be applied in the target environment
- **Customization plan:** continue with advanced templates and deeper optimization tuning/testing
- **Task spec:** `booking_quote_system/scheduler_implementation_plan.md`

---

## Travel Time + Routing

### Google Routes API
- **Use:** Estimate travel time between jobs for scheduling windows
- **Endpoint:** `POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix`
- **Notes:** Returns distance/duration matrix for multiple origins/destinations

---

## Admin Data Maintenance (Internal)

### Generated-Data Removal (GH-626 preview / GH-729 execute / GH-730 admin UI)
- **Preview:** `POST /api/operations/generated-data/preview` — admin-only, non-destructive dry-run; per-category identity + per-table counts in FK-safe order.
- **Execute:** `POST /api/operations/generated-data/execute` — admin-only destructive removal of generated/test data. Requires `confirm:true` + `acknowledgement:"REMOVE GENERATED DATA"` + echoed `expectedDeletionCounts`/`expectedIdentityCount` (server recomputes fresh, `409` on drift). Optional `identityIds` allowlist; `dryRun:true` audits without deleting.
- **Safety:** ownership-only selection (actor/creator links like `tasks.created_by` never delete real-owned rows), FK-safe order with `auth.users` removed last via compensated `deleteUser`, hard exclusion of `@amigaclean.com`/non-`.test`. Every call audited to `admin_data_maintenance_events` (see `db_notes.md`).
- **Admin UI (GH-730):** `/app/settings/generated-data` (admin **Settings** sidebar group) is the operator surface over both endpoints — category selection, preview/deletion-plan rendering, preserved-table badges, and a typed-acknowledgement + count-echo confirm dialog that handles `409` drift / `403` / `401` without dead ends. Consumes the endpoints as-is; no deletion semantics live in the UI. App runbook: `docs/local-validation-personas.md` → "Generated-Data Maintenance UI (GH-730)".
- **Bot impact:** `amiga-operator-cmo` only. Operator sign-off required before destructive live validation and before merge.
