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
 * - **Thin over the stable seams.** The SDK is pre-1.0 (0.4.1). The recorder
 *   path uses `defaultExecutionOptions`, events, settings, logging, and one
 *   child. GH-805's second capability uses only the adjacent documented plugin
 *   database, CLI, lifecycle-event, thread-list, and thread-send surfaces.
 *
 * - **Cannot block or veto.** Plugin event handlers are fire-and-forget with
 *   no veto hook (GH-630 probe), and one stalling plugin stalls every project on
 *   the server. So:
 *     * `defaultExecutionOptions` is a loopback RPC; `await`-ing it yields the
 *       event loop (cooperative), it never blocks it.
 *     * the triple write is delegated to a child with `unref()` — its handler never
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
import { createHash, randomUUID } from "node:crypto";
import path from "node:path";
import type { BbPluginApi } from "@bb/plugin-sdk";

const SCRIPT_REL = path.join("bin", "record_executed_triples.py");
const ROLE_RESOLVER_REL = path.join("bin", "resolve_role_wake.py");
const WAKE_POINTER = "event; inspect canonical state";
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

type Db = ReturnType<BbPluginApi["storage"]["database"]>;
type RoleTarget = { project_id: string; thread_id: string };
type WakeResult = "accepted" | "ambiguous" | "coalesced" | "confirmed-failure" | "retrying";
type ScheduleRetry = (target: RoleTarget, reservation: string) => void;
const RETRY_DELAY_MS = 1_000;

export const WAKE_SCHEMA = `CREATE TABLE IF NOT EXISTS role_wake_dedupe (
    project_id TEXT NOT NULL,
    role_thread_id TEXT NOT NULL,
    family TEXT NOT NULL,
    semantic_key TEXT NOT NULL,
    pending INTEGER NOT NULL CHECK (pending IN (0, 1)),
    reservation TEXT,
    PRIMARY KEY (project_id, role_thread_id)
  ) STRICT`;
const WAKE_MIGRATIONS = [WAKE_SCHEMA];

