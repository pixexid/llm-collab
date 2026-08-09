import { spawn } from "node:child_process";
import path from "node:path";
import type { BbPluginApi } from "@bb/plugin-sdk";

type AgentResult = {
  agent: number;
  threadId: string;
  environmentId: string;
  branchName: string;
  output: string;
  status: "idle";
};

export type FanOutResult = { count: number; agents: AgentResult[] };

type SpawnedThread = { id: string; environmentId: string };

type EnvironmentStatus = {
  outcome: "available" | "unavailable";
  workspaceStatus?: {
    workingTree: { hasUncommittedChanges: boolean };
    checkout:
      | { kind: "branch"; branchName: string; headSha: string | null }
      | { kind: string; branchName?: string | null; headSha?: string | null };
    branch: { currentBranch: string | null };
    mergeBase: { baseRef: string | null } | null;
  };
};

type FanOutSdk = {
  threads: {
    wait: (args: any) => Promise<unknown>;
    output: (args: { threadId: string; signal: AbortSignal }) => Promise<{ output: string | null }>;
    stop: (args: { threadId: string }) => Promise<unknown>;
  };
  environments: {
    status: (args: { environmentId: string; mergeBaseBranch: string; signal: AbortSignal }) => Promise<EnvironmentStatus>;
  };
};

type FanOutOptions = {
  projectId: string;
  count: number;
  prompt: string;
  providerId: string;
  model: string;
  reasoningLevel: string;
  baseSha: string;
  repoTarget?: string;
  permissionMode: "accept-edits" | "auto" | "full";
  deadlineMs?: number;
};

type Settings = { checkoutPath?: string; pythonPath?: string };
type GateSpawn = (agent: number, options: FanOutOptions, signal: AbortSignal) => Promise<SpawnedThread>;
const FAN_OUT_DEADLINE_MS = 30 * 60_000;

class GateSpawnFailure extends Error {
  readonly doNotRetry: boolean;
  readonly nativeThreadId: string | null;

  constructor(message: string, doNotRetry: boolean, nativeThreadId: string | null) {
    super(`${message}${nativeThreadId ? ` native_thread_id=${nativeThreadId}` : ""}`);
    this.name = "GateSpawnFailure";
    this.doNotRetry = doNotRetry;
    this.nativeThreadId = nativeThreadId;
  }
}

class AgentFailure extends Error {
  readonly agent: number;
  readonly reason: string;
  readonly doNotRetry: boolean;
  readonly nativeThreadId: string | null;

  constructor(agent: number, reason: string, doNotRetry = false, nativeThreadId: string | null = null) {
    super(`agent ${agent} failed: ${reason}`);
    this.name = "AgentFailure";
    this.agent = agent;
    this.reason = reason;
    this.doNotRetry = doNotRetry;
    this.nativeThreadId = nativeThreadId;
  }
}

/** One bounded fan-out; failure aborts waits, stops every known sibling, and has no partial result. */
export async function fanOut(
  sdk: FanOutSdk,
  options: FanOutOptions,
  spawnAgent: GateSpawn,
): Promise<FanOutResult> {
  if (!Number.isSafeInteger(options.count) || options.count < 1 || options.count > 32) {
    throw new Error("count must be an integer from 1 through 32");
  }

  const abort = new AbortController();
  const spawned = new Set<string>();
  const deadlineMs = options.deadlineMs ?? FAN_OUT_DEADLINE_MS;
  const deadline = setTimeout(() => abort.abort(), deadlineMs);
  const runs = Array.from({ length: options.count }, (_, index) => runAgent(
    sdk, options, spawnAgent, index + 1, abort.signal, deadlineMs, (threadId) => spawned.add(threadId),
  ));
  try {
    const results = await Promise.all(runs);
    const environmentIds = new Set(results.map((result) => result.environmentId));
    const branches = new Set(results.map((result) => result.branchName));
    if (environmentIds.size !== options.count) throw new Error("isolation assertion failed: environments are not distinct");
    if (branches.size !== options.count) throw new Error("isolation assertion failed: branches are not distinct");
    return { count: results.length, agents: results };
  } catch (error) {
    abort.abort();
    await Promise.allSettled(runs);
    await Promise.allSettled([...spawned].map((threadId) => sdk.threads.stop({ threadId })));
    throw error;
  } finally {
    clearTimeout(deadline);
  }
}

