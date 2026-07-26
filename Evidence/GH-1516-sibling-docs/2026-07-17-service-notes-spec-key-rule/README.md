# GH-1516 sibling-docs fix — service_notes_system_spec.md key-rotation rule

Codex cold-review (app head 70751cc6) flagged a P1 docs-sync blocker: the spec still encoded the
OLD unsafe key rule (mint a fresh key on a DIFFERENT current op with safeToStart:true). Exact-head
code + test now PRESERVE the retained key in that case.

## Scope
- File: marketing/service_notes_system_spec.md (lines ~165-175, "generation idempotency-key lifecycle")
- Docs-only. No app code touched. OpenClaw root NOT touched.

## Hashes (sha256)
- BEFORE: d78982441bff6b4a29975bffee944978bcfb4d2f30e599116858d838ace2d5e0
- AFTER:  a704159933f477942996c7803827854de298d95ab9acd4c6a005604953e4ee70
(BEFORE reconstructed by reversing the single paragraph edit; hash verified == Codex's cited d789…)

## What changed
OLD (unsafe): "a fresh key is minted once authority reports that exact operation terminally settled
(succeeded|failed|blocked) OR reports a DIFFERENT current operation with safeToStart:true (superseded)…
Only a null operation preserves an older ambiguous key."
NEW (fail-closed): a fresh key is minted ONLY when authority reports the retained operation's OWN id
terminally settled. A different reported op does NOT prove the retained op settled (after an ambiguous
transport the client does not reload authority, so the reported op may be the stale pre-submit one, and
minting fresh could drop a live paid run or buy a second). A different reported op, a null op, and any
non-terminal matching status ALL preserve the retained key; only a matching terminal settlement rotates.

## Traceability to exact-head code
- src/routes/app/-service-notes-social-review-actions.ts:377-388 (mayReuseRetainedGenerationKey)
- src/routes/app/-service-notes-social-generation-actions.test.ts:176-184 (REUSES for a DIFFERENT reported op)

## Sibling sweep
- marketing/social_content_program.md: only states the SAFE rule (Recover only with the exact retained
  key for provider_outcome_unknown; no automatic recovery). No different-op mint-fresh wording. No change.
- No other marketing/*.md file restates the key-rotation rule (grep swept: "different current operation",
  "fresh key is minted", "retained key", "terminally settled").

## Files
- before_service_notes_system_spec.md / after_service_notes_system_spec.md / scoped.diff