export default function plugin(bb: BbPluginApi): void {
  const db = bb.storage.database();
  bb.storage.migrate(db, WAKE_MIGRATIONS);
  const retryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  let loadTimer: ReturnType<typeof setTimeout> | undefined;
  const scheduleRetry: ScheduleRetry = (target, reservation) => {
    if (retryTimers.has(reservation)) return;
    retryTimers.set(reservation, setTimeout(() => {
      retryTimers.delete(reservation);
      void retryWake(bb, db, target, reservation, scheduleRetry).catch((failure) => {
        bb.log.warn(`exec-tracking: silent wake retry refused: ${describe(failure)}`);
      });
    }, RETRY_DELAY_MS));
  };
  bb.onDispose(() => {
    if (loadTimer) clearTimeout(loadTimer);
    for (const timer of retryTimers.values()) clearTimeout(timer);
    retryTimers.clear();
  });
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

  for (const event of ["thread.failed", "thread.archived", "thread.deleted"] as const) {
    bb.events.on(event, ({ thread, ...payload }) => {
      const error = "error" in payload ? payload.error : null;
      void wakeForThread(bb, settings, db, event, thread, error, scheduleRetry).catch((failure) => {
        bb.log.warn(`exec-tracking: ${event} wake refused for thread ${thread.id}: ${describe(failure)}`);
      });
    });
  }
  bb.events.on("thread.idle", ({ thread }) => {
    void rearmForIdle(bb, settings, db, thread).catch((failure) => {
      bb.log.warn(`exec-tracking: idle re-arm refused for thread ${thread.id}: ${describe(failure)}`);
    });
  });

  bb.cli.register({
    name: "silent-wake",
    summary: "Queue one silent orchestrator pointer from a residual watcher",
    commands: [{
      name: "emit",
      summary: "Emit a pr-artifacts or heartbeat semantic wake",
      usage: "bb silent-wake emit --project <id> --producer <pr-artifacts|heartbeat> --semantic <sha256>",
    }],
    run: (argv) => runWakeCli(bb, settings, db, argv, scheduleRetry),
  });

  // The SDK is bind-gated during factory evaluation. One next-turn reconcile
  // covers abnormal threads that survived a daemon restart; it is not a poll.
  loadTimer = setTimeout(() => {
    resumeRetryableWakes(db, scheduleRetry);
    void reconcileAbnormalThreads(bb, settings, db, scheduleRetry).catch((failure) => {
      bb.log.warn(`exec-tracking: load reconcile refused: ${describe(failure)}`);
    });
  }, 0);
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

async function wakeForThread(
  bb: BbPluginApi,
  settings: { get(): Promise<Settings> },
  db: Db,
  event: string,
  thread: { id: string; projectId: string; updatedAt?: number; archivedAt?: number | null; deletedAt?: number | null },
  error: string | null,
  scheduleRetry: ScheduleRetry,
): Promise<WakeResult> {
  const semantic = digest([
    event,
    thread.id,
    thread.updatedAt ?? null,
    thread.archivedAt ?? null,
    thread.deletedAt ?? null,
    error,
  ]);
  return requestWake(
    bb,
    settings,
    db,
    { threadProject: thread.projectId },
    `worker:${thread.id}`,
    semantic,
    scheduleRetry,
  );
}

async function requestWake(
  bb: BbPluginApi,
  settings: { get(): Promise<Settings> },
  db: Db,
  scope: { threadProject: string } | { project: string },
  family: string,
  semantic: string,
  scheduleRetry: ScheduleRetry,
): Promise<WakeResult> {
  const target = await resolveRole(settings, scope);
  return deliverWake(bb, db, target, family, semantic, scheduleRetry);
}

export async function deliverWake(
  bb: BbPluginApi,
  db: Db,
  target: RoleTarget,
  family: string,
  semantic: string,
  scheduleRetry: ScheduleRetry,
): Promise<WakeResult> {
  const reservation = randomUUID();
  const claimed = db.prepare(`
    INSERT INTO role_wake_dedupe
      (project_id, role_thread_id, family, semantic_key, pending, reservation)
    VALUES (?, ?, ?, ?, 1, ?)
    ON CONFLICT(project_id, role_thread_id) DO UPDATE SET
      family = excluded.family,
      semantic_key = excluded.semantic_key,
      pending = CASE WHEN role_wake_dedupe.pending = 0 THEN 1 ELSE role_wake_dedupe.pending END,
      reservation = CASE
        WHEN role_wake_dedupe.pending = 0 THEN excluded.reservation
        ELSE role_wake_dedupe.reservation
      END
    WHERE role_wake_dedupe.pending = 0
       OR role_wake_dedupe.family <> excluded.family
       OR role_wake_dedupe.semantic_key <> excluded.semantic_key
    RETURNING reservation
  `).get(target.project_id, target.thread_id, family, semantic, reservation) as
    | { reservation: string | null }
    | undefined;
  if (claimed?.reservation !== reservation) return "coalesced";

  return sendReservedWake(
    bb, db, target, reservation, family, semantic, scheduleRetry,
  );
}

async function sendReservedWake(
  bb: BbPluginApi,
  db: Db,
  target: RoleTarget,
  reservation: string,
  family: string,
  semantic: string,
  scheduleRetry: ScheduleRetry,
): Promise<WakeResult> {

  const settleConfirmedFailure = db.prepare(`
        UPDATE role_wake_dedupe
        SET
          pending = CASE WHEN family = ? AND semantic_key = ? THEN 0 ELSE 1 END,
          reservation = CASE
            WHEN family = ? AND semantic_key = ? THEN NULL
            ELSE reservation
          END
        WHERE project_id = ? AND role_thread_id = ? AND reservation = ?
        RETURNING family, semantic_key, pending
      `);
  let attemptedFamily = family;
  let attemptedSemantic = semantic;
  while (true) {
    try {
      await bb.sdk.threads.send({
        threadId: target.thread_id,
        input: [{
          type: "text",
          text: WAKE_POINTER,
          mentions: [],
          visibility: "agent-only",
        }],
        mode: "queue-if-active",
      });
      return "accepted";
    } catch (failure) {
      if (isRetryableFailure(failure)) {
        const retryReservation = `retry:${randomUUID()}`;
        const retained = db.prepare(`
          UPDATE role_wake_dedupe
          SET reservation = ?
          WHERE project_id = ? AND role_thread_id = ?
            AND pending = 1 AND reservation = ?
          RETURNING reservation
        `).get(
          retryReservation,
          target.project_id,
          target.thread_id,
          reservation,
        ) as { reservation: string } | undefined;
        if (!retained) return "coalesced";
        scheduleRetry(target, retryReservation);
        bb.log.warn(
          `exec-tracking: silent wake retryable failure for project ${target.project_id} `
          + `role ${target.thread_id} (${failureStatus(failure)}); retry scheduled`,
        );
        return "retrying";
      }
      if (!isConfirmedFailure(failure)) {
        // Timeout, disconnect, and 5xx can happen after server commit. Retaining
        // the atomic reservation is the only retry-safe outcome.
        bb.log.warn(
          `exec-tracking: silent wake acceptance ambiguous for project ${target.project_id} `
          + `role ${target.thread_id}; retry suppressed`,
        );
        return "ambiguous";
      }
      const latest = settleConfirmedFailure.get(
        attemptedFamily,
        attemptedSemantic,
        attemptedFamily,
        attemptedSemantic,
        target.project_id,
        target.thread_id,
        reservation,
      ) as { family: string; semantic_key: string; pending: number } | undefined;
      bb.log.warn(
        `exec-tracking: silent wake confirmed failed for project ${target.project_id} `
        + `role ${target.thread_id} (${failureStatus(failure)})`,
      );
      if (!latest || latest.pending === 0) return "confirmed-failure";
      // A different semantic event coalesced under this reservation while the
      // failed send was in flight. Hand the one claimant to that latest event.
      attemptedFamily = latest.family;
      attemptedSemantic = latest.semantic_key;
    }
  }
}

export async function retryWake(
  bb: BbPluginApi,
  db: Db,
  target: RoleTarget,
  retryReservation: string,
  scheduleRetry: ScheduleRetry,
): Promise<WakeResult> {
  if (!retryReservation.startsWith("retry:")) return "coalesced";
  const reservation = randomUUID();
  const claimed = db.prepare(`
    UPDATE role_wake_dedupe
    SET reservation = ?
    WHERE project_id = ? AND role_thread_id = ?
      AND pending = 1 AND reservation = ?
    RETURNING family, semantic_key
  `).get(
    reservation,
    target.project_id,
    target.thread_id,
    retryReservation,
  ) as { family: string; semantic_key: string } | undefined;
  if (!claimed) return "coalesced";
  return sendReservedWake(
    bb,
    db,
    target,
    reservation,
    claimed.family,
    claimed.semantic_key,
    scheduleRetry,
  );
}

export function resumeRetryableWakes(db: Db, scheduleRetry: ScheduleRetry): void {
  const rows = db.prepare(`
    SELECT project_id, role_thread_id, reservation
    FROM role_wake_dedupe
    WHERE pending = 1 AND reservation LIKE 'retry:%'
  `).all() as Array<{
    project_id: string;
    role_thread_id: string;
    reservation: string;
  }>;
  for (const row of rows) {
    scheduleRetry(
      { project_id: row.project_id, thread_id: row.role_thread_id },
      row.reservation,
    );
  }
}

export function rearmWake(
  db: Db,
  target: RoleTarget,
  threadId: string,
): void {
  const family = `worker:${threadId}`;
  db.prepare(`
    UPDATE role_wake_dedupe
    SET pending = 0, reservation = NULL
    WHERE project_id = ? AND role_thread_id = ?
      AND (? = role_thread_id OR family = ?)
  `).run(target.project_id, target.thread_id, threadId, family);
}

async function rearmForIdle(
  bb: BbPluginApi,
  settings: { get(): Promise<Settings> },
  db: Db,
  thread: { id: string; projectId: string },
): Promise<void> {
  const target = await resolveRole(settings, { threadProject: thread.projectId });
  rearmWake(db, target, thread.id);
}

async function runWakeCli(
  bb: BbPluginApi,
  settings: { get(): Promise<Settings> },
  db: Db,
  argv: string[],
  scheduleRetry: ScheduleRetry,
): Promise<{ exitCode: number; stdout?: string; stderr?: string }> {
  try {
    const parsed = parseWakeCli(argv);
    const result = await requestWake(
      bb,
      settings,
      db,
      { project: parsed.project },
      parsed.producer,
      parsed.semantic,
      scheduleRetry,
    );
    if (result === "confirmed-failure") {
      return { exitCode: 1, stderr: "silent wake confirmed failed; reservation released\n" };
    }
    return { exitCode: 0, stdout: `${result}\n` };
  } catch (failure) {
    return { exitCode: 1, stderr: `silent wake refused: ${describe(failure)}\n` };
  }
}

function parseWakeCli(argv: string[]): { project: string; producer: string; semantic: string } {
  if (argv[0] !== "emit" || argv.length !== 7) throw new Error("usage: silent-wake emit --project <id> --producer <pr-artifacts|heartbeat> --semantic <sha256>");
  const values = new Map<string, string>();
  for (let index = 1; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag.startsWith("--") || !value || values.has(flag)) throw new Error("invalid or repeated silent-wake argument");
    values.set(flag, value);
  }
  const project = values.get("--project");
  const producer = values.get("--producer");
  const semantic = values.get("--semantic");
  if (!project || project !== project.trim()) throw new Error("--project must be non-empty unpadded text");
  if (producer !== "pr-artifacts" && producer !== "heartbeat") throw new Error("--producer must be pr-artifacts or heartbeat");
  if (!semantic || !/^[0-9a-f]{64}$/.test(semantic)) throw new Error("--semantic must be a lowercase SHA-256 digest");
  return { project, producer, semantic };
}

