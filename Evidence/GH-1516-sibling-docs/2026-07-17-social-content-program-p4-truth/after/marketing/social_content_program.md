# Social + Content SEO Program (Lane B shipped; Lane C1 MERGED (913895c5); C2 implemented/under-review; D deferred; rest proposal)

> **Authority split (2026-07-14; updated 2026-07-15).** Review/approval authority is DECIDED (Lane C1/C2, GH-1491). The remaining cadence, media, downstream delivery scheduling, and external-publication program is still a proposal pending operator decisions. Accepted/refined children:
> **GH-1489 / Lane B** (merged `371a8dd`) implements admin-triggered draft generation from an already-published Service
> Note into three private `needs_review` rows. **GH-1491 / Lane C1** (**MERGED** — squash `913895c5`, PR #1492,
> 2026-07-15; issue closed, shared migration applied) adds the backend/shared-DB
> social review authority — closed `needs_review`/`approved`/`rejected` lifecycle, allowlisted edits, immutable review audit,
> optimistic-concurrency RPCs, per-row + atomic-set approval — and ends at review authority with **no scheduling, no delivery,
> no provider call, no publication**. Lane C2 (direct-app review UI) is **implemented and under exact-head review (repair round in progress)** under
> GH-1493/TASK-56A85C. **GH-1515 adds the backend-only, admin-authenticated generation-status read
> prerequisite** (`claimed | running | succeeded | failed | blocked | lease_expired | provider_outcome_unknown`
> plus a server-derived `safeToStart` boolean); GH-1515 itself adds no UI, provider call, scheduling,
> delivery, or publication. **GH-1516 / TASK-58FDE1 (the P4 UI child) now SHIPS the direct-app
> affordance on that authority**: on a published note's social panel an admin sees a **Generate drafts**
> control **if and only if** the server reports `safeToStart=true`, and — while `safeToStart=false` for a
> `provider_outcome_unknown` operation whose exact retained idempotency key this session still holds — a
> **Recover last run** control that settles that original operation without starting or paying for a
> second run. There is **no automatic recovery** (no sweeper/cron settles `provider_outcome_unknown`),
> and the UI still makes **no scheduling, delivery, publication, or Postiz call** — it writes drafts for
> review only. The accepted Lane B contract in §§2, 3, and 5.2–5.3 remains implementation authority.

> **⛔ DOWNSTREAM RE-PROVIDER (2026-07-15) — Radaar is dropped; Postiz-integration boundary frozen by Codex.** The downstream
> delivery/scheduling platform is now self-hosted **Postiz** (https://github.com/gitroomhq/postiz-app + Postiz Agent; local-dev-first;
> long-term one self-hosted Postiz shared across Amiga and future projects), replacing Radaar everywhere below. Ruling: **C1/C2 stay
> Amiga's sole review/approve authority** — Postiz's Public API exposes draft/schedule/now but no approval-audit, idempotency,
> source-invalidation, or optimistic-concurrency contract, so approval must not move into Postiz. Deferred **Lane D = a Postiz
> integration** that consumes only current Amiga-approved rows (`approved` AND `invalidated_at is null`), maps project/channel→Postiz
> organization/group/integration, creates scheduled posts only after explicit Amiga authorization (or a Postiz-draft-only explicit
> handoff), persists Postiz postId/integration/status, and supports fail-closed invalidation/cancel/reconciliation. Amiga retains
> content safety, approval, no-send/kill switch, invalidation, and delivery attempt/audit; postiz-agent/CLI/MCP is an operator/agent
> tool, never an autonomous bypass around Amiga approval. Every Radaar reference in §4 / §5.4 / §8 and the scheduler/integration
> matrix is stale and non-authoritative. No Lane D implementation or Postiz call is authorized; no-send / no-publication gates remain.

> **⛔ ARCHITECTURE SUPERSEDED (2026-07-12; provider further superseded 2026-07-15 — see the re-provider banner above: Radaar is dropped for self-hosted Postiz).** §4 / §5.4 / §8 (Radaar CSV bulk-import + scheduler-owned
> approval) are NO LONGER the plan. The operator confirmed adapting Pixexid's proven queue and had
> (as of 2026-07-12) configured Amiga channels in Radaar. That 2026-07-12 interim plan delivered via **per-channel Radaar webhooks**
> with a **durable Supabase queue** and an **in-app operator review/approve** step (not CSV, not
> scheduler-owned approval). The authoritative, current split lives in the refined task
> **TASK-91F563** ("Refined implementation-ready split"). The trigger seam (§5.1), sanitization (§5.2),
> per-channel prompt contracts (§5.3), and media pipeline (§6) below remain valid and are reused.

