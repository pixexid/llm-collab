# Amiga Ordered Issue Queue

> Generated from `issue-queue.json`. Edit the JSON, then run `python3 bin/project_issue_queue.py sync-markdown --project amiga`.

- Last updated: `2026-04-12T16:51:07+00:00`
- Source issue: `GH-257`
- Source task: `TASK-A3AEFF`
- Next ready lane: `GH-233` / `TASK-48C9F9` / `cdx2`

## Recently Completed

- `GH-219` / `TASK-44A521` / `codex` / `done`
- `GH-220` / `TASK-390E21` / `claude` / `done`
- `GH-234` / `TASK-36FEB8` / `gemini` / `done`
- `GH-241` / `TASK-136AF6` / `cdx2` / `done`
- `GH-232` / `TASK-B0D14F` / `cdx2` / `done`

## Remaining Queue

| Order | Issue | Task | Owner | Task Status | Queue State | Tier | Depends On | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | GH-233 | TASK-48C9F9 | cdx2 | pending | ready | 2 | - | Current next lane after GH-232. |
| 2 | GH-235 | TASK-C44C3E | cdx2 | pending | queued | 2 | - | Next cdx2 scheduling lane after GH-233. |
| 3 | GH-230 | TASK-5283BD | cdx2 | pending | queued | 3 | - | Availability UX/logic lane after the remaining tier-2 cdx2 slices. |
| 4 | GH-236 | TASK-316440 | claude | pending | queued | 3 | - | Claude-owned follow-up after the earlier operations/tasks work. |
| 5 | GH-239 | TASK-C710AD | gemini | pending | queued | 3 | - | Gemini-owned customer preference slice. |
| 6 | GH-237 | TASK-92C17E | cdx2 | pending | queued | 4 | - | Later cdx2 duration/settings lane. |
| 7 | GH-231 | TASK-9B33E0 | claude | pending | blocked | 4 | TASK-37C311 | Blocked until the referenced Phase 4 lane completes. |
| 8 | GH-221 | TASK-1DF3C7 | gemini | pending | queued | 4 | - | Gemini-owned dashboard metrics lane. |
| 9 | GH-226 | TASK-B374E9 | cdx2 | pending | queued | 5 | - | Payments follow-up after the scheduling queue clears. |
| 10 | GH-229 | TASK-B5D56C | cdx2 | pending | queued | 5 | - | Notification delivery lane. |
| 11 | GH-223 | TASK-C78AED | claude | pending | queued | 5 | - | Claude-owned booking confirmation/customer state lane. |
| 12 | GH-227 | TASK-EF110F | gemini | pending | queued | 5 | - | Gemini-owned admin reschedule surface. |
| 13 | GH-238 | TASK-65615C | cdx2 | pending | queued | 5 | - | Latest queued cdx2 pricing/recurrence lane in this plan. |