async function reconcileAbnormalThreads(
  bb: BbPluginApi,
  settings: { get(): Promise<Settings> },
  db: Db,
  scheduleRetry: ScheduleRetry,
): Promise<void> {
  const limit = 200;
  for (let offset = 0; ; offset += limit) {
    const threads = await bb.sdk.threads.list({
      archived: false,
      includeHidden: true,
      limit,
      offset,
    });
    for (const thread of threads) {
      if (thread.status === "error" || thread.status === "stopping") {
        await wakeForThread(bb, settings, db, "load.reconcile", thread, null, scheduleRetry);
      }
    }
    if (threads.length < limit) return;
  }
}

async function resolveRole(
  settings: { get(): Promise<Settings> },
  scope: { threadProject: string } | { project: string },
): Promise<RoleTarget> {
  const cfg = await settings.get();
  if (!cfg.checkoutPath || !cfg.pythonPath) {
    throw new Error("checkoutPath and pythonPath must be configured");
  }
  const script = path.join(cfg.checkoutPath, ROLE_RESOLVER_REL);
  const args = [
    script,
    ...("threadProject" in scope
      ? ["--thread-project", scope.threadProject]
      : ["--project", scope.project]),
  ];
  const output = await boundedChild(cfg.pythonPath, args, cfg.checkoutPath);
  let parsed: unknown;
  try {
    parsed = JSON.parse(output);
  } catch {
    throw new Error("role resolver returned malformed JSON");
  }
  if (
    !parsed
    || typeof parsed !== "object"
    || typeof (parsed as RoleTarget).project_id !== "string"
    || typeof (parsed as RoleTarget).thread_id !== "string"
    || !(parsed as RoleTarget).project_id
    || !(parsed as RoleTarget).thread_id
  ) {
    throw new Error("role resolver returned an invalid target");
  }
  return parsed as RoleTarget;
}

