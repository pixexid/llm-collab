/**
 * exec-tracking plugin — first capability: record the thread's EXECUTED
 * (provider, model, reasoning_level) triple — which triple actually ran.
 *
 * GH-710 / GH-630 first scope / GH-617. On `thread.created` this reads the
 * thread's resolved execution options IN-PROCESS via
 * `bb.sdk.threads.defaultExecutionOptions`, snapshots the resolved values AND
 * their `source` to primitives, and hands them to a child running our own
 * `bin/record_executed_triples.py` (the bounded write authority). That script
 * records a durable, project-scoped row ONLY when `source ===
 * client/turn/requested`; every other source is refused observably.
 *
 * **Executed-triple evidence, one named source (GH-710).**
 * The artifact is executed-triple evidence — which triple actually ran. That is
 * the invariant the frozen-triple rule needs audited; "creation-time defaults"
 * was a proxy for it, chosen before GH-706 established the proxy is
 * unobservable on this SDK. The accepted source is `client/turn/requested`
 * because the repository already decided that source is the proof:
 * `llm_collab/bb_client.py:21-24` names its `execution` block the authoritative
 * record of the profile bb actually ran, and `BbClient` validates against it.
 * This recorder aligns with that authority rather than dissenting from it.
 *
 * The gate stays a gate — it admits ONE named source:
 *   - `client/turn/requested` — recorded, `evidence: executed`.
 *   - `client/thread/start` — refused with its OWN distinct reason
 *     (`ignored thread_start_not_executed`): its payload carries no execution
 *     options on this SDK (GH-706), and if it ever does, that marker is the
 *     tripwire rather than a silent drop.
 *   - anything else (`client/turn/start`, absent, unrecognised) — refused as
 *     `ignored out_of_contract`, source named.
 * What was measured at `thread.created` (GH-706): `defaultExecutionOptions`
 * reported `client/turn/requested` in 8 of 8 probes, varying provider (`pi` and
 * `claude-code`), model, visibility, parentage, an idle fork, and a spawn with no
 * explicit provider or model. None reported `client/thread/start`. An idle fork
 * emitted `client/turn/requested` BEFORE `client/thread/start`, so there is no
 * pre-turn window to sample.
 *
 * The SDK documents no ordering: `bb-plugin-sdk.d.ts` declares the `source` enum
 * and says nothing about when each value is produced. So treat the above as
 * measurement, not as a guarantee — do not write code that assumes it holds.
 *
 * If a `client/thread/start` result does occur, it refuses with its own distinct
 * `thread_start_not_executed` reason and is logged, so it is visible rather than
 * silent. There is no retry: this slice records the authoritative source when it
 * is observable and refuses the rest observably. That is a known limitation of
 * the slice, not an oversight.
 *
 * Why this shape:
 *
 * - **Resolved values, never a mutable reference.** GH-617's defect is that a
 *   stored preset name can be edited to retroactively change what a historical
 *   dispatch resolves to. We read the resolved options and copy their fields into
 *   plain strings before anything is persisted; no object reference survives.
 *
 * - **Thin over the stable seams.** The SDK is pre-1.0 (0.4.1). This module
 *   touches only `bb.sdk.threads.defaultExecutionOptions`, `bb.events`,
 *   `bb.settings`, `bb.log`, and `node:child_process` — the documented surfaces,
 *   never internals.
 *
 * - **Cannot block or veto.** `thread.created` handlers are fire-and-forget with
 *   no veto hook (GH-630 probe), and one stalling plugin stalls every project on
 *   the server. So:
 *     * `defaultExecutionOptions` is a loopback RPC; `await`-ing it yields the
 *       event loop (cooperative), it never blocks it.
 *     * the write is delegated to a child with `unref()` — the handler never
 *       waits on it, and no synchronous filesystem I/O runs in-process.
 *     * the handler is `void`-ed; any rejection is caught and logged, never
 *       propagated to the emitter.
 *
 * - **Records, never gates.** Enforcement stays at our CLI call sites; this only
 *   records the executed triple. An options object that cannot be resolved
 *   is recorded as an explicit `unresolved` row so an absent row and a failed
 *   resolution are distinguishable.
 *
 * - **No silent failure.** Spawn errors, nonzero exits, and ignored/scope-
 *   mismatched/out-of-contract events are logged ASYNCHRONOUSLY via the child's
 *   `error`/`close` events — the event-loop constraint still holds. A recorder
 *   failure must be distinguishable from an event that never happened (GH-630
 *   review, finding 5).
 */
import { spawn } from "node:child_process";
import path from "node:path";
import type { BbPluginApi } from "@bb/plugin-sdk";

const SCRIPT_REL = path.join("bin", "record_executed_triples.py");
// Both streams are piped+drained with a capped buffer so a verbose child cannot
// fill the pipe and block. STDOUT_CAP bounds the captured stdout used for info
// markers; STDERR_CAP bounds the captured stderr used for failure logging.
const STDOUT_CAP = 4096;
const STDERR_CAP = 4096;
// Recorder stdout lines that are observable-but-not-failure: a correctly-ignored
// unknown native project or a correctly-conflicted re-resolution. Matched with
// startsWith, so each marker includes its trailing space.
const INFO_MARKERS = ["ignored ", "conflict "] as readonly string[];

interface Settings {
  checkoutPath: string | undefined;
  pythonPath: string | undefined;
}

