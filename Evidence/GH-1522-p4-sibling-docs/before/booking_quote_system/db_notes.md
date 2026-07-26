# DB Notes (Supabase)

## Goals
- Fast, low-friction quote → booking flow
- Realtime updates for dashboards (avoid page reloads)
- Avoid redundant API calls by caching property data and quote breakdowns

## Realtime Strategy
Enable Supabase Realtime on:
- `quotes`
- `bookings`
- `app_notifications`
- `notifications`
- `pricing_settings`
- `services`
- `addons`
- `tasks`
- `task_checklist_items`
- `route_runs`
- `route_run_stops`

Status (2026-03-04):
- Enabled on Amiga project `wbqjeasgxakubqcutgjt` for all tables above.

This allows UI to update when:
- A quote is saved or updated
- Booking status changes
- An in-app notification is inserted or its read state changes
- Reminders are sent
- Pricing rules or add-ons are updated

## Caching & Redundant Calls
- Store RentCast results in `properties` to avoid repeated lookups
- Store full `price_breakdown` JSON in `quotes` to avoid recomputation
- Store add-on selections in `quote_addons`
- `properties.duration_adjustment_minutes` and `properties.duration_notes` now hold operator-managed service-time overrides for specific homes; these are operational estimate modifiers, not a replacement for the canonical saved service-plan baseline.

## Retention Policy
- Saved quotes expire after **30 days**
- Enforce via scheduled job that marks expired quotes and deletes or archives

**Suggested SQL (pg_cron):**
`select cron.schedule('expire_quotes_daily', '0 3 * * *', $$select public.expire_quotes();$$);`

Status (2026-03-04):
- `pg_cron` extension enabled.
- Job `expire_quotes_daily` created with schedule `0 3 * * *`.

## Security Notes
- RLS policies restrict data to the authenticated user
- Admin access is enabled via `profiles.role = 'admin'` and `public.is_admin()`
- Staff access is granted via `staff_assignments` or when a staff member is assigned to a task tied to a client (`tasks.client_id`)
- RLS helper hardening applied for `is_admin`, `is_staff`, `has_staff_access`, `is_assigned_to_task`, and `is_task_client`: each helper is `SECURITY DEFINER STABLE`, owned by `postgres`, pins `search_path = public, pg_temp`, and revokes `PUBLIC`. Interim exception: `anon` keeps explicit EXECUTE until dependent policies are role-scoped away from `PUBLIC`, so anonymous policy evaluation returns false instead of raising permission-denied errors.
- Function `search_path` hardening applied for: `set_updated_at`, `is_admin`, `is_staff`, `expire_quotes`, `is_assigned_to_task`, `is_task_client`, `has_staff_access`
- RLS enabled on `services`, `addons`, and `pricing_settings` with public-read + admin-write policies
- RLS enabled on `staff_profiles` as of 2026-04-23 with admin full access and self read/update for the matching `profile_id`
- `app_notifications` now uses recipient-scoped RLS:
  - authenticated users can `SELECT` only rows where `recipient_id = auth.uid()`
  - authenticated users can update only the `read_at` column on their own rows
  - inserts and deletes stay server-side/service-role only

Auth security follow-up:
- Supabase advisor still reports `Leaked Password Protection Disabled`.
- Attempted enabling via Supabase Management API on 2026-03-04; blocked by plan requirement (`HTTP 402`, Pro+ required).
- Free-plan compensating controls applied on 2026-03-04:
  - `password_min_length = 10`
  - `security_update_password_require_reauthentication = true`

## Roles
- **anonymous:** no profile row; quote draft stored only in browser local storage (client-side)
- **user:** saved-quote account (Google OAuth + email/password)
- **client:** booked customer (full profile)
- **staff:** assigned to specific clients via `staff_assignments`
- **admin:** staff access (full read/write)

