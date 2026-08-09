import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";
import { fanOut, parseArgs, parseGateFailure } from "./server.ts";

const baseSha = "0123456789abcdef0123456789abcdef01234567";
const options = {
  projectId: "project", prompt: "Read and report; do not modify files.", providerId: "codex",
  model: "model", reasoningLevel: "low", baseSha, permissionMode: "accept-edits" as const, deadlineMs: 10,
};

function sdkFor(mode: "success" | "error" | "missing" | "empty-output" | "status-hang" = "success") {
  const stopped: string[] = [];
  return {
    stopped,
    threads: {
      async wait(args: { event?: string }) {
        if (args.event === "thread.failed") return new Promise(() => {});
        return { matched: true };
      },
      async output({ threadId }: { threadId: string }) {
        return { output: mode === "empty-output" ? null : `result from ${threadId}` };
      },
      async stop({ threadId }: { threadId: string }) { stopped.push(threadId); },
    },
    environments: {
      async status({ environmentId }: { environmentId: string; signal?: AbortSignal }) {
        if (mode === "status-hang") return new Promise<never>(() => {});
        const agent = environmentId.slice(4);
        return {
          outcome: "available" as const,
          workspaceStatus: {
            workingTree: { hasUncommittedChanges: false },
            checkout: { kind: "branch" as const, branchName: `fan-out/${agent}`, headSha: baseSha },
            branch: { currentBranch: `fan-out/${agent}` },
            mergeBase: { baseRef: baseSha },
          },
        };
      },
    },
    spawn: async (agent: number) => {
      if (mode === "error" && agent === 2) throw new Error("spawn refused");
      if (mode === "missing" && agent === 2) return null;
      return { id: `thread-${agent}`, environmentId: `env-${agent}` };
    },
  };
}

function gate(fake: ReturnType<typeof sdkFor>) {
  return async (agent: number) => {
    const result = await fake.spawn(agent);
    if (!result) throw new Error("no result");
    return result;
  };
}

test("adapter exit 2 preserves do-not-retry classification and native identity", () => {
  const failure = parseGateFailure("DO NOT RETRY: orphaned native_thread_id=thr-2: assignment write failed", 2);
  assert.equal(failure.doNotRetry, true);
  assert.equal(failure.nativeThreadId, "thr-2");
  assert.match(failure.message, /native_thread_id=thr-2/);
});

test("mutation proof: collapsing exit 2 to retryable is rejected", async () => {
  const source = await readFile(new URL("./server.ts", import.meta.url), "utf8");
  const mutant = source.replace(
    "const doNotRetry = exitCode === 2 || detail.startsWith(\"DO NOT RETRY:\");",
    "const doNotRetry = false;",
  );
  assert.notEqual(mutant, source);
  const module = await loadMutant(mutant);
  assert.equal(module.parseGateFailure("DO NOT RETRY: orphaned native_thread_id=thr-2: failed", 2).doNotRetry, false);
  assert.equal(parseGateFailure("DO NOT RETRY: orphaned native_thread_id=thr-2: failed", 2).doNotRetry, true);
});

test("documented run command parses after consuming run", () => {
  const parsed = parseArgs(["run", "--project", "project", "--count", "2", "--prompt", "read only", "--provider", "codex", "--model", "model", "--reasoning-level", "low", "--base-sha", baseSha]);
  assert.equal(parsed.baseSha, baseSha);
  assert.equal(parsed.count, 2);
});

test("success reports every agent and matching count", async () => {
  const fake = sdkFor();
  const result = await fanOut(fake, { ...options, count: 3 }, gate(fake));
  assert.equal(result.count, 3);
  assert.equal(result.agents.length, 3);
  assert.equal(new Set(result.agents.map((agent) => agent.environmentId)).size, 3);
  assert.equal(new Set(result.agents.map((agent) => agent.branchName)).size, 3);
});

