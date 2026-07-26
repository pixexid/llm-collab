# Consent & legal copy drafts — GH-826b / 826c / 826d

> ⚠️ **DRAFTS FOR LEGAL REVIEW — not legal advice.** Claude authored these as a starting point for the operator's counsel to review, edit, and approve before any of it ships. Each copy block lives (or will live) in a single versioned constant so legal can refine wording without code churn, mirroring the shipped `TERMS_VERSION` (GH-825) and `SMS_CONSENT_VERSION` (GH-833) pattern.

Status: GH-826 was split into four children. **826a (TCPA SMS opt-in) shipped (GH-833); its account-side consent management shipped as the GH-834 follow-up** — `/app/account` → Notifications now reads the truthful effective SMS state (phone-bound: consent present AND not suppressed) and lets a user grant or withdraw via authenticated, caller-scoped endpoints (`/api/account/sms-consent`), reusing `sms_consents` + `sms_opt_outs` with no new DDL. Granting from the account clears any prior opt-out so it takes effect (opt-out otherwise wins); the booking-flow `SMS_CONSENT_LABEL` / `SMS_CONSENT_DISCLOSURE` are reused verbatim. This doc covers the remaining three children, each its own lane, each blocked on legal sign-off of the copy below.

---

## GH-826b — Recurring-service agreement

**When it shows:** only at checkout when the booking cadence is recurring (`frequency !== 'one_time'`). It is an additional acknowledgment shown alongside the existing required Terms checkbox (GH-825) — it does NOT replace it.

**Why:** a recurring booking creates an ongoing card-on-file billing relationship; the customer should affirmatively acknowledge the cadence, the recurring charge, and how to cancel/skip before the series starts.

**Checkbox label (draft):**
> ☐ I understand this is a recurring {{cadence_label}} cleaning. My card on file is charged after each completed clean, and I can skip or cancel any upcoming visit anytime from my bookings.

**Expanded agreement text (draft, shown in a "Recurring terms" expander):**
> **Recurring service agreement.** You're booking a recurring {{cadence_label}} clean. Here's how it works:
> - **Schedule.** We'll return on your selected cadence ({{cadence_label}}); each visit's date and arrival window are shown in your bookings before it happens.
> - **Billing.** Each visit is charged to your card on file **after** that clean is completed — never before. The per-visit price is {{per_visit_price}}.
> - **Skip or cancel.** You can skip a single visit or cancel the whole series anytime from your bookings, up to [X hours] before a scheduled visit, at no charge. Cancellations inside that window may be charged per our cancellation policy.
> - **Price changes.** If your per-visit price ever changes, we'll notify you by email before the next charge.
> This agreement is in addition to our [Terms of Service] and [Privacy Policy].

**Persistence (additive, DB-gated):** mirror GH-825 — a `RECURRING_AGREEMENT_VERSION` constant + either additive nullable booking columns (`recurring_agreement_version`, `recurring_agreement_accepted_at`) or a `recurring_agreement_accepted` booking_event recorded on the parent + shared across the recurring series.

**RESOLVED (from codebase research 2026-06-07):**
- Cancellation window = **48 hours**, **$50 fee** for late cancellation. Source of truth: `src/routes/terms.tsx`, `src/routes/policies.tsx`, `src/components/FAQ.tsx` all state 48h. So the agreement copy uses *"up to 48 hours before a scheduled visit, at no charge; cancellations inside 48 hours may incur a $50 fee per our cancellation policy."*
- ✅ **OPERATOR DECISION 2026-06-07: standardize on 48 hours (2 days).** Rationale: 24h is too near the next scheduled job. 48h also matches the existing Terms/policies/FAQ, so this is consistency + the better policy. **The booking-flow microcopy (`src/components/quote/BookingStep.tsx` `cancellationPolicy="Free changes up to 24 hours…"`) must be corrected 24h → 48h** to match Terms + the recurring agreement. Fold this one-line fix into the 826b lane (it owns cancellation copy) or a tiny standalone issue.

**Still open for legal/operator:**
1. Whether the per-visit price-change notice should also offer a re-consent step or is notice-only.

---

## GH-826c — Marketing / communication preferences

**STATUS: SHIPPED (GH-838).** Account → "Marketing & promotions" section (a distinct card below Notifications) with two default-OFF toggles (Marketing email, Marketing texts), the approved copy below, and an honest "nothing is sent yet" note. Persistence is additive + ISOLATED from transactional consent: `communication_preferences` (marketing_email/sms_opt_in booleans) + a SEPARATE append-only `marketing_sms_consents` proof table (NOT `sms_consents` — the GH-834 transactional read path has no source filter, so a marketing row there would falsely enable transactional SMS). Marketing SMS uses its own `MARKETING_SMS_CONSENT_VERSION` + disclosure. Endpoint `/api/account/communication-preferences` (authenticated, caller-scoped). No sender wired — honest scaffolding.

**Where it lives:** the Account → Notifications surface (separate from the transactional SMS consent toggle, which is GH-834). This governs **marketing** messages (promotions, tips, seasonal offers) — distinct from transactional service messages (reminders, receipts), which customers always receive about their own bookings.