async function runAgent(
  sdk: FanOutSdk,
  options: FanOutOptions,
  spawnAgent: GateSpawn,
  agent: number,
  signal: AbortSignal,
  deadlineMs: number,
  onSpawn: (threadId: string) => void,
): Promise<AgentResult> {
  let thread: SpawnedThread;
  try {
    thread = await spawnAgent(agent, options, signal);
    onSpawn(thread.id);
  } catch (error) {
    if (error instanceof GateSpawnFailure && error.nativeThreadId) onSpawn(error.nativeThreadId);
    if (error instanceof GateSpawnFailure) {
      throw new AgentFailure(agent, `spawn ${error.message}`, error.doNotRetry, error.nativeThreadId);
    }
    throw new AgentFailure(agent, `spawn error: ${describe(error)}`);
  }
  await waitForIdle(sdk, thread.id, agent, signal, deadlineMs);
  const result = await readResult(sdk, thread.id, agent, signal);
  const status = await readStatus(sdk, thread.environmentId, options.baseSha, agent, signal);
  const workspace = status.workspaceStatus;
  const checkout = workspace?.checkout;
  const branchName = checkout?.kind === "branch" ? checkout.branchName : null;
  if (
    status.outcome !== "available" || !workspace ||
    workspace.workingTree.hasUncommittedChanges ||
    checkout?.kind !== "branch" || checkout.headSha !== options.baseSha ||
    workspace.branch.currentBranch !== branchName ||
    workspace.mergeBase?.baseRef !== options.baseSha || !branchName
  ) {
    throw new AgentFailure(agent, "post-turn worktree proof failed (dirty, moved, unavailable, or wrong base)");
  }
  return { agent, threadId: thread.id, environmentId: thread.environmentId, branchName, output: result, status: "idle" };
}

async function readResult(sdk: FanOutSdk, threadId: string, agent: number, signal: AbortSignal): Promise<string> {
  let response: { output: string | null };
  try {
    response = await sdk.threads.output({ threadId, signal });
  } catch (error) {
    throw new AgentFailure(agent, `result lookup error: ${describe(error)}`);
  }
  if (response.output === null || !response.output.trim()) throw new AgentFailure(agent, "result output was absent or empty");
  return response.output;
}

async function readStatus(sdk: FanOutSdk, environmentId: string, baseSha: string, agent: number, signal: AbortSignal): Promise<EnvironmentStatus> {
  const aborted = new Promise<never>((_, reject) => {
    if (signal.aborted) reject(new AgentFailure(agent, "environment status deadline exceeded"));
    signal.addEventListener("abort", () => reject(new AgentFailure(agent, "environment status deadline exceeded")), { once: true });
  });
  try {
    return await Promise.race([
      sdk.environments.status({ environmentId, mergeBaseBranch: baseSha, signal }),
      aborted,
    ]);
  } catch (error) {
    throw error instanceof AgentFailure ? error : new AgentFailure(agent, `environment status error: ${describe(error)}`);
  }
}

async function waitForIdle(sdk: FanOutSdk, threadId: string, agent: number, parentSignal: AbortSignal, deadlineMs: number): Promise<void> {
  const local = new AbortController();
  const abortLocal = () => local.abort();
  parentSignal.addEventListener("abort", abortLocal, { once: true });
  const timeout = new Promise<never>((_, reject) => {
    const timer = setTimeout(() => reject(new AgentFailure(agent, "did not complete")), deadlineMs);
    local.signal.addEventListener("abort", () => clearTimeout(timer), { once: true });
  });
  const idle = sdk.threads.wait({ threadId, status: "idle", timeoutMs: deadlineMs, signal: local.signal })
    .then((result) => { if (!result) throw new AgentFailure(agent, "idle wait returned no result"); })
    .catch((error) => { throw error instanceof AgentFailure ? error : new AgentFailure(agent, `idle wait error: ${describe(error)}`); });
  const failed = sdk.threads.wait({ threadId, event: "thread.failed", timeoutMs: deadlineMs, signal: local.signal })
    .then((result) => { if (!result) throw new AgentFailure(agent, "failure wait returned no result"); throw new AgentFailure(agent, "reported thread.failed"); })
    .catch((error) => { throw error instanceof AgentFailure ? error : new AgentFailure(agent, `failure wait error: ${describe(error)}`); });
  try { await Promise.race([idle, failed, timeout]); } finally {
    parentSignal.removeEventListener("abort", abortLocal);
    local.abort();
  }
}