test("agent error fails the whole run, identifies it, and stops siblings", async () => {
  const fake = sdkFor("error");
  await assert.rejects(() => fanOut(fake, { ...options, count: 3 }, gate(fake)), /agent 2/);
  assert.deepEqual(new Set(fake.stopped), new Set(["thread-1", "thread-3"]));
});

test("idle without output fails the whole run and identifies the agent", async () => {
  const fake = sdkFor("empty-output");
  await assert.rejects(() => fanOut(fake, { ...options, count: 1 }, gate(fake)), /result output was absent/);
});

test("agent with no returned result fails the whole run and identifies the agent", async () => {
  const fake = sdkFor("missing");
  await assert.rejects(() => fanOut(fake, { ...options, count: 3 }, gate(fake)), /agent 2/);
});

test("dirty or moved worktree proof fails closed", async () => {
  const fake = sdkFor();
  fake.environments.status = async () => ({
    outcome: "available" as const,
    workspaceStatus: {
      workingTree: { hasUncommittedChanges: true },
      checkout: { kind: "branch" as const, branchName: "fan-out/1", headSha: "moved" },
      branch: { currentBranch: "fan-out/1" }, mergeBase: { baseRef: baseSha },
    },
  });
  await assert.rejects(() => fanOut(fake, { ...options, count: 1 }, gate(fake)), /post-turn worktree proof/);
});

test("mutation proof: tolerating a failed agent is rejected", async () => {
  const source = await readFile(new URL("./server.ts", import.meta.url), "utf8");
  const mutant = source.replace("throw new AgentFailure(agent, `spawn error: ${describe(error)}`);", "return { id: `mutant-${agent}`, environmentId: `mutant-env-${agent}` };");
  assert.notEqual(mutant, source);
  const module = await loadMutant(mutant);
  const fake = sdkFor("error");
  await assert.doesNotReject(() => module.fanOut(fake, { ...options, count: 2 }, gate(fake)));
  await assert.rejects(() => fanOut(fake, { ...options, count: 2 }, gate(fake)), /agent 2/);
});

test("mutation proof: removing sibling cleanup is observable", async () => {
  const source = await readFile(new URL("./server.ts", import.meta.url), "utf8");
  const mutant = source.replace(
    "await Promise.allSettled([...spawned].map((threadId) => sdk.threads.stop({ threadId })));",
    "await Promise.allSettled([]);",
  );
  assert.notEqual(mutant, source);
  const module = await loadMutant(mutant);
  const mutantFake = sdkFor("error");
  await assert.rejects(() => module.fanOut(mutantFake, { ...options, count: 3 }, gate(mutantFake)), /agent 2/);
  assert.deepEqual(mutantFake.stopped, []);
  const realFake = sdkFor("error");
  await assert.rejects(() => fanOut(realFake, { ...options, count: 3 }, gate(realFake)), /agent 2/);
  assert.notDeepEqual(realFake.stopped, []);
});

test("mutation proof: removing the post-turn worktree guard is rejected", async () => {
  const source = await readFile(new URL("./server.ts", import.meta.url), "utf8");
  const mutant = source
    .replace("status.outcome !== \"available\" || !workspace ||", "false ||")
    .replace("workspace.workingTree.hasUncommittedChanges ||", "false ||")
    .replace("checkout?.kind !== \"branch\" ||", "false ||")
    .replace("checkout.headSha !== options.baseSha ||", "false ||")
    .replace("workspace.branch.currentBranch !== branchName ||", "false ||")
    .replace("workspace.mergeBase?.baseRef !== options.baseSha || !branchName", "false");
  assert.notEqual(mutant, source);
  const module = await loadMutant(mutant);
  const fake = sdkFor();
  fake.environments.status = async () => ({
    outcome: "available" as const,
    workspaceStatus: {
      workingTree: { hasUncommittedChanges: true },
      checkout: { kind: "branch" as const, branchName: "fan-out/1", headSha: "moved" },
      branch: { currentBranch: "fan-out/1" }, mergeBase: { baseRef: baseSha },
    },
  });
  await assert.doesNotReject(() => module.fanOut(fake, { ...options, count: 1 }, gate(fake)));
  await assert.rejects(() => fanOut(fake, { ...options, count: 1 }, gate(fake)), /post-turn worktree proof/);
});