**Why the separation matters:** marketing SMS is a **higher legal bar** than transactional — TCPA requires separate *prior express written consent* for marketing texts, and it must not be bundled with transactional consent or made a condition of service.

**Toggle copy (draft):**
> **Marketing email** — ☐ Send me Amiga news, cleaning tips, and occasional offers by email. *You can unsubscribe anytime from any email.*
>
> **Marketing texts** — ☐ Send me occasional Amiga promotions and offers by text. *Separate from your booking/service texts. Consent is not a condition of any purchase. Msg & data rates may apply. Reply STOP to opt out.* See our [Terms] and [Privacy Policy].

**Persistence (additive):** a small preferences record — either additive `profiles` columns (`marketing_email_opt_in boolean`, `marketing_sms_opt_in boolean`, `marketing_prefs_updated_at`) or a `communication_preferences` table. Marketing SMS opt-in, if granted, should also write an `sms_consents`-style proof row with a marketing-scope `source` and its own disclosure text (it's a separate consent from the transactional 826a one).

**RESOLVED (codebase research 2026-06-07):** no marketing email/SMS sender exists today (only transactional Resend + the GH-833 transactional SMS consent). So 826c is **honest scaffolding** — toggles default OFF and record a real opt-in; nothing sends until a marketing program is built.

**Still open for legal/operator:**
1. Marketing SMS, when launched, needs its own disclosure version + a 10DLC campaign classified as *marketing* (separate from the transactional campaign).

---

## GH-826d — Cookie consent

**STATUS: SHIPPED (GH-839).** Site-wide cookie consent banner + accessible `Manage` modal, mounted client-only in `src/routes/__root.tsx` (covers public routes AND `/app`). Categories: Essential (always on), Analytics + Marketing (default OFF). Client-side persistence in `localStorage` via `src/lib/cookie-consent.ts` (`COOKIE_CONSENT_VERSION` + timestamp + per-category booleans; a version bump re-prompts; "no choice yet" = essential-only and the banner persists). Gate API `useCookieConsent().isCategoryAllowed("analytics")` / `<ConsentGate>` is what a future GA loader checks. **No GA/gtag/marketing script or visitor tracking cookie is loaded in this lane** — gate/framework only (Essential-only behavior today, Google Search Console only). Accept all / Reject non-essential are equally prominent; no pre-checked non-essential. **amiga-concierge note:** if the bot surfaces cookie/privacy guidance, it should state Amiga uses essential cookies only today (analytics is opt-in and not yet active) and never imply tracking is on by default.

**What:** a site-wide cookie consent banner + stored preference. **Privacy-preserving default: decline non-essential** (the project privacy rule — never pre-opt-in to analytics/marketing cookies, never dark-pattern the accept).

**Banner copy (draft):**
> **Cookies.** We use essential cookies to make this site work. With your permission we'd also use analytics cookies to understand how the site is used. We don't sell your data. [Accept all] [Reject non-essential] [Manage]
>
> *(Accept all and Reject non-essential are equally prominent. No pre-checked non-essential categories.)*

**Category descriptions (draft, in "Manage"):**
> - **Essential** *(always on)* — Required for the site to function: security, your session, your booking in progress. Cannot be turned off.
> - **Analytics** *(off by default)* — Helps us understand which pages are used so we can improve them. Anonymous/aggregated.
> - **Marketing** *(off by default)* — Used to measure and improve our ads. Only enabled if you opt in.

**Persistence:** consent stored client-side (cookie/localStorage) with a `COOKIE_CONSENT_VERSION` + timestamp + per-category booleans; analytics/marketing scripts are **category-gated** (don't load until consent). Independent of booking/account — site-wide.

**RESOLVED (codebase research + operator 2026-06-07):** no on-page analytics or marketing tooling is wired today. Operator confirms the only tool today is **Google Search Console** — a server-verified webmaster/crawl tool that sets **no visitor tracking cookies**, so it is NOT a consent concern. Operator expects to add **Google Analytics (or similar) later**. So 826d ships **Essential-only now**, with the **Analytics category framework built and ready to gate Google Analytics (gtag)** the day it's added — GA must load only after the visitor opts into Analytics.

**Still open for legal/operator:**
1. Jurisdiction scope — the stricter GDPR-style "reject by default + explicit opt-in" posture is already the chosen default here (also CCPA-friendly); confirm no region-specific variant is needed at launch.

---

## Cross-cutting notes

- **Version constants** for all three mirror the shipped `TERMS_VERSION` / `SMS_CONSENT_VERSION` pattern — bump on any material copy change; customers/visitors re-consent against the new version.
- **All persistence is additive** → each lane goes through the Codex shared-Supabase DB gate; no destructive DDL.
- **Composes with GH-825**, doesn't duplicate it — privacy acknowledgment is already captured by the GH-825 combined Terms+Privacy clickwrap, so none of these re-ask for privacy acceptance.
- **Sequencing:** 826c (marketing) and 826d (cookie) are low launch-urgency scaffolding; 826b (recurring agreement) matters as soon as recurring bookings are actively sold.