Created: 2026-07-10. Vendor research: ZCode 2026-07-11. Reframed to a proposal 2026-07-12. Status:
**Lane B (draft generation) SHIPPED 2026-07-14 under GH-1489 (PR #1490, merged `371a8dd`). Lane C1
(social review authority) is MERGED 2026-07-15 (GH-1491/TASK-3CF55E, squash `913895c5`, PR #1492).
Lane C2 (direct-app review UI) is implemented and under exact-head review (repair round in progress) under GH-1493/TASK-56A85C. Lane D
(self-hosted Postiz downstream delivery) remains a deferred child. The broader cadence/media/
extra-channel content program below remains an unaccepted proposal pending operator review.**

Builds on `marketing/seo_strategy.md` (local SEO pillars) and `seo_standards_internal_external.md`
(GBP weekly posts, review velocity). This program is the *content engine* that feeds those pillars.

This is the strategy parent (GH-1442). It records proposed program decisions and child-task split,
while documenting the separately accepted GH-1489 Lane B contract. It does not authorize approval
UI, media, delivery integrations, or external publication. Later UI work is designed directly in the
app; no `design/**` lane.

---

## 1. Goal and positioning

Amiga's edge is real: owner-operated, W-2 model, transparent zone pricing, real service notes from
real cleans. The content program turns operational exhaust (service notes, quotes, route days) into
public content that ranks, gets shared, and builds the "honest local cleaner" brand.

Tone: **warm, funny-adjacent, radically transparent.** We can joke about ovens and mystery smells;
we never joke about a customer's home or shame mess. Price honesty is the recurring theme — it's
also exactly what GH-1430/1431/1441 shipped (zone pricing, route days, hand-reviewed big jobs,
city-page price-vs-engine reconciliation).

### Humor ceiling (proposed v1 — pending operator review)

Warm and informative; light self-deprecating humor about cleaning work is allowed. Never joke about
a customer, their home, or their mess. Label dramatized content. Channel-specific modulation:

- **GBP:** factual, no humor risk.
- **LinkedIn:** operational insight, minimal humor.
- **Instagram:** light humor allowed, owner voice.

## 2. Current state (verified 2026-07-13; A2.5 backend contract)

Amiga's **rendered** service-note surface is now DB-backed (A3 / GH-1487): the public `/service-notes`
feed, per-note canonical `/service-notes/{slug}` pages, taxonomy/pagination, RSS, and runtime sitemap all
read the A2.5 published authority, not static files.

- `src/data/service-notes.ts` is no longer the public runtime feed; it remains seed/schema-test authority
  only, and `src/data/service-notes-legacy-slugs.ts` holds the 13 legacy hash → canonical-slug map.
  `scripts/seo-audit.mjs` still enforces the static privacy/dollar guards over the rendered source.
- The database now has restricted source/brief/media/editorial tables, 13 corresponding published
  seed notes, lifecycle/generation authority, and the compatibility
  `list_published_service_notes(slug,service_type,city_slug,limit)` public/RSS RPC.
- A2.5 adds additive published-only keyset browse, mandatory facets, content-free archive resolution,
  safe `sourceType`, exact launch taxonomy, and deterministic published-slug JSON sitemap authority at
  `GET /api/service-notes/published?format=sitemap`. It preserves `format=rss` and does not add A3 UI.
- Unsafe `relatedServicePage` values are rejected by a validated 20-path storage invariant and are
  also suppressed in generation/public projections; shared apply fails closed rather than rewriting
  any unsafe existing row.
- A2.5 itself did not add a social queue. GH-1489 Lane B now adds the private draft queue and
  generation authority described below; downstream (deferred Lane D — Postiz) delivery and external
  publication remain absent.
- The `serviceNotes` field in `src/server/staff-assigned-run-packet.ts` is an unrelated staff
  run-packet description text field — it is NOT a marketing-diary producer.

**Lane B contract:** `POST /api/operations/service-notes/social/generate` accepts only an operation UUID
and published-note UUID, derives one ordered `google_business_profile | instagram | linkedin` set in
one provider call, and atomically stores three `needs_review` rows under a separate social operation
and audit. It has no approval, scheduler, downstream provider, delivery, or publication side effect. The static
13-entry batch remains a compatibility/test source; published database notes are authoritative.

## 3. Channels and what each one gets

### v1 initial channels (ACCEPTED for Lane B / GH-1489; deterministic order `google_business_profile | instagram | linkedin`; Facebook is not a separate draft — any FB cross-post stays in deferred Lane D)

| Channel | Role | Cadence | Voice |
|---|---|---|---|
| Google Business Profile | Highest-converting local surface | 2–3 posts/wk | Plain, photo-led, service-area keywords |
| Instagram (+ FB cross-post) | Visual proof: before/after, route-day life | 3–4/wk | Light humor, human, owner-voice captions |
| LinkedIn | The business story | 1 article/mo + 1 post/wk | "Building a W-2 cleaning company in the Bay" — operations, pricing math, hiring ethics |

### Deferred channels (proposed — pending operator review; see decision packet O-3)

| Channel | Status | Reason |
|---|---|---|
| Nextdoor | Deferred — gated or manual child | Nextdoor has a gated Publish API (requires application + approval). Native scheduling not found in reviewed official Nextdoor docs; Nextdoor absent from reviewed official Radaar pages. A separate child must justify the API application effort or manual posting. |
| Medium | Manual-only | Medium's official API is deprecated and closed to new integrations (CONFIRMED). Not automatable through an accepted/supported v1 integration. 1–2 articles/mo can be published manually in Medium's editor. |
| TikTok / YouTube | Deferred until media readiness | Radaar supports both, but media pipeline (S4) must be ready first. |

Article seeds (channel-fitted):
- **LinkedIn:** "Why we hire W-2 cleaners in a 1099 industry (CA ABC test, and why it's good
  business)." / "Route days: the logistics trick that makes small-city cleaning affordable." /
  "Solo-operator financial stack: how we estimate taxes without a CFO."
- **GBP:** city-specific micro-posts derived from service notes ("This week in San Mateo: 3
  kitchens, 1 heroic oven").
- **Medium (manual):** "What house cleaning actually costs in the Bay Area (and why Pescadero is
  different)" — publishes our real zone table; nobody else dares. / "The Initial Reset: why your
  first clean isn't like your fifth."

## 4. Scheduler and vendor capabilities (evidence-backed)

### Scheduler: deferred Lane D — self-hosted Postiz integration (re-providered 2026-07-15, boundary frozen by Codex)

Radaar is dropped as the downstream scheduler/delivery platform. The replacement is a self-hosted
**Postiz** integration (https://github.com/gitroomhq/postiz-app + Postiz Agent; AGPL-3.0, pin a
tested version), consumed as **deferred Lane D** — not built now. Detailed Postiz vendor
evidence/capability research (channel coverage, API surface, pricing/self-host cost, per-project
org+key topology) is **Codex-owned Lane D research**, not authored in this program doc. The frozen
boundary this program doc must respect: Lane C1/C2 (GH-1491) stay Amiga's sole review/approve
authority — Postiz's own approval/draft/schedule features are never used as the approval rail. Lane D
consumes only current Amiga-`approved` rows and submits **schedule-only** (never immediate) requests
after explicit Amiga authorization; Lane D persists the returned Postiz postId/status and owns
retries/reconciliation via polling (Postiz has no idempotency-key/result-webhook contract) plus a
kill switch that also demotes/cancels already-queued Postiz posts. No CSV bulk import, no
scheduler-owned approval, no public Postiz API assumption beyond what Codex's Lane D research
confirms.

**v1 ingest path:** deferred; will be authored as part of Codex's Lane D research and implementation,
not CSV bulk import.

### Medium (evidence)

| Capability | Status | Source |
|---|---|---|
| Publishing API exists | CONFIRMED (OAuth2, integration tokens) | https://developers.medium.com/ |
| API status | CONFIRMED DEPRECATED | "The Medium API is no longer supported. We do not recommend using it." + "We don't allow any new integrations with our API." https://github.com/Medium/medium-api-docs |
| New integration tokens | NOT VERIFIED — out of v1 | Community reports indicate the Settings → Integration tokens UI option was removed and legacy tokens may still work, but no official Medium source is cited here. Irrelevant to v1 (manual-only). |
| Third-party scheduler support | NOT FOUND in reviewed schedulers | No major reviewed scheduler (Buffer, Hootsuite, Radaar) lists Medium as a currently supported channel. Medium's API docs state no new integrations are allowed. Not a v1 dependency. |
| CSV/bulk import | NOT FOUND | No official Medium documentation references CSV or bulk import. Single-post import by URL exists. Irrelevant to v1 (manual-only). |

**v1 posture:** Medium is manual-only. Articles are written and published directly in Medium's editor.

### Nextdoor (evidence)

| Capability | Status | Source |
|---|---|---|
| Publish API exists | CONFIRMED (OAuth2, business accounts) | https://developer.nextdoor.com/ — launched Dec 2023 |
| API access model | CONFIRMED GATED | Must apply for developer token, specify "Publish API", provide company logo + redirect URI. https://developer.nextdoor.com/reference/applying-for-access |
| Third-party scheduler support | INFERENCE — out of v1 | Developer docs mention "Social Media Management platforms" as a use case. Specific partners (Orlo, Social News Desk) appear in public web results but are not cited from official Nextdoor sources here. Out of v1 scope (Nextdoor is deferred). |
| Native in-platform scheduling | NOT FOUND | Help center describes real-time posting only. https://help.nextdoor.com/s/article/How-to-post |
| Share Plugin (open, no application) | CONFIRMED | Lighter-weight embed for ad-hoc sharing. https://developer.nextdoor.com/docs/sharing-overview |

**v1 posture:** Nextdoor is deferred. A separate child must justify the gated API application or
commit to manual posting by the operator.

## 5. Target architecture: accepted draft boundary → deferred publication program

The requested outcome is **"each newly generated service note creates drafts for every selected
channel."** That is the target end-state below. The static 13-entry batch is the **first test /
fallback**, not the destination.

### 5.1 Trigger seam

Lane B implements the admin-only manual trigger
`POST /api/operations/service-notes/social/generate`. An automatic publish event remains deferred:

1. **Note-creation hook.** When a service note is created/finalized (the future note-authoring
   workflow, or an admin "publish note" action), emit a lightweight domain event
   `service_note.published { noteId, citySlug, serviceType, season, privacyLevel }`. This is the ONE
   new seam the program needs; it can be a direct function call at the existing note-write site
   before any queue/webhook exists — no new infra required to start.
2. **Draft-generation consumer (implemented by GH-1489).** The manual action accepts only operation
   UUID + published-note UUID, derives all authority from the database snapshot, and writes one
   complete `SocialDraftSet`.
3. **Legacy seeds.** The 13 seeded published database notes are valid sources, including notes with
   no story brief. The static TypeScript entries remain seed/schema-test authority, not a batch
   generation input or public runtime fallback.

Sequence (note → published post):

```
published database Service Note + manual admin action (implemented Lane B)
  → server-derived source snapshot, price, and canonical-link authority
  → one strict selector call over exact source fragments
  → deterministic server rendering + validation (GBP / Instagram / LinkedIn) → SocialDraftSet
  → atomic three-row private queue commit (`needs_review`)  ← Lane B stops here
implemented/refined:
  → media brief per draft → media pipeline (§6) → rendered asset(s)
  → HUMAN REVIEW (Lane C1 review authority: approve/edit/reject/reopen; GH-1491, MERGED)  ←
    nothing proceeds without this; C1 ends at review authority (no scheduling, no delivery,
    no provider call)
deferred program proposal (Lane D, not built):
  → downstream delivery via a self-hosted Postiz integration consumes only current C1-`approved`
    rows and submits schedule-only after explicit Amiga authorization → published
```

Two review gates are deliberate: **(a)** operator review of generated drafts+media in Amiga's own
Lane C1 review authority before anything is eligible for delivery, and **(b)** the deferred Lane D
Postiz submission only after explicit Amiga authorization. No auto-publish at any point in v1.

### 5.2 Sanitization and grounding contract (implemented Lane B)

Grounding is a closed derivation, not a keyword classifier. The server splits the published headline,
excerpt, body, and takeaway into a deduplicated, ID-addressed catalog of exact sentence/content
fragments. Catalog identity ignores case, whitespace, and superficial punctuation separators anywhere
in the identity without changing retained source text, so `Room-by-room` and `Room by room` cannot
count as two facts. Fewer than two distinct normalized fragments fails before a provider call. The
provider may select exactly two distinct known fragment IDs per channel and closed
price-mode/format enums; it cannot return persisted prose, hashtags, image instructions, or links. The
server reproduces the selected fragment text exactly inside a small, explicit set of neutral channel,
CTA, price, before-Amiga attribution, and disclosure templates. It also deterministically supplies the
channel hashtags, Instagram text-only image brief, and canonical URL. Unknown IDs, duplicate content,
extra fields, or arbitrary factual prose reject the whole set. Therefore every persisted factual
sentence/detail is an exact source fragment, while every other persisted word is server-owned neutral
framing. The strict schema uses supported nested `anyOf` object variants to bind each channel to its
server-accepted price mode and compatible price format; remote-coastal requests therefore expose only
GBP `none`, Instagram `estimate-only`, and LinkedIn `none`, never a paid-call option the server will
reject. The server independently enforces exact channel order and distinct fragment IDs.

Before the request is serialized, every provider-input field is structurally or safety validated;
every complete published source field (including `toneHumor`) and every retained catalog fragment is
screened for customer contact details, street addresses/access facts, direct quotes,
testimonial/review framing, prohibited credibility, source-authored price/currency, and public
identifiers. Unsafe-pattern screening runs against both source-exact text and its NFKC-normalized form,
so compatibility forms such as full-width-digit addresses cannot bypass pre-provider rejection.
Street-designator matching rejects numeric, letter-suffixed, and hyphenated house numbers such as
`123 Main Street`, `123B Main Street`, `123-5 Main St`, and `123-B Main Street` while leaving ordinary
alphanumeric and hyphenated identifiers outside address grammar alone. Its closed residential suffix
set covers `alley|aly`, `avenue|ave`, `boulevard|blvd`, `circle|cir`, `court|ct`, `cove|cv`,
`crescent|cres`, `drive|dr`, `highway|hwy`, `lane|ln`, `loop`, `parkway|pkwy`, `place|pl`,
`point|pt`, `road|rd`, `square|sq`, `street|st`, `terrace|ter`, `trail|trl`, and `way`. One
unsafe fragment fails before the provider call even if no returned selection would have used it;
selected fragments and fully rendered fields are screened again. Government/payment
screening removes phone parentheses and normalizes punctuation, Unicode dash punctuation, U+2212
MINUS SIGN, and Unicode whitespace between digits,
then rejects every standalone sequence of at least 9 digits, with no upper bound, as SSN-, phone-,
account-, or payment-like. This includes separated 12-digit forms such as `6759 1234 5678` and IBAN
forms such as `DE89 3704 0044 0532 0130 00`, plus compact parenthesized phones such as
`(408)555-1212`, so dotted, slashed, hyphenated, en/em-dash,
ordinary-space, and thin-space forms cannot bypass it. In sentence-local `payment`, `account`, `card`,
or `bank` context, the normalized threshold tightens to at least 6 digits with no upper bound, so
`Payment account 1234 5678` fails; an ordinary standalone 8-digit date/number outside that context is
not globally rejected. Independently, the NFKC-normalized presence of any closed access-secret label
fails with `unsafe_source` before budget reservation or the provider call. The labels are singular or
plural `PIN`, `OTP`, `one-time password`, `one-time passcode`, `passcode`, `password`, and access/alarm/
door/entry/garage/gate/keypad/lock/lockbox/security `code`. The label-only check NFKC-normalizes and removes
Unicode Format characters before matching, so zero-width insertion anywhere inside a label cannot
hide it; other safety rails still receive the unchanged source. Compound labels accept compact/camel
forms such as `doorcode`, `doorcodes`, `doorCode`, and `oneTimePassword`, horizontal whitespace
(tab or Unicode space separators, including NBSP), or one unspaced machine separator: `_`, `/`, `.`,
a Unicode dash, or U+2212. The matcher does not bridge comma-separated clauses, sentence punctuation
followed by whitespace, CR/LF, or Unicode line/paragraph separators. Unicode-aware outer token
boundaries keep unrelated containing words such as `pinstripe`, `passwordless`, and `outdoorcodes`
from matching. The label alone is enough:
the server does not try to infer whether adjacent prose is a credential value, guidance, or a benign
meta noun. This deliberately rejects both actual secrets such as `Password P@ssw0rd` and guidance such
as `Change password regularly`, `Use password manager guidance`, or `keypad code format guidance`.
Operators must remove or rephrase all such label-bearing source fields before retrying generation.
Unlabeled words, symbol-bearing tokens, and ordinary 6-digit references remain valid. The same closed
label check runs against selected fragments and fully rendered output as defense in depth.
Hashtags are fixed server templates. The Instagram image brief contains the exact selected fragments
and fixed Published Service Note attribution rather than provider-authored detail; a
`fictionalized_or_composite` brief also requires the exact approved disclosure to appear on the
graphic. Before budget reservation, the server enumerates the same exact two-fragment selections
through the same renderers and requires at least one selection per channel to satisfy its word/sentence
profile plus the Instagram 1,000-byte and post-text 4,000-byte UTF-8 bounds. An inevitable overage or
impossible short profile makes zero provider calls; a provider-selected overage when another selection
was feasible is paid invalid output with provider usage retained and zero queue rows.

Price claims follow §7's GH-1441 authority. For on-route output the server alone renders the exact
engine value as `$N`, `USD N`, or `N dollars`; no other numeric or written-number price form is
accepted. Every non-on-route mode, including remote-coastal estimate framing and LinkedIn, contains no
numeric price. One closed static authority defines every retained non-USD code token and derives the
singular, plural, irregular-plural, and common English currency units from the same grouped entries.
Numeric or written-number amounts adjacent to any unit fail in either order before provider input; for
example `500 naira`, `naira 500`, `500 shillings`, `shillings 500`, `500 cedi`, `cedi five hundred`,
`500 cedis`, `500 won`, and `one hundred euros` all reject. The `WON` common-word exception remains
available only when intervening ordinary prose disproves an immediate currency-unit pair, as in
`Won 3 practice rounds`. The generic `dollar(s)` unit remains under the separate exact USD price rail because non-USD
dollar codes share that unit; those non-USD code tokens still reject. Non-USD currency codes and symbols
(for example `EUR`, `€`, `GBP`, or `£`) are rejected in every mode. Currency screening uses both the
source-exact and NFKC-normalized views, so compatibility forms such as `ＧＢＰ １６５`, `１６５ ＥＵＲ`,
and `￡１６５` cannot bypass it. Genuine amount-code forms are matched case-insensitively, so `gbp 165`,
`165 GbP`, `gbp: 165`, and `GbP-165` fail. The complete contextual
common-prose collision set is exactly `ALL`, `TOP`, `TRY`, and `WON`; every other recognized code,
including `CAD`, is unambiguous and rejects whenever it forms a whitespace code/amount pair. Thus
`cad 165`, `Cad 165`, and `165 cad` always fail. An uppercase ambiguous currency-code token adjacent
to a number also always fails closed, so ordinary `ALL 3 rooms` and `TRY 3 passes` are accepted false
positives. Only lowercase/title-case `ALL`/`TOP`/`TRY`/`WON` pairs use context:
prior sentence-local price context can qualify either whitespace form, while price grammar immediately
after either complete whitespace pair also qualifies the pair. Immediate unit-price grammar includes
`/clean`, `/visit`, `/hour`, `per clean`, `per visit`, `per hour`, and `each`; therefore
`CAD 165/clean`, `165 CAD/hour`, `CAD 165 each`, and their lowercase/mixed-case equivalents fail.
Any whitespace code/amount pair with a decimal amount is formal currency syntax regardless of case or
context, so `cad 165.00`, `Cad 165.00`, and `165.00 cad` also fail conservatively. So do
`costs try 165`, `Amount all 3`, `165 try total`, `165 all was the total`, `TRY 165 total`,
`try 165 is the total`, `165 try per clean`, `165 all per visit`, and `165 try per hour`. Ordinary
title-case `Try 3 passes` and `All 3 rooms` remain valid because intervening prose breaks the immediate
grammar. As accepted conservative fail-closed tradeoffs, punctuated ambiguous pairs such as `TOP-5`
and `TOP: 5`, and uppercase ambiguous number pairs, remain rejected as possible currency amounts.
Conversely, a lowercase/title-case `ALL`/`TOP`/`TRY`/`WON` integer pair without the bounded price
grammar can be treated as ordinary prose even when its author intended currency; that unavoidable
lexical ambiguity is accepted for this exact four-token set and no others. That contextual exception
belongs only to the code-token matcher: `500 won` still rejects through the currency-unit authority.
The same additive source-exact plus NFKC views govern the USD authority rail: source forms such as
`＄９９９` or `ＵＳＤ ９９９` reject before a provider call, and an NFKC-obfuscated extra price that
reaches rendered output rejects even when the same post also contains its one server-authorized ASCII price.
Any failure writes zero queue rows.

### 5.3 Versioned set-prompt contract (implemented Lane B)

Lane B uses the distinct version `social-derivation-v1` and one strict-JSON provider call. The server
supplies an exact-fragment catalog plus the server-derived on-route value and remote-coastal flag; the
client supplies none of those authorities. Canonical-link, channel-copy, hashtag, image-brief,
attribution, and disclosure rendering remains entirely server-owned. Route authority also shapes the
provider instructions and supported nested-`anyOf` schema branches, including channel-specific
price-mode/format combinations for remote-coastal requests.

**Authoritative input shape** (conceptual):
```
{ sourceInputFingerprint, sourceFragments:[{ id, text }], serviceType, city, citySlug,
  toneHumor, sourceType, narrativeMode, fromValueOnRoute, remoteCoastalEstimateOnly }
```
**Common output selection fields** (each channel/mode branch fixes its allowed values):
```
{ channel, sourceFragmentIds:[id,id],
  priceClaimMode: "on-route-from" | "estimate-only" | "none",
  priceFormat: "dollar_symbol" | "usd_prefix" | "dollars_suffix" | "none" }
```
**Shared constraints (all channels):** provider output is selection-only; selected fragment IDs must
be exact and unique within each channel. The strict provider schema uses the supported array
`minItems`/`maxItems` constraints plus nested `anyOf` variants and deliberately omits unsupported
`uniqueItems`; the server remains authoritative for distinct-ID and channel-order enforcement. The
server screens all source fields and catalog fragments before provider input, preflights exact rendered
profile/storage feasibility, screens selected and rendered text again, applies
the closed renderer, and prefixes the exact matching relative city/service path with
`SERVICE_NOTE_PUBLIC_SITE_URL` before persistence.
**Idempotency/retry (set-operation model, GH-1489):** idempotency is the social **operation UUID**, not
`(noteId, channel, promptVersion)`. One admin call generates the complete ordered channel set in ONE
provider call + ONE atomic three-row commit; replaying the same operation UUID returns the exact
committed set with no second paid call. Accepted operation and source-note UUID text is canonicalized
to lowercase before service/database use, so uppercase UUID inputs replay against the same PostgreSQL
identity. Uniqueness is `UNIQUE (social_operation_id, channel)` per set
plus one-current `(source_note_id, channel) WHERE invalidated_at IS NULL`. A new set (prompt-version bump
or post-correction regen) is a NEW operation that atomically invalidates/supersedes the prior current set.
The same terminal operation UUID remains bound to its original note/profile and replays its stored
`succeeded`/`failed`/`blocked` outcome even after the deployed server prompt version changes; prompt
bounds and equality still apply to new operations and live `started` retries. If an expired
pre-provider lease is reclaimed after the still-published, `needs_regeneration=false` note was
corrected, the service settles the stable `stale_source` outcome with zero provider calls rather than
misclassifying it as `source_not_published`.
The database stores bounded prompt/model/failure and queue prose/image text in trimmed form with raw-byte
limits. Forward repair fails closed on padded oversized rows and requires every current queue row's
`narrative_mode` and `disclosure_text` to exactly match its source note (exact disclosure for
`fictionalized_or_composite`, otherwise null). A terminal audit must also match its operation's
`failure_code` exactly (including null on success).

| Channel | Length | Humor | Hashtags | Price mode | Extra |
|---|---|---|---|---|---|
| GBP | 40–80 words | none (factual) | none | on-route-from or none | two exact fragments + neutral CTA |
| Instagram | hook + 2–3 sentences | light owner framing allowed | 5 fixed server tags | on-route-from or estimate-only | server-rendered text-only `imageBrief` MANDATORY |
| LinkedIn | 80–150 words | minimal, operational | 2 fixed server tags | none (story, not price) | two exact fragments + neutral operational framing |

**Prompt/server-contract regressions:** for each channel — (a) PII in any catalog fragment → zero
provider calls and whole set rejected;
(b) remote-coastal city ⇒ ordered modes GBP `none`, Instagram `estimate-only`, LinkedIn `none`, with
no exact price and no `on-route-from` schema branch; (c) on-route city with a valid from-value ⇒ price matches the engine value; (d) output
matches the selector JSON schema; (e) arbitrary provider prose and unknown fragment IDs are rejected;
(f) exact selected source fragments survive byte-for-byte in the rendered result, while punctuation-only
duplicate fields cannot satisfy the two-fragment gate; (g) written-number money, lowercase/mixed-case
non-USD amounts, and separator-obfuscated SSN/card-like identifiers are rejected in every
derived field; (h) idempotency: replaying the same social operation UUID ⇒ the exact committed ordered
set, one paid call.

### 5.4 Downstream delivery ingestion (deferred Lane D — Postiz integration)

**Not built; deferred.** The CSV-bulk-import-to-Radaar ingest path is dropped. When Lane D is built,
approved drafts (Lane C1 `review_state='approved' AND invalidated_at IS NULL`) will be consumed by a
self-hosted Postiz integration (Codex-owned research; https://github.com/gitroomhq/postiz-app) that
maps channel → Postiz integration and submits schedule-only requests after explicit Amiga
authorization — no CSV bulk import, no scheduler-owned approval. Amiga's Lane C1 review authority is
already the human-review gate; Lane D adds no additional approval step, only delivery + kill switch +
reconciliation. Detailed Postiz provider topology/evidence is Codex-owned Lane D research, not
authored here.

## 6. AI image/video pipeline (actionable)

An end-to-end generation → review → schedule pipeline. Video is part of it, not indefinitely deferred
— it simply sequences after the image path proves out.

### 6.1 Content formats (concrete)
- **Branded cards (image):** "quote-of-the-clean" fact cards, "route-day Tuesday — {city}" cards,
  honest pricing explainers ("what a first clean includes"), Initial-Reset before/after layout.
- **Real before/after (image):** only with written consent (§6.4); the trust centerpiece.
- **Shorts (video, 15–30s):** stills + captions + licensed/AI music; e.g. "3 kitchens, 1 heroic oven"
  route-day montage, a pricing-honesty explainer, an Initial-Reset time-lapse (with consent).

### 6.2 Prompt/template strategy
- **Templates, not raw one-offs:** a fixed brand-kit template set (type, color, logo lockup, safe
  margins). AI fills copy/imagery *into* templates so every asset is on-brand and repeatable.
- Each `imageBrief` from §5.3 maps to a template id + fill fields. The generator proposes the brief;
  the media step renders it. Dramatized/illustrative imagery is labeled as such on the asset.

### 6.3 Provider selection criteria (choose after a bounded bake-off, not pre-frozen)
Score candidates (e.g. OpenAI images, Ideogram, Flux, a template API like Bannerbear/Placid for the
card layer, a video assembler like Creatomate/Shotstack) on: brand-template fit, text-in-image
fidelity, per-asset cost, commercial-use/licensing clarity, API stability, and content-policy fit.
Pick per-layer (card-render vs generative-image vs video-assembly may be different tools). **Operator
sets the monthly media budget ceiling (decision O-1) before any paid provider is selected.**

### 6.4 Asset provenance + consent (fail-closed)
- Real before/after media requires **explicit written customer consent + source metadata + an
  approval timestamp** before publication. Never present generated/dramatized media as a real
  customer home. Legally-reviewed consent wording + the storage owner are frozen before any real
  media enters the pipeline (decision P-7 / O-4).
- Every asset carries metadata: `{ assetId, kind: real|template|generated, sourceNoteId?,
  consentRef?, templateId?, providerRef?, createdAt, approvedBy?, approvedAt? }`. No `kind:real`
  asset publishes without a `consentRef`.

### 6.5 Review state + storage
- Asset lifecycle: `draft → in_review → approved → scheduled → published` (or `rejected`).
- Stored with the metadata above (owner/location is a child decision); nothing leaves `approved`
  without operator sign-off, mirroring the §5 human-review gate.

### 6.6 Downstream delivery handoff + cost controls (deferred Lane D)
- Approved asset + its post draft will hand off together to the deferred Lane D Postiz integration
  (§5.4), referenced by `assetId`. No auto-publish.
- Cost controls: monthly spend cap (O-1), per-asset cost logged, template reuse preferred over fresh
  generation, and a dry-run/preview before any paid render.

### 6.7 First experiments (smallest useful)
1. Render 3 **branded card** templates from existing service notes (no paid generative model needed
   for the card layer) → operator review. Proves the template+brief+review loop for ~$0.
2. One **AI-generated illustrative card** via one candidate provider → compare cost/quality/brand fit.
3. Only then: one **consented real before/after**, then one **15–30s Short**. Each gated on the prior
   proving out and on the operator's budget/consent decisions.

## 7. Public content to support city pages — TWO distinct surfaces

The operator's "update Amiga public content to support new city pages" means the **public GitHub
repo**, which is a first-class SEO/brand surface separate from the private app.

### 7a. Public repo `pixexid/amiga-cleaning-services` (the requested deliverable)
- Local checkout `/Users/pixexid/Projects/amiga-cleaning-services`, public, `main@f93524e`, single
  tracked file `README.md` (build-in-the-open story + a "Live Pages" list that currently links only
  to Palo Alto).
- **First-class work:** audit the README/info-architecture and expand it to reflect the real live
  footprint — a structured **Service Areas** section grouping the 44 live cities by zone with links
  to key city/service pages and the `/areas` hub, plus honest positioning copy.
- **Hard rails:** truthful only; **no private operations detail**, **no hardcoded/stale pricing** (link
  to the live page, never repeat a number), no customer data. Concrete proposal + example markdown
  live in `social_content_first_wave.md` (deliverable 3).

### 7b. Private-app city/service pages (SEPARATE lane — do not substitute)
- The 220 live `/…-{city}` marketing pages inside the private app. Copy/FAQ refreshes there respect
  `city_service_page_standard.md` + the seo:audit gates and are their own children (public-page
  refresh), NOT part of the public-repo deliverable above.
- GH-1441 (city-page pricing reconciliation) is **shipped** at merge `2b255cef` (PR #1460); it is the
  pricing source of truth for any price-referencing article/public content and no longer blocks this
  parent.

Every LinkedIn/Medium article and public-repo section links back to the matching live city/service
page (anchor discipline per internal SEO standard). Article briefs + cross-link targets:
`social_content_first_wave.md`.

### Pricing-claim safety (GH-1441, shipped 2026-07-11)

City/service-page prices are now reconciled to the live quote engine and gated by a price-vs-engine
audit (`amiga/src/lib/city-pricing-audit.ts`). When social content cites a city "from" price, use
the on-route engine value (date-independent SF +$35 / remote +$100 included; date-dependent
off-route/coastal excluded). For remote-coastal cities (san-gregorio/la-honda/loma-mar/pescadero),
frame as an estimate with manual scheduling — never promise an exact/instant price.

## 8. Approval surface (superseded by GH-1491 / Lane C1, MERGED)

Approval is **Amiga-owned**, not scheduler-owned: Lane C1 (backend/shared-DB, GH-1491, MERGED squash
`913895c5`, PR #1492) is the closed `needs_review`/`approved`/`rejected` review lifecycle with
allowlisted edits, immutable review audit, and optimistic-concurrency RPCs — see
`service_notes_system_spec.md` §5.4 for the authoritative lifecycle. Lane C2 (direct-app review UI,
GH-1493/TASK-56A85C) is implemented and under exact-head review (repair round in progress). The deferred Lane D Postiz integration
consumes only current C1-`approved` rows and adds no approval step of its own.

## 9. Publication boundary (decided architecture; publication itself deferred pending Lane D)

The review/approval architecture is decided, not proposed: Lane C1 (GH-1491, MERGED `913895c5`) is
Amiga's review authority; its direct-app UI, Lane C2 (GH-1493/TASK-56A85C), is implemented and under
exact-head review (repair round in progress). What remains genuinely pending is **external publication**, which is blocked until the
deferred Lane D self-hosted Postiz integration is built and accepted. S1 (Postiz org/API-key +
per-channel integration audit, brand-voice guide, calendar skeleton) is setup-only and not yet done.
No external publication until S1 AND the deferred Lane D Postiz integration (S2b) are accepted. A
separately accepted publication action owns the first external post.

## 10. Child task matrix (decomposition status: B shipped, C1 merged, C2 implemented/under-review, D deferred, rest proposal)

This split started as a proposal; part of it is now decided/shipped. **S2a/Lane B (GH-1489, PR #1490)
is SHIPPED** (merged `371a8dd`). **The approval child (Lane C1, GH-1491/TASK-3CF55E) is MERGED**
(squash `913895c5`, PR #1492, 2026-07-15; issue closed, shared migration applied) — no longer
optional/proposed. **Lane C2 (app-owned review UI, GH-1493/TASK-56A85C) is implemented and under
exact-head review (repair round in progress).** **Lane D (self-hosted Postiz downstream delivery) is an accepted-boundary deferred
child**, not an open proposal. The remaining rows below (S1 setup, S3 articles, public-page
refreshes, S4 media, S5 video, OpenClaw sync) are still proposal/deferred pending operator review and
are created only as the operator accepts each. Owners follow the backend-leans-Codex,
frontend/UI-leans-Claude convention; the queue owner confirms owner at activation. No child is
created with an unresolved blocker — child-level execution gates (account purchase, legal wording,
provider budget) are named in-child, not treated as parent blockers.

| Child | Repo | Owner (by fit) | Depends on | UI/UX class | Bot impact | Safety boundary | Verification | Activation order |
|---|---|---|---|---|---|---|---|---|
| **S1** program setup: self-hosted Postiz org/API-key + per-channel integration audit + brand-voice one-pager + calendar skeleton | docs + ops/account | Codex/operator (account gate) | parent accepted | none (docs/ops) | `amiga-operator-cmo` (voice guide) | no external publication in S1; setup only | doc review; Postiz org/integration audit evidence | proposal, not yet started |
| **S2a / Lane B** — **SHIPPED** (GH-1489, PR #1490, merged `371a8dd`): sanitized structured-draft generator (service-note → per-channel draft objects; PII/$ strip) | amiga (app) | Codex (backend/safety) | none (shipped independent of S1) | none (backend module) | `amiga-operator-cmo` adjacent | sanitize-before-emit; no auto-publish; draft-only contract | unit tests on sanitizer + per-channel schema; `pnpm verify` | shipped |
| **C1** — **MERGED** (squash `913895c5`, PR #1492, 2026-07-15; GH-1491/TASK-3CF55E closed; shared migration applied): backend/shared-DB social review authority — closed `needs_review`/`approved`/`rejected` lifecycle, allowlisted edits, immutable operation + per-row review audit, optimistic-concurrency RPCs, honest replay | amiga (app) | Codex (backend) | Lane B (shipped) | none (backend module) | `amiga-operator-cmo` direct workflow knowledge; `amiga-concierge` pointer-only | ends at review authority; no scheduling/delivery/provider call/publication | shared apply + runtime proof complete; merged at exact head | merged |
| **S2b / Lane D** deferred: self-hosted Postiz delivery integration (Codex-owned research) — consumes only current Lane C1 `approved` rows, schedule-only, per-project org+key | amiga (app) or docs | Codex (backend) | S1 (Lane C1 already merged) | none (backend/ops) | `amiga-operator-cmo` | reads only `approved AND invalidated_at IS NULL`; schedule-only submission after explicit Amiga authorization; kill switch demotes/cancels queued Postiz posts; no publication until proven on a real Postiz account | round-trip Postiz submit → postId/status persisted → poll/reconcile evidence (verified on real account) | deferred |
| **C2** (formerly S2c) — **implemented and under exact-head review (repair round in progress)** (GH-1493/TASK-56A85C): app-owned direct-app review UI, child of merged Lane C1 (GH-1491, `913895c5`) | amiga (app) | Claude (UI) | Lane C1 (GH-1491, merged) | direct-app UI (Impeccable/D8 + desktop/true-393 Browser) | `amiga-operator-cmo` | Lane C1 is Amiga's sole review/approve authority; Postiz never receives approval authority | direct-app UI validation | under exact-head review |
| **S3** article engine (first LinkedIn + owned-site; Medium manual-only) | amiga + docs | per fit | Lane B (shipped) + GH-1441 (pricing claims) | docs/backend (rendered public-page work stays in the separate public-page child) | `amiga-concierge` if public facts/pricing change | no unverified pricing in articles; GH-1441 shipped contract is source of truth | pricing cross-check vs live engine; SEO audit | proposal, not yet started; GH-1441 satisfied |
| **public-page refreshes** (city-page/FAQ amplification) | amiga (app) | Claude (UI) | GH-1441 | direct-app UI (Impeccable/D8 + desktop/true-393 Browser) | `amiga-concierge` | on-route honest pricing per GH-1441; no baked date-dependent surcharge | direct-app Browser (desktop/true-393) | after parent; GH-1441 satisfied |
| **S4** consent + provenance + branded templates + media render integration + provider/budget research | amiga + docs | Codex (legal/safety) + Claude (templates UI) | Lane B (shipped) | mixed: backend/docs consent+provenance + direct-app template-render UI (Impeccable/D8) | `amiga-operator-cmo` | real before/after requires written consent + source metadata + approval timestamp; never present generated media as a real home; legal wording frozen before real media | consent/provenance schema review; template render check | proposal, not yet started; Lane B satisfied |
| **S5** video shorts | amiga + vendor | deferred | S4 | deferred — future video-render/embed surface classified when scoped | `amiga-operator-cmo` | same consent/provenance rails as S4 | deferred | after S4 |
| **OpenClaw operator-bot sync** (`amiga-operator-cmo` campaign/local-SEO drafting + approval boundary) | OpenClaw (canonical) | Codex (bot spec) | S1 | none / docs-spec | `amiga-operator-cmo` (primary) | never edit canonical OpenClaw source in its dirty root checkout; sync from clean `origin/main` | bot-spec diff review | after S1 |

### Activation order

**Actual: Lane B (S2a) SHIPPED → Lane C1 (GH-1491) MERGED (`913895c5`, PR #1492) → Lane C2 (GH-1493)
implemented/under exact-head review → S1 (Postiz setup, not started) → S2b (deferred Lane D Postiz, blocked
on S1) → OpenClaw sync.** S3 activation: after Lane B (shipped); GH-1441 satisfied (shipped).
Public-page refreshes: after parent; GH-1441 satisfied. S4: after Lane B (shipped). S5: after S4.
GH-1441 gates only the pricing-dependent children (S3, public-page), not this parent, S1, Lane B,
S2b, or OpenClaw sync.

## 11. Bot impact

- **`amiga-operator-cmo`**: IMPACTED as workflow knowledge. S1 voice guide, Lane B draft generation
  (shipped), Lane C1 review authority (merged), Lane C2 review UI (implemented, under exact-head review), S2b Lane D
  delivery, S4 media briefs, and OpenClaw sync touch internal drafting/approval territory. C1 adds no
  bot tool or autonomous approval path; the bot must not imply that approval schedules or publishes.
  GH-1515 adds server authority only (not a bot tool); GH-1516 ships the P4 UI on that authority. The
  bot must describe it truthfully and owns no generation tool itself: **Generate drafts** is offered
  only when the server reports `safeToStart=true`; **Recover last run** only when the session holds the
  exact retained key for a `provider_outcome_unknown` operation; there is no automatic recovery; and
  neither control schedules, delivers, publishes, or calls Postiz. The bot must not imply it can
  generate, nor advise waiting for an automatic recovery, nor direct an operator to a control the
  server has not offered.
- **`amiga-concierge`**: pointer-only for C1/C2. Public Service Notes behavior is unchanged and
  private review state must not be exposed. It becomes directly impacted only if later
  public-page/pricing/promo work changes customer-facing facts.

Never edit canonical OpenClaw source in its dirty root checkout. Sync from clean `origin/main`.