test("environment status deadline rejects a stalled daemon", async () => {
  const fake = sdkFor("status-hang");
  await assert.rejects(() => fanOut(fake, { ...options, count: 1, deadlineMs: 5 }, gate(fake)), /environment status deadline/);
});

test("mutation proof: removing the environment status deadline is rejected", async () => {
  const source = await readFile(new URL("./server.ts", import.meta.url), "utf8");
  const mutant = source
    .replace(
      "const aborted = new Promise<never>((_, reject) => {\n    if (signal.aborted) reject(new AgentFailure(agent, \"environment status deadline exceeded\"));\n    signal.addEventListener(\"abort\", () => reject(new AgentFailure(agent, \"environment status deadline exceeded\")), { once: true });\n  });",
      "const aborted = new Promise<never>(() => {});",
    )
    .replace(
      "return await Promise.race([\n      sdk.environments.status({ environmentId, mergeBaseBranch: baseSha, signal }),\n      aborted,\n    ]);",
      "return await sdk.environments.status({ environmentId, mergeBaseBranch: baseSha, signal });",
    );
  assert.notEqual(mutant, source);
  const module = await loadMutant(mutant);
  const realFake = sdkFor("status-hang");
  await assert.rejects(() => fanOut(realFake, { ...options, count: 1, deadlineMs: 5 }, gate(realFake)), /environment status deadline/);
  const mutantFake = sdkFor("status-hang");
  const timeout = new Promise((resolve) => setTimeout(() => resolve("timed out"), 50));
  const mutantResult = await Promise.race([
    module.fanOut(mutantFake, { ...options, count: 1, deadlineMs: 5 }, gate(mutantFake)).then(() => "completed", () => "rejected"),
    timeout,
  ]);
  assert.equal(mutantResult, "timed out");
});

test("mutation proof: accepting idle without output is rejected", async () => {
  const source = await readFile(new URL("./server.ts", import.meta.url), "utf8");
  const mutant = source.replace(
    "if (response.output === null || !response.output.trim()) throw new AgentFailure(agent, \"result output was absent or empty\");",
    "if (false) throw new AgentFailure(agent, \"result output was absent or empty\");",
  );
  assert.notEqual(mutant, source);
  const module = await loadMutant(mutant);
  const fake = sdkFor("empty-output");
  await assert.doesNotReject(() => module.fanOut(fake, { ...options, count: 1 }, gate(fake)));
  await assert.rejects(() => fanOut(fake, { ...options, count: 1 }, gate(fake)), /result output was absent/);
});

test("mutation proof: rejecting a healthy run is rejected", async () => {
  const source = await readFile(new URL("./server.ts", import.meta.url), "utf8");
  const mutant = source.replace("return { count: results.length, agents: results };", "throw new Error(\"mutant rejected healthy run\");");
  assert.notEqual(mutant, source);
  const module = await loadMutant(mutant);
  const fake = sdkFor();
  await assert.rejects(() => module.fanOut(fake, { ...options, count: 2 }, gate(fake)), /mutant rejected healthy run/);
  await assert.doesNotReject(() => fanOut(fake, { ...options, count: 2 }, gate(fake)));
});

async function loadMutant(source: string): Promise<typeof import("./server.ts")> {
  const directory = await mkdtemp(path.join(tmpdir(), "fan-out-mutant-"));
  const file = path.join(directory, "server.ts");
  await writeFile(file, source);
  try { return await import(`${pathToFileURL(file).href}?mutation=${Date.now()}-${Math.random()}`); }
  finally { await rm(directory, { recursive: true, force: true }); }
}