export default function plugin(bb: BbPluginApi): void {
  const settings = bb.settings.define({
    checkoutPath: { type: "string", label: "Absolute llm-collab checkout path" },
    pythonPath: { type: "string", label: "Absolute python3.11 path" },
  });
  bb.cli.register({
    name: "fan-out",
    summary: "Fan out read-only agents into isolated worktrees; fail closed",
    commands: [{ name: "run", summary: "Run the bounded isolated fan-out", usage: "run --project ID --count N --prompt TEXT --provider ID --model MODEL --reasoning-level LEVEL --base-sha 40_HEX_SHA" }],
    async run(argv) {
      try {
        const options = parseArgs(argv);
        const config = await settings.get();
        if (!config.checkoutPath || !config.pythonPath || !path.isAbsolute(config.checkoutPath) || !path.isAbsolute(config.pythonPath)) {
          throw new Error("checkoutPath and pythonPath must be absolute configured paths");
        }
        const result = await fanOut(bb.sdk, options, (agent, spawnOptions, signal) => gateSpawn(config, agent, spawnOptions, signal));
        return { exitCode: 0, stdout: `${JSON.stringify(result)}\n` };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        bb.log.warn(`fan-out: ${message}`);
        const exitCode = error instanceof AgentFailure && error.doNotRetry ? 2 : 1;
        return { exitCode, stderr: `${message}\n` };
      }
    },
  });
}

function gateSpawn(config: Settings, agent: number, options: FanOutOptions, signal: AbortSignal): Promise<SpawnedThread> {
  const script = path.join(config.checkoutPath!, "bin", "bb_spawn.py");
  const argv = [
    script, "--assignment-kind", "read-only", "--collab-project", options.projectId,
    ...(options.repoTarget ? ["--repo-target", options.repoTarget] : []),
    "--provider", options.providerId, "--model", options.model, "--reasoning-level", options.reasoningLevel,
    "--base-sha", options.baseSha, "--permission-mode", options.permissionMode,
    "--title", `Fan-out agent ${agent}`, "--prompt", options.prompt, "--new-environment", "worktree", "--json",
  ];
  return new Promise((resolve, reject) => {
    const child = spawn(config.pythonPath!, argv, { cwd: config.checkoutPath, stdio: ["ignore", "pipe", "pipe"], signal });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => { if (stdout.length < 65536) stdout += chunk.toString("utf8"); });
    child.stderr.on("data", (chunk: Buffer) => { if (stderr.length < 65536) stderr += chunk.toString("utf8"); });
    child.on("error", (error) => reject(error));
    child.on("close", (code) => {
      if (code !== 0) {
        const detail = stderr.trim() || `bb_spawn.py exited ${code}`;
        reject(parseGateFailure(detail, code ?? -1));
        return;
      }
      try {
        const result = JSON.parse(stdout) as { id?: string; environmentId?: string };
        if (!result.id || !result.environmentId) throw new Error("bb_spawn.py returned no thread/environment");
        resolve({ id: result.id, environmentId: result.environmentId });
      } catch (error) { reject(error); }
    });
  });
}

export function parseGateFailure(detail: string, exitCode: number): GateSpawnFailure {
  const doNotRetry = exitCode === 2 || detail.startsWith("DO NOT RETRY:");
  const match = /^DO NOT RETRY: ([^ ]+)(?: native_thread_id=([^: ]+))?: (.*)$/s.exec(detail);
  const reason = match ? `${match[1]}: ${match[3]}` : detail;
  return new GateSpawnFailure(reason, doNotRetry, match?.[2] ?? null);
}

function describe(error: unknown): string { return error instanceof Error ? error.message : String(error); }

export function parseArgs(argv: string[]): FanOutOptions {
  const args = argv[0] === "run" ? argv.slice(1) : argv;
  const values = new Map<string, string>();
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i]; const value = args[i + 1];
    if (!key?.startsWith("--") || value === undefined || value.startsWith("--")) throw new Error("arguments must be --name value pairs");
    values.set(key.slice(2), value);
  }
  const required = ["project", "count", "prompt", "provider", "model", "reasoning-level", "base-sha"];
  for (const key of required) if (!values.has(key)) throw new Error(`missing --${key}`);
  const count = Number(values.get("count"));
  if (!Number.isSafeInteger(count)) throw new Error("count must be an integer");
  const permissionMode = values.get("permission-mode") ?? "accept-edits";
  if (permissionMode !== "accept-edits" && permissionMode !== "auto" && permissionMode !== "full") throw new Error("invalid permission mode");
  return { projectId: values.get("project")!, count, prompt: values.get("prompt")!, providerId: values.get("provider")!, model: values.get("model")!, reasoningLevel: values.get("reasoning-level")!, baseSha: values.get("base-sha")!, repoTarget: values.get("repo-target"), permissionMode };
}