function boundedChild(executable: string, args: string[], cwd: string): Promise<string> {
  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    const child = spawn(executable, args, { cwd, stdio: ["ignore", "pipe", "pipe"], env: process.env });
    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf-8");
      if (stdout.length > STDOUT_CAP) child.kill();
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf-8");
      if (stderr.length > STDERR_CAP) child.kill();
    });
    child.once("error", reject);
    child.once("close", (code) => {
      if (stdout.length > STDOUT_CAP || stderr.length > STDERR_CAP) {
        reject(new Error("role resolver output exceeded its bound"));
      } else if (code !== 0) {
        reject(new Error(stderr.trim() || `role resolver exited ${code}`));
      } else {
        resolve(stdout.trim());
      }
    });
  });
}

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function isConfirmedFailure(failure: unknown): boolean {
  const status = failureStatusNumber(failure);
  return status !== null && status >= 400 && status < 500;
}

function isRetryableFailure(failure: unknown): boolean {
  const status = failureStatusNumber(failure);
  return status === 408 || status === 425 || status === 429;
}

function failureStatusNumber(failure: unknown): number | null {
  if (!failure || typeof failure !== "object" || !("status" in failure)) return null;
  const status = (failure as { status?: unknown }).status;
  return typeof status === "number" && Number.isInteger(status) ? status : null;
}

function failureStatus(failure: unknown): string {
  return failureStatusNumber(failure)?.toString() ?? "unknown";
}

function describe(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