## Tasks
- `tasks` capture operational work items (cleaning, follow-up, admin)
- `task_assignments` supports multi-user assignment (staff + admins)
- Set `tasks.client_id` when a task is tied to a customer so staff can access related client data via RLS
- `task_checklist_templates` define service checklists; `task_checklist_items` track completion
- `task_addons` stores snapshot pricing for add-ons at task time
- `task_client_comments` are client-visible notes tied to a task
- `task_change_requests` store client edits that require review before schedule changes
- `task_change_requests.request_no` is the sequence-backed human request number used in operator UI as `CR-<n>`; the database stores the bare bigint and the app applies the display prefix.
- `bookings.booking_no` is the root-row sequence-backed human booking number used in operator UI as `B-<n>`; the database stores the bare bigint and the app applies the display prefix.
- `bookings.series_booking_no` is the stable series-facing booking number. One-time bookings and recurring parent bookings set it to their own `booking_no`; generated recurring child visits inherit the parent/root `series_booking_no` and keep `booking_no = null` so future visits do not consume or display new human booking numbers.
- Recurring visits still use dated child `bookings` rows for tasks, route stops, payments, holds, and per-visit audit. A full one-booking-many-occurrences table rewrite remains out of scope until explicitly designed.
- recurring booking change requests now extend that same table instead of introducing a second booking-only queue:
  - `task_change_request_type` also includes `cadence_change`, `skip_visit`, and `end_series`
  - `task_change_requests.booking_id` links booking-scoped requests directly to the recurring booking series
  - `skip_visit` requires both `booking_id` and the linked visit `task_id`; `cadence_change` and `end_series` are booking-scoped without forcing a task row
  - operator decisions stay review-only in this lane: `cadence_change` and `end_series` do not auto-mutate the schedule, and `skip_visit` only uses the existing linked-task skip/cancel path when the visit task already exists
  - customer-facing `/app/bookings/$bookingId` now reads pending booking-scoped requests from this table so duplicate cadence/skip/end-series submissions can be blocked without relying on free-text `booking_events`

## In-App Notifications Runtime
- `public.app_notifications` is the operator-facing in-app notifications table and is distinct from `public.notifications`, which remains the delivery-channel/reminder log.
- `public.client_messages` is the canonical operator-facing message context table for dashboard/inbox-style previews. Current columns:
  - `client_profile_id`
  - optional `booking_id` / `task_id`
  - `thread_reference`
  - `channel` enum: `sms | email`
  - `message_subject`
  - `message_preview`
  - `message_body`
  - `context_deep_link`
  - `reply_deep_link`
  - `created_at`
  - `read_at`
- current linkage rule for dashboard message previews:
  - `app_notifications.entity_kind = 'client_message'`
  - `app_notifications.entity_id = client_messages.id`
  - the dashboard MessagesCard joins notification unread state from `app_notifications` with identity/channel/preview data from `client_messages`
- Current enum surfaces:
  - `app_notification_type`: `dispatch_exception`, `change_request`, `client_message`, `task_overdue`, `booking_updated`, `staff_update`, `incident`
  - `app_notification_severity`: `info`, `attention`, `urgent`
  - `app_notification_entity_kind`: `booking`, `task`, `route_run`, `dispatch_exception`, `change_request`, `client_message`, `staff_profile`, `incident`
  - `app_notification_source`: `bookings`, `tasks`, `dispatch`, `clients`, `staff`, `messages`, `routes`, `incidents`
- Runtime API contract:
  - `GET /api/app-notifications`
  - `PATCH /api/app-notifications/:id/read`
  - `POST /api/app-notifications/mark-all-read`
- Runtime/UI contract:
  - one shared client store powers the bell badge, tray, `/app/dashboard/notifications`, and the staff read-only `/app/updates` history surface
  - one Supabase Realtime subscription per signed-in admin/staff session listens on `app_notifications` filtered by `recipient_id`
  - account/client roles do not mount the bell and are redirected away from notification surfaces
  - `/app/updates` is staff-only and reuses `GET /api/app-notifications?window=30d&read_state=all`, `PATCH /api/app-notifications/:id/read`, and `POST /api/app-notifications/mark-all-read`; it adds no tables, producers, compose/reply/threading, or Inbox semantics
  - the dashboard `MessagesCard` now reads recent message rows from `client_messages` while preserving unread state from the linked `app_notifications` row
- Deep-link producer contract:
  - staff task lifecycle notifications target `/app/my-day?taskId=<taskId>`
  - operator booking/change-request notifications target `/app/operations?view=bookings&bookingId=<bookingId>` when a booking is known
  - dispatch exception notifications and dashboard activity target `/app/operations?view=dispatch&selectedRunId=<routeRunId>` with optional exception context
  - task dashboard activity targets `/app/operations?view=tasks&taskId=<taskId>`
  - incident/report notifications remain at `/app/incidents` until an incident/report detail route consumes a specific search parameter

