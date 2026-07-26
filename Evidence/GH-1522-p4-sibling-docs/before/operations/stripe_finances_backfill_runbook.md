# Stripe Finances Backfill — Operator Runbook

Operator-facing contract for the admin Stripe backfill endpoint added in GH-1358
(Finances Phase 1b). This is a service-role maintenance action that reads live
Stripe and writes shared Supabase, so it is admin-only and dry-run by default.

## What it does

Resolves existing booking/invoice payment references to Stripe PaymentIntents,
upserts the normalized Stripe rows (PaymentIntent + Charge) by object ID, and
populates `invoices.stripe_payment_intent_id` so historical `cs_`-referenced
invoices join correctly into the Finances queries.

- Resolves `pi_` references directly and `cs_` references via the Checkout
  Session. `cs_` resolution verifies session ownership and skips a candidate
  whose session points at a different booking.
- Idempotent by Stripe object ID (safe to re-run).
- Writes a per-run event watermark so a delayed older `payment_intent.*`
  webhook cannot overwrite a snapshot the backfill just wrote.
- Does **not** re-issue invoices (invoice uniqueness stays
  `(booking_id, payment_reference)`).
- Failed Stripe reads are collected into the run summary, not fatal.

## Endpoint

`POST /api/admin/stripe/backfill-finances`

### Authorization

- Requires an authenticated **admin** operator (`requireOperationalWriter`,
  then an explicit `role === "admin"` check → `403` otherwise).
- View-as / impersonation read-only sessions are rejected.
- Rate limited per operator (`admin_stripe_backfill`).

### Request body (all optional)

| Field    | Type    | Default | Meaning |
|----------|---------|---------|---------|
| `dryRun` | boolean | `true`  | When `true`, computes the plan and summary without writing. Set `false` for a real run. |
| `from`   | string  | —       | Start of the candidate window (invoice issue time). |
| `to`     | string  | —       | End of the candidate window. |
| `limit`  | number  | `100`   | Max real candidates processed this run (max `500`). Output budget only — the scanner pages through the full window regardless. |

### Response

```json
{ "summary": {
  "dryRun": true,
  "candidates": 0,
  "resolved": 0,
  "skipped": 0,
  "failed": 0,
  "failures": [{ "paymentReference": "cs_…", "reason": "…" }],
  "invoicesLinked": 0
} }
```

Error statuses: `400` invalid payload, `403` non-admin, `503` Stripe key not
configured, `500` unexpected failure.

## Run procedure

1. **Dry run first** (default). Call with the intended `from`/`to`/`limit` and
   `dryRun` omitted or `true`. Review the summary: `candidates` (found),
   `resolved` (would be linked/synced), `skipped`, `failed` + `failures[]`.
2. **Investigate failures** before a real run — each entry names the payment
   reference and reason.
3. **Real run**: repeat the same window with `dryRun: false`. Because the
   operation is idempotent by object ID, re-running is safe and only touches
   still-unresolved candidates.
4. **Repeat by window** for large backfills — bound each run with `from`/`to`
   and `limit`, and re-run until `candidates` reaches zero for the window.

## Safety notes

- Only run against the intended Supabase project; this writes shared finance
  data (normalized Stripe rows + invoice PI links).
- Prefer bounded windows over one unbounded pass.
- The scanner is complete and stable (fixed-size paging with a deterministic
  order), so a real run over a window processes every eligible candidate in
  that window.