export default function plugin(bb: BbPluginApi): void {
  const settings = bb.settings.define({
    checkoutPath: {
      type: "string",
      label: "Absolute path to the llm-collab checkout (where bin/, projects.json, and collab.config.json live)",
    },
    pythonPath: {
      type: "string",
      label: "Absolute path to python3.11 (server PATH is narrow; bare python3.11 gave ENOENT)",
    },
  });

  bb.events.on("thread.created", ({ thread }) => {
    // Fire-and-forget. The promise never reaches the emitter: onCreated catches
    // its own failures and logs them, so a resolution/write error is contained.
    void onCreated(bb, settings, thread).catch((error) => {
      bb.log.warn(`exec-tracking: thread.created handler failed: ${describe(error)}`);
    });
  });
}

async function onCreated(
  bb: BbPluginApi,
  settings: { get(): Promise<Settings> },
  thread: { id?: string; providerId?: string | null; projectId?: string } | undefined,
): Promise<void> {
  const cfg = await settings.get();
  if (!cfg.checkoutPath || !cfg.pythonPath) {
    // Needs operator configuration (install step). Skip loudly rather than guess a
    // checkout or interpreter — recording nothing is honest; recording to a guessed
    // path is silent corruption.
    bb.log.warn(
      "exec-tracking: checkoutPath and pythonPath must be set before triples are recorded",
    );
    return;
  }

  const threadId = thread?.id;
  const threadProject = thread?.projectId;
  if (!threadId || !threadProject) return;
  const provider = thread?.providerId ?? null;

  // Read the thread's resolved execution options in-process. Snapshot to
  // primitives immediately; do not hold the resolved object. Pass the SDK-reported
  // `source` through unchanged; the recorder records ONLY client/turn/requested
  // (the executed-evidence source bb_client.py:21-24 names authoritative, GH-710),
  // refuses client/thread/start with its own distinct reason, and refuses any
  // other source as out of contract — so a non-executed snapshot never enters an
  // artifact documented as executed-triple evidence.
  let resolved: {
    model?: string | null;
    reasoningLevel?: string | null;
    source?: string | null;
  } | null;
  try {
    resolved = await bb.sdk.threads.defaultExecutionOptions({ threadId });
  } catch (error) {
    spawnRecorder(bb, cfg, threadId, threadProject, provider, [
      "--unresolved",
      "profile_resolution_error",
      "--failure-detail",
      describe(error),
    ]);
    return;
  }

  const model = resolved?.model ?? null;
  const reasoningLevel = resolved?.reasoningLevel ?? null;
  const source = resolved?.source ?? null;

  if (resolved && model && reasoningLevel && source) {
    spawnRecorder(bb, cfg, threadId, threadProject, provider, [
      "--model",
      model,
      "--reasoning-level",
      reasoningLevel,
      "--source",
      source,
    ]);
  } else if (resolved === null) {
    // null before/at creation when the server cannot form concrete defaults for
    // the current policy/provider combination — a real, distinguishable state.
    spawnRecorder(bb, cfg, threadId, threadProject, provider, [
      "--unresolved",
      "profile_not_resolved",
    ]);
  } else {
    spawnRecorder(bb, cfg, threadId, threadProject, provider, [
      "--unresolved",
      "profile_incomplete",
    ]);
  }
}

function spawnRecorder(
  bb: BbPluginApi,
  cfg: Settings,
  threadId: string,
  threadProject: string,
  provider: string | null,
  tripleArgs: string[],
): void {
  const script = path.join(cfg.checkoutPath as string, SCRIPT_REL);
  const argv = [
    cfg.pythonPath as string,
    script,
    "--thread-id",
    threadId,
    "--thread-project",
    threadProject,
    ...(provider ? ["--provider", provider] : []),
    ...tripleArgs,
  ];
  let stderr = "";
  let stdout = "";
  try {
    // unref(): the handler returns immediately and never waits on the child. The
    // bounded, durable write runs independently; killing the child mid-write
    // cannot corrupt state (the writer is temp+rename atomic).
    //
    // Both streams are piped and drained so a verbose process cannot fill the
    // pipe buffer and block. stdout is captured so an informational marker — a
    // unknown native project that was correctly ignored (S1), or a conflicting
    // re-resolution that was correctly rejected (N1) — is logged rather than
    // silently dropped, and a recorder failure (nonzero) is logged with stderr.
    // F5 / N3.
    const child = spawn(argv[0], argv.slice(1), {
      cwd: cfg.checkoutPath as string,
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });
    child.stdout?.on("data", (chunk: Buffer) => {
      if (stdout.length < STDOUT_CAP) stdout += chunk.toString("utf-8");
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      if (stderr.length < STDERR_CAP) stderr += chunk.toString("utf-8");
    });
    child.on("error", (error) => {
      bb.log.warn(`exec-tracking: recorder spawn failed for thread ${threadId}: ${describe(error)}`);
    });
    child.on("close", (code) => {
      // Async, on the event loop — never blocks.
      if (code !== 0) {
        // A refusal or failure (registry, scope config, budget, corruption, bad
        // path): warn with the stderr the recorder produced.
        bb.log.warn(`exec-tracking: recorder exited ${code} for thread ${threadId}: ${stderr.trim()}`);
        return;
      }
      const line = stdout.trim();
      if (INFO_MARKERS.some((marker) => line.startsWith(marker))) {
        // Observable but not a failure: a correctly-ignored or correctly-conflicted
        // event. Logged so it is never indistinguishable from an event unseen (N3).
        bb.log.info(`exec-tracking: ${line}`);
      }
      // A quiet "recorded"/"noop" line needs no log — the file row is the record.
    });
    child.unref();
  } catch (error) {
    // Synchronous spawn failure (bad interpreter path, etc.). Logged, not swallowed.
    bb.log.warn(`exec-tracking: recorder could not spawn for thread ${threadId}: ${describe(error)}`);
  }
}

function describe(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
