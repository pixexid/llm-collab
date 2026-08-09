/**
 * exec-tracking plugin — first capability: record the executed triple.
 *
 * GH-630 first scope / GH-617. On `thread.created` this resolves the executed
 * (provider, model, reasoning_level) profile IN-PROCESS via the only surface that
 * exposes it — `bb.sdk.threads.defaultExecutionOptions` — snapshots the resolved
 * values to primitives, and hands them to a child running our own
 * `bin/record_executed_triple.py` (the bounded write authority). That script
 * appends a durable, project-scoped row.
 *
 * Why this shape:
 *
 * - **Resolved values, never a mutable reference.** GH-617's defect is that a
 *   stored preset name can be edited to retroactively change what a historical
 *   dispatch resolves to. We read the resolved triple and copy its fields into
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
 *   records what ran. A profile that cannot be resolved is recorded as an
 *   explicit `unresolved` row so an absent row and a failed resolution are
 *   distinguishable — never silently omitted.
 *
 * - **No silent failure.** Spawn errors, nonzero exits, and ignored/scope-
 *   mismatched events are logged ASYNCHRONOUSLY via the child's `error`/`close`
 *   events — the event-loop constraint still holds. A recorder failure must be
 *   distinguishable from an event that never happened (GH-630 review, finding 5).
 */
import { spawn } from "node:child_process";
import path from "node:path";
import type { BbPluginApi } from "@bb/plugin-sdk";

const SCRIPT_REL = path.join("bin", "record_executed_triple.py");
const STDERR_CAP = 4096;

interface Settings {
  checkoutPath: string | undefined;
  projectId: string;
  pythonPath: string | undefined;
}

export default function plugin(bb: BbPluginApi): void {
  const settings = bb.settings.define({
    checkoutPath: {
      type: "string",
      label: "Absolute path to the llm-collab checkout (where bin/, projects.json, and collab.config.json live)",
    },
    projectId: {
      type: "string",
      label: "Registered llm-collab project_id this instance records (must exist in projects.json)",
      default: "llm-collab",
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

  // Resolve the executed profile in-process — the only place it is available.
  // Snapshot to primitives immediately; do not hold the resolved object.
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
    "--project",
    cfg.projectId,
    "--thread-id",
    threadId,
    "--thread-project",
    threadProject,
    ...(provider ? ["--provider", provider] : []),
    ...tripleArgs,
  ];
  let stderr = "";
  try {
    // unref(): the handler returns immediately and never waits on the child. The
    // bounded, durable write runs independently; killing the child mid-write
    // cannot corrupt state (the writer is temp+rename atomic).
    //
    // stderr is piped (not ignored) and drained so a verbose failure cannot fill
    // the pipe buffer and block the child. stdout is captured to distinguish an
    // observable "ignored" event from a quiet "recorded" one. F5.
    const child = spawn(argv[0], argv.slice(1), {
      cwd: cfg.checkoutPath as string,
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      if (stderr.length < STDERR_CAP) stderr += chunk.toString("utf-8");
    });
    child.on("error", (error) => {
      bb.log.warn(`exec-tracking: recorder spawn failed for thread ${threadId}: ${describe(error)}`);
    });
    child.on("close", (code) => {
      // Async, on the event loop — never blocks. Nonzero => a refusal or failure
      // (registry, scope config, budget, corruption, bad path): warn with stderr.
      // Zero + "ignored" => a thread for another project (expected, but observable
      // so a dropped event is never confused with one that was never seen).
      if (code !== 0) {
        bb.log.warn(`exec-tracking: recorder exited ${code} for thread ${threadId}: ${stderr.trim()}`);
      }
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