## Customer Complaint Intake
- `customer_complaint_drafts` stores customer-submitted issue reports from the standalone `/app/report-issue` route and post-service `/app/bookings/:bookingId/report-issue` route.
- Rows are pending operator-review drafts until an operator explicitly promotes or closes them in the operator-only workflow; customer submission does not auto-create an operator Incident.
- The retry-safety key is `(submitter_kind, client_token)`. After submission, repeated save or submit requests return the existing row without changing content or re-sending acknowledgement email or SMS.
- Customer-readable responses expose only the customer-safe draft/receipt shape. Internal fields such as `severity_hint`, `inferred_category`, `pending_operator_review`, `ip_hash`, `user_agent`, and raw storage paths stay server/operator-only.
- Attachments are stored in the private `customer-complaint-attachments` bucket. Customer routes upload through the runtime API; operator surfaces must receive only server-minted signed URLs.
- Runtime operator visibility is read-only in this slice: `/app/incidents` and `GET /api/operations/customer-reports` show submitted drafts, detail payloads, attachment signed URLs, and per-booking pending counts for staff/admin without creating an Incident or mutating operator decision fields.
- Shared project `wbqjeasgxakubqcutgjt` has the GH-452 RLS-helper hardening applied, so authenticated admin/staff/customer PostgREST reads no longer hit the prior `54001` recursion in the `is_admin()` / `profiles_select_own` helper chain.
- Auth signup bootstrap (TASK-AE140F / #1251): `auth.users` has the `on_auth_user_created` trigger calling `public.handle_new_user()` to create a lightweight `public.profiles` row (`role = 'user'`, `profile_stage = 'lightweight'`) for normal public signups only. Operator/admin/service-role provisioning must set `raw_app_meta_data.skip_lightweight_profile = true` or `provisioning_source in ('operator', 'admin_api', 'service_role')`; the trigger checks for an existing profile and never updates or clobbers it. Quote-modal save-to-account replay is independent from booking: `BOOK` never writes pending quote-save storage, while `SAVE` uses same-browser pending save records plus `public.quotes.client_pending_id` / `quote_hash` idempotency to avoid duplicate saved quotes after email confirmation, OAuth redirects, token refresh, or multi-tab replay.
- SMS acknowledgement channel (GH-455): `customer_complaint_drafts` now stores inline `ack_sms_status`, `ack_sms_error`, `ack_sms_sent_at`, `ack_sms_recipient`, and `ack_sms_attempts` fields alongside the email acknowledgement fields. Public reports with phone contact values attempt one transactional SMS acknowledgement; public reports with email-only contact values record SMS as `skipped`; token reports can receive SMS in addition to email when the submitter profile has a phone. SMS provider failures are non-blocking and record `failed`.
- SMS opt-out enforcement lives in `public.sms_opt_outs` (`phone_e164`, `opted_out_at`, `source`) with RLS enabled and a single admin-only `FOR ALL` policy. Runtime acknowledgement checks the table before every send. Inbound STOP webhook ingestion is not implemented yet; until that follow-up ships, rows are admin/manual seeded. Customer-facing SMS copy must include `Reply STOP to opt out`.

## Scheduling & Routing
- `route_runs` represent daily staff routes
- `teams` and `team_members` represent explicit crew membership. `team_members.role` is `member` or `lead`; a partial unique index allows at most one lead per team.
- `route_runs.team_id` is nullable. Solo runs keep `team_id = null` and continue to use `route_runs.staff_profile_id`; crew runs may set `team_id`, while `staff_profile_id` remains the on-road lead/driver.
- Dashboard and operations route team-code projections read explicit membership first and fall back to the legacy `staff_profiles.zone_ids` mapping during rollout.
- `route_runs.recurrence_rule` and `route_runs.recurrence_exception` store recurring scheduler rules/exclusions
- `route_runs.recurrence_parent_run_id` and `route_runs.recurrence_instance_date` support persisted single-occurrence recurrence overrides
- `route_run_stops` store ordered tasks, ETAs, travel minutes, distance estimates, and manual overrides
- Route optimization workflow updates `route_run_stops` sequencing/timing fields and refreshes route-run totals

## Dashboard And Operations Weather Forecast Events
- Dashboard and Operations Bookings per-stop weather use the existing `public.booking_events` audit table; no separate forecast table is required.
- Runtime writes provider-derived rows with `event_type = 'weather_forecast'` for same-day bookings whose property coordinates or address can resolve through NWS/weather.gov.
- Required payload fields: `provider = 'nws'`, `source = 'api.weather.gov'`, `weatherForecastVersion`, `serviceDate`, `fetchedAt`, `expiresAt`, provider period metadata, coordinates/source, and `forecast` (`iconKey`, `temp`, `condition`, `impactsOutdoor`).
- Freshness rule: provider forecast rows only render for the matching booking `scheduled_date` while `expiresAt` is still in the future. Expired provider rows are ignored and may be refreshed non-blockingly.
- Outdoor-impacting forecasts (`forecast.impactsOutdoor = true` or legacy weather-risk payloads) feed the dashboard Needs attention `weather_risk` category for today only.
- Operations Bookings rows and the drawer Overview are stricter consumers: they render only real NWS provider rows that are still fresh, exclude `seededFixture` payloads, and render no chip/ring when no qualifying forecast exists.
- Provider/geocoding/write failures are non-blocking: the dashboard continues to load and affected rows render with `weatherForecast = null`.
- Property-level duration adjustment support (2026-04-20):
  - `properties.duration_adjustment_minutes integer default 0`
  - `properties.duration_notes text`
  - booking operational estimates apply the per-property minute adjustment after baseline estimate derivation so downstream availability and staffing logic see the adjusted elapsed time
- Booking reminder automation baseline (2026-03-04):
  - DB function: `public.upsert_booking_reminders(target_booking_id uuid)`
  - Schedules `booking_reminder_24h` and `booking_reminder_2h` email rows in `notifications`
  - Uses booking local schedule (`scheduled_date`, `window_start`, `time_zone`) to compute absolute `scheduled_at`
  - Idempotent by booking/channel/template unique index: `notifications_booking_channel_template_unique_idx`
- Customer SMS reminder mirror (GH-862):
  - After email reminder scheduling, opted-in customer bookings mirror the same `booking_reminder_24h` and `booking_reminder_2h` rows as `channel = 'sms'`, `recipient_kind = 'customer'`, and `recipient_profile_id = bookings.profile_id`.
  - SMS mirroring is best-effort and never blocks booking reminder scheduling.
  - Dispatch re-checks effective transactional SMS consent and `sms_opt_outs` at send time. Revoked consent, missing phone, or opt-out suppresses the SMS row; email reminders remain separate and unchanged.
  - No schema migration is required beyond the existing recipient-addressed notification columns from GH-827a.
- Staff assignment email baseline (GH-1044/GH-1045):
  - `staff_assignment_published` rows are now live as both staff bell updates and `notifications.channel = 'email'` rows addressed by `recipient_kind = 'staff'` and `recipient_profile_id`.
  - Dispatch resolves the staff profile from `notifications.recipient_profile_id` for staff email rows instead of using the booking customer profile.
  - Staff assignment bell/email deep links target `/app/my-day?taskId=<taskId>` so notification clicks can open the assigned task drawer on My Day.
  - `staff_schedule_changed` remains app-only; staff SMS and run-level route-summary digests remain separate follow-ups.
- Booking mutation customer notification baseline (2026-05-25):
  - Staff/admin cancellation enqueues a `booking_cancelled` email notification after the `booking_cancelled` audit event is recorded.
  - Staff/admin reschedule enqueues a `booking_rescheduled` email notification after the `booking_rescheduled` audit event is recorded.
  - Both use the existing `notifications_booking_channel_template_unique_idx` idempotency key and do not require a schema migration.
- Change-request acknowledgment email baseline (GH-854):
  - On the first operator decision for a pending change request, the app records `booking.change_request_acknowledged` with `payload.requestId` and a structured summary, then enqueues `change_request_acknowledged:<change_request_id>` as a customer email notification.
  - The instance-keyed `template_key` reuses the existing notification uniqueness model and allows multiple change requests on one booking to each receive their own acknowledgment without a schema migration.
  - The acknowledgment enqueue is best-effort and must not fail the operator decision.
- Notification dispatch claim recovery (GH-831):
  - `notifications.claimed_at timestamptz` is nullable and additive.
  - Dispatch claims set `status = 'sending'` and stamp `claimed_at` through the service-role-only `claim_notification_for_dispatch` RPC, not a PostgREST PATCH `.or()` update.
  - The due-notification scanner can reclaim `sending` rows when `claimed_at` is older than the configured stale-claim window, or when `claimed_at` is NULL from the pre-GH-831 claim path.
  - Reclaim is at-least-once delivery: a rare duplicate is accepted over permanently stranded customer communication; provider-level idempotency remains a separate follow-up.
  - Release and sent paths clear `claimed_at` so non-stranded rows do not look stale.
  - Migrations: `db/migrations/20260607_add_notifications_claimed_at.sql`, `db/migrations/20260610_add_claim_notification_for_dispatch_rpc.sql` (+ `db/schema.sql`).

## Staffing Availability
- `staff_profiles` remains the weekly/default staffing surface for employment status, bio/facts, zone coverage, preferred weekday schedule, and workforce classification.
- `staff_profiles.workforce_classification` is now the operational source-of-truth for scheduling eligibility:
  - `core` counts toward live scheduling availability and default assignment paths
  - `guest` is excluded from the default auto-count path
  - `test_local` is reserved for local validation personas so they never influence live availability
- task-assignment mutations must validate assignees against that same `staff_profiles` core classification instead of `profiles.role = 'staff'`, so auth role and operational eligibility do not drift apart.
- `staff_unavailability` is the date-level override surface for staffing feasibility.
- Current operator behavior:
  - booking feasibility checks count recommended crew separately from feasible in-zone crew
  - `staff_unavailability` dates remove a staff member from feasible-crew calculations even when their weekly schedule is enabled
  - degraded staffing is operator-only until approval; the customer-side elapsed-time contract stays on the recommended crew

## Payment Authorization
- Booking records persist payment lifecycle fields:
  - `payment_status` (`unpaid`, `authorized`, `captured`, `failed`, ...)
  - `payment_provider`
  - `payment_reference`
  - `deposit_required` / `deposit_amount`
- Deposit baseline:
  - Deep and move-in/out bookings store `deposit_amount` as 50% of quote total at booking creation.
- Stripe authorization confirmation updates `bookings` and appends `booking_events` (`payment_authorization_started`, `payment_authorized`, `payment_captured`, `payment_failed`).
- Operational payment actions support:
  - capture authorized deposits (`payment_status -> captured`)
  - void authorized deposits (`payment_status -> void`)
  - booking events: dotted runtime event types `payment.captured`, `payment.voided`, `payment.refunded`, and `payment.capture_failed`; legacy underscore rows remain readable.
- Payment audit event payloads live in `booking_events.payload`:
  - capture: captured/authorized/capturable amounts, reason code, note, task id, app operation idempotency key, provider idempotency key, actor profile/role, and processor reference/status.
  - capture idempotency: explicit `Idempotency-Key` header/body values are forwarded to Stripe unchanged; implicit task/booking fallback attempts keep the app operation replay key stable but use a fresh provider idempotency key so Retry capture is not pinned to a cached Stripe failure.
  - capture failure: attempted amount, reason code, processor message, task id, idempotency key, actor profile/role, and attempted timestamp.
  - void: voided timestamp, void reason, actor profile/role, and processor reference/status.
  - refund: refunded timestamp, amount, reason, actor profile/role when refund runtime lands.
- Customer-facing booking loaders expose only payment status plus captured/refunded amount/date fields; provider, reference, failure, void/refund reason, actor, note, and idempotency details are operator-only.

## Booking Creation Transaction Boundary
- Customer booking creation now uses a narrowed database transaction boundary for the critical parent core.
- The app resolves Google place coordinates in TypeScript before persistence, then calls the service-role-only `public.create_booking_atomic` RPC.
- `create_booking_atomic` is `SECURITY DEFINER`, pins `search_path`, and is revoked from `public`, `anon`, and `authenticated`; only `service_role` can execute it.
- Website self-serve bookings create the canonical `client_accounts` row at the same atomic boundary as the customer profile upsert. Existing operator-created client accounts are preserved with `ON CONFLICT DO NOTHING`; a backfill on 2026-06-30 repaired booked client profiles that were missing this row.
- `bookings.intake_channel` is nullable text with a CHECK allowing `website`, `phone`, `concierge`, and `referral`. There is no default and no backfill: website self-serve creation persists `website`, operator/concierge on-behalf creation persists `concierge`, and legacy `NULL` rows keep the Inbox fallback (`customer.phone` -> `phone`, otherwise `website`). `referral` is reserved until a referral intake producer exists.
- The RPC owns the atomic write set for:
  - `profiles`
  - `properties`
  - `quotes`
  - `quote_addons`
  - parent `bookings`
  - initial `booking_created` event
- A partial unique index on `bookings(payment_reference)` for `setup_intent:%` provisional references prevents duplicate parent bookings for concurrent create attempts using the same Stripe SetupIntent.
- Work intentionally remains post-commit with existing compensation and audit behavior:
  - Stripe manual-capture authorization
  - walkthrough file upload/removal
  - capacity holds
  - operational task and route scheduling
  - recurrence child bookings/tasks
  - reminders and confirmation notification enqueueing
- The parent app path skips a second `booking_created` event after the RPC succeeds and records only the downstream operational/payment/reminder events in the post-commit phase.

## Booking Archive Semantics
- Cancelled and completed bookings can now be archived without being deleted.
- Archive state lives on `bookings`:
  - `is_archived`
  - `archived_at`
  - `archived_by_profile_id`
  - `archive_reason`
- Archive is intentionally audit-safe:
  - booking row remains in place
  - `booking_events` remain intact
  - direct booking-detail access remains valid
- Archive eligibility is server-enforced:
  - booking must already be `cancelled` or `completed`
  - linked tasks must already be `done` or `cancelled`
  - route-stop remnants must already be cleared
- Default list/history reads exclude archived closed bookings unless the caller explicitly opts into archive/history access.

## Comment Cutoff (App-Level)
- Comments inside the 24‑hour window are allowed but not guaranteed to be actioned
- Service edits inside the 24‑hour window should be logged in `task_change_requests` and require staff/admin approval

## Local Dev Notes
- Orbstack/Postgres does not include Supabase `auth` schema by default; `db_schema.sql` creates a minimal `auth.users` table and `auth.uid()` for local testing.

## Remote Supabase Bootstrap
- Production quote/booking persistence requires the full schema in `../amiga/db/schema.sql`.
- If `/rest/v1` OpenAPI shows only `"/"` (no `/profiles`, `/quotes`, etc.), schema is not deployed yet.
- Expected core tables for quote/book flow:
  - `profiles`
  - `properties`
  - `quotes`
  - `quote_addons`
  - `bookings`
  - `booking_events`

Additional pre-phase2 hardening applied (2026-03-04):
- FK coverage indexes added for advisor-flagged keys (missing FK index check = `0`).
- Private storage bucket `booking-walkthroughs` pre-created with:
  - `file_size_limit = 52428800` (50MB)
  - `allowed_mime_types = image/jpeg, image/png, image/webp, video/mp4, video/quicktime`

### Troubleshooting: "Failed to upsert profile"
- Most common cause: missing `public.profiles` table in the target Supabase project.
- Apply `db/schema.sql` to the target project first, then retry quote save.

## Workflow Rule For DB-Impacting Lanes

Use the app-repo runbook:

- `../amiga/docs/supabase-db-workflow.md`

DB work is not only migrations. Treat all of the following as DB-impacting:

- schema changes in `db/migrations` or `db/schema.sql`
- runtime/server code that depends on new columns, JSONB settings, policies, or functions
- auth/RLS role changes
- shared-data seeding or repair work

Required rule for shared Amiga DB work:

1. author the migration in git
2. align `db/schema.sql`
3. apply to shared project `wbqjeasgxakubqcutgjt` when the lane reaches integration/apply stage
4. assert the resulting schema state explicitly
5. only then trust PM2/browser runtime validation against that route

Recommended tool split:

- Supabase CLI for local authoring and remote inspection support
- Amiga Supabase MCP for the authoritative shared-project apply/assert/advisor steps

Do not treat a checked-in migration file as equivalent to a live shared-project update.

## Known Limits / Edge Cases
- RentCast lookup can fail; manual entry required
- Address may change after save; update both `quotes` and `properties`
- If a user books without saving, the parent core must go through `create_booking_atomic`; do not reintroduce separate app-side inserts for profile/property/quote/parent booking creation.

## Admin Data-Maintenance Audit (GH-729)

`public.admin_data_maintenance_events` is the durable audit trail for destructive
generated-data removal. One row per execute and per confirm-dry-run call of
`POST /api/operations/generated-data/execute`.

- Columns: `actor_user_id`, `actor_email`, `ran_at`, `mode` (`dry_run`|`execute`),
  `selected_categories` (text[]), `identity_scoped`, `identity_ids` (uuid[]),
  `identity_count`, `preview_counts` (jsonb), `deletion_counts` (jsonb),
  `auth_users_deleted`, `skipped` (jsonb), `request_digest`.
- Written only by the service-role client; admin-only RLS select (`public.is_admin()`),
  no authenticated insert/update/delete grant.
- `actor_user_id` is a plain uuid (no FK) so the audit survives the identity churn it records.
- Migration: `db/migrations/20260602_add_admin_data_maintenance_events.sql` (+ `db/schema.sql`).
  DDL apply to shared `wbqjeasgxakubqcutgjt` is Codex/operator-owned; destructive live
  validation and merge each require operator sign-off.

## GH-825 — Terms/Privacy clickwrap acceptance (bookings)

- Additive columns on `public.bookings` (nullable, no backfill — legacy rows = NULL = pre-clickwrap):
  - `terms_accepted_at timestamptz` — server-stamped at booking create.
  - `terms_version text` — the `TERMS_VERSION` (see `src/lib/terms.ts`) the customer accepted.
- Also an immutable `terms_accepted` `booking_events` row (`{version, acceptedAt}`), zero-DDL audit.
- Migration: `db/migrations/20260606_add_bookings_terms_acceptance.sql` (+ `db/schema.sql`).
  Shared `wbqjeasgxakubqcutgjt` DDL apply is Codex/operator-owned.

## GH-833 (826a) — TCPA SMS opt-in consent records

- Additive table `public.sms_consents` records optional booking-flow SMS opt-ins:
  - `profile_id` references `profiles(id)` with cascade delete.
  - `phone_e164` is required and constrained to E.164.
  - `consented_at` and `created_at` are server-stamped.
  - `consent_version` stores `SMS_CONSENT_VERSION` from `src/lib/sms-consent.ts`.
  - `source` is constrained to `booking_flow` or `account`; GH-833 writes `booking_flow` only.
  - `disclosure_text` stores the exact TCPA/CTIA disclosure shown to the customer as consent proof.
- RLS is enabled with a single admin-only `FOR ALL` policy mirroring `sms_opt_outs`; runtime writes go through the service-role booking path.
- `sms_opt_outs` still wins at send time. GH-827 lifecycle SMS must require both a matching opt-in and no active opt-out before delivery.
- Migration: `db/migrations/20260606_add_sms_consents.sql` (+ `db/schema.sql`).
  Shared `wbqjeasgxakubqcutgjt` DDL apply is Codex/operator-owned.

## GH-838 (826c) — Marketing communication preferences

- Additive table `public.communication_preferences` stores one default-OFF marketing preference row per profile:
  - `profile_id uuid primary key` references `profiles(id)` with cascade delete.
  - `marketing_email_opt_in boolean not null default false`.
  - `marketing_sms_opt_in boolean not null default false`.
  - `marketing_prefs_updated_at timestamptz not null default now()`.
- Additive append-only table `public.marketing_sms_consents` stores marketing-SMS proof:
  - `profile_id` references `profiles(id)` with cascade delete.
  - `phone_e164` is required and constrained to E.164.
  - `consented_at` and `created_at` are server-stamped.
  - `consent_version` stores `MARKETING_SMS_CONSENT_VERSION` from `src/lib/marketing-consent.ts`.
  - `source` is constrained to `marketing_account`.
  - `disclosure_text` stores the exact marketing-SMS disclosure shown to the customer.
- Marketing SMS proof is intentionally isolated from transactional `sms_consents`. GH-834 account SMS consent reads `sms_consents` by profile and phone with no marketing source filter, so writing marketing proof there would falsely enable transactional booking/service texts.
- RLS is enabled on both GH-838 tables with one admin-only `FOR ALL` policy each; runtime account reads/writes go through the service-role endpoint.
- No marketing sender exists yet. These records only preserve future marketing preferences and proof.
- Migration: `db/migrations/20260608_add_communication_preferences.sql` (+ `db/schema.sql`).
  Shared `wbqjeasgxakubqcutgjt` DDL apply is Codex/operator-owned.

## GH-1116 — Chat translation backend

- Additive profile language preference:
  - `profiles.preferred_language text`, nullable; null means unset.
  - BCP-47-style check allows tags such as `en`, `es`, and `pt-br`.
  - Runtime reads/writes use `GET/POST /api/account/profile` as
    `profile.preferredLanguage`; language-only POST bodies do not require or
    overwrite `fullName`/`phone`.
- Additive translation cache table:
  - `message_translations(id, message_id, target_lang, translated_text, source_lang, created_at)`.
  - Unique cache key: `(message_id, target_lang)`.
  - RLS select policy delegates to `can_read_message_thread` through the parent `messages.thread_id`.
  - Browser clients have select only; writes happen through the server endpoint with the service-role client.
- Runtime API:
  - `POST /api/translate`
  - body: `{ "message_ids": ["<uuid>"], "target_lang": "es" }`
  - response: `{ "translations": { "<message_id>": { "translated_text": "...", "source_lang": "en", "target_lang": "es", "status": "cached" | "translated" | "not_required" } } }`
  - the caller's bearer token gates every requested message through message RLS before cache reads or Azure calls.
- Azure secrets stay server-side:
  - `AZURE_TRANSLATOR_KEY`
  - `AZURE_TRANSLATOR_REGION`
  - optional `AZURE_TRANSLATOR_ENDPOINT` defaults to `https://api.cognitive.microsofttranslator.com`.
- Migration: `db/migrations/20260622_add_chat_translations.sql` (+ `db/schema.sql`).
  Shared `wbqjeasgxakubqcutgjt` DDL apply is Codex/operator-owned.

## GH-1125 — Chat UI-string translation endpoint

- Additive UI-string cache table:
  - `ui_string_translations(source_text, target_lang, translated_text, source_lang, created_at, updated_at)`.
  - Primary cache key: `(source_text, target_lang)`.
  - `source_text` is capped at 500 characters and must be non-blank.
  - RLS is enabled and `anon`/`authenticated` direct access is revoked; runtime reads and writes go through the server endpoint with the service-role client after user auth.
- Runtime API:
  - `POST /api/translate-ui`
  - body: `{ "strings": ["Show original"], "target_lang": "es" }`
  - response: `{ "translations": { "Show original": "Mostrar original" } }`
  - the endpoint requires an authenticated user, applies the shared translation rate-limit bucket, normalizes BCP-47 target language tags, and enforces per-request string count plus per-string/total character caps before Azure calls.
- Azure secrets stay server-side and use the same environment contract as `POST /api/translate`.
- Migration: `db/migrations/20260622_add_ui_string_translations.sql` (+ `db/schema.sql`).
  Shared `wbqjeasgxakubqcutgjt` DDL apply is Codex/operator-owned.

## GH-1127 — Chat message delete

- Migration: `db/migrations/20260622190224_gh_1127_delete_chat_message.sql` (+ `db/schema.sql`).
- New RPC:
  - `delete_chat_message(p_message_id uuid) returns text[]`
  - admin-gated via `is_admin()` and callable only by `authenticated`/`service_role`, matching `delete_chat_attachment`.
  - hard-deletes the `messages` row, relies on existing FK cascades for `message_translations` and `message_attachments`, recomputes `message_threads.last_message_id/last_message_at`, and returns attachment storage paths for best-effort R2 cleanup.
- New API:
  - `DELETE /api/messages`
  - request: `{ "messageId": "<message_uuid>" }`
  - response: `{ "ok": true }`
  - rejects unauthenticated, impersonated, malformed, and non-admin calls.
- Realtime propagation uses the existing `messages`, `message_threads`, and `message_attachments` publication entries; UI should remove/tombstone on message DELETE events.
