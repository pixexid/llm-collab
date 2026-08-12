import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";

import plugin, { WAKE_SCHEMA, deliverWake, rearmWake } from "./server.ts";

function database() {
  const db = new DatabaseSync(":memory:");
  db.exec(WAKE_SCHEMA);
  return db;
}

function target(project = "project-a", thread = "role-a") {
  return { project_id: project, thread_id: thread };
}

function api(send) {
  const warnings = [];
  return {
    sdk: { threads: { send } },
    log: { warn: (line) => warnings.push(line) },
    warnings,
  };
}

test("concurrent native and CLI producers reserve one pending wake", async () => {
  const db = database();
  const sends = [];
  const bb = api(async (request) => {
    sends.push(request);
    await Promise.resolve();
    return { ok: true };
  });
  const results = await Promise.all([
    deliverWake(bb, db, target(), "worker:thread-a", "a".repeat(64)),
    deliverWake(bb, db, target(), "pr-artifacts", "b".repeat(64)),
  ]);
  assert.deepEqual(results.sort(), ["accepted", "coalesced"]);
  assert.equal(sends.length, 1);
  assert.deepEqual(sends[0], {
    threadId: "role-a",
    input: [{ type: "text", text: "event; inspect canonical state", visibility: "agent-only" }],
    mode: "queue-if-active",
  });
});

test("pending reservation survives a plugin reload", async () => {
  const db = database();
  let sends = 0;
  assert.equal(
    await deliverWake(api(async () => (++sends, { ok: true })), db, target(), "heartbeat", "a".repeat(64)),
    "accepted",
  );
  assert.equal(
    await deliverWake(api(async () => (++sends, { ok: true })), db, target(), "heartbeat", "a".repeat(64)),
    "coalesced",
  );
  assert.equal(sends, 1);
});

test("semantic change coalesces while pending and recovery re-arms", async () => {
  const db = database();
  let sends = 0;
  const bb = api(async () => (++sends, { ok: true }));
  await deliverWake(bb, db, target(), "worker:thread-a", "a".repeat(64));
  assert.equal(
    await deliverWake(bb, db, target(), "worker:thread-a", "b".repeat(64)),
    "coalesced",
  );
  rearmWake(db, target(), "thread-a");
  assert.equal(
    await deliverWake(bb, db, target(), "worker:thread-a", "b".repeat(64)),
    "accepted",
  );
  assert.equal(sends, 2);
});

test("role idle re-arms every producer family", async () => {
  const db = database();
  let sends = 0;
  const bb = api(async () => (++sends, { ok: true }));
  await deliverWake(bb, db, target(), "pr-artifacts", "a".repeat(64));
  rearmWake(db, target(), "role-a");
  assert.equal(
    await deliverWake(bb, db, target(), "heartbeat", "b".repeat(64)),
    "accepted",
  );
  assert.equal(sends, 2);
});

test("confirmed failure releases while ambiguous failure suppresses retry", async () => {
  const confirmedDb = database();
  const confirmed = api(async () => { throw Object.assign(new Error("bad request"), { status: 400 }); });
  assert.equal(
    await deliverWake(confirmed, confirmedDb, target(), "heartbeat", "a".repeat(64)),
    "confirmed-failure",
  );
  assert.equal(
    confirmedDb.prepare("SELECT pending FROM role_wake_dedupe").get().pending,
    0,
  );

  const ambiguousDb = database();
  const ambiguous = api(async () => { throw Object.assign(new Error("server lost reply"), { status: 500 }); });
  assert.equal(
    await deliverWake(ambiguous, ambiguousDb, target(), "heartbeat", "a".repeat(64)),
    "ambiguous",
  );
  assert.equal(
    await deliverWake(api(async () => assert.fail("retry must be suppressed")), ambiguousDb, target(), "heartbeat", "a".repeat(64)),
    "coalesced",
  );
});

test("role promotion retargets at the new role-thread key", async () => {
  const db = database();
  const sent = [];
  const bb = api(async ({ threadId }) => (sent.push(threadId), { ok: true }));
  await deliverWake(bb, db, target("project-a", "role-old"), "heartbeat", "a".repeat(64));
  await deliverWake(bb, db, target("project-a", "role-new"), "heartbeat", "a".repeat(64));
  assert.deepEqual(sent, ["role-old", "role-new"]);
});

test("project rows are isolated even when role ids collide", async () => {
  const db = database();
  let sends = 0;
  const bb = api(async () => (++sends, { ok: true }));
  await deliverWake(bb, db, target("project-a", "same-role"), "heartbeat", "a".repeat(64));
  await deliverWake(bb, db, target("project-b", "same-role"), "heartbeat", "a".repeat(64));
  assert.equal(sends, 2);
  assert.equal(db.prepare("SELECT count(*) AS n FROM role_wake_dedupe").get().n, 2);
});

test("malformed dedupe schema fails closed before send", async () => {
  const db = new DatabaseSync(":memory:");
  db.exec("CREATE TABLE role_wake_dedupe (project_id TEXT)");
  let sends = 0;
  await assert.rejects(
    deliverWake(api(async () => (++sends, { ok: true })), db, target(), "heartbeat", "a".repeat(64)),
    /no column named role_thread_id/,
  );
  assert.equal(sends, 0);
});

test("plugin registers the three abnormal events, idle re-arm, CLI, and one reconcile", async () => {
  const db = database();
  const events = new Map();
  let cli;
  let reconciles = 0;
  plugin({
    storage: { database: () => db, migrate: () => {} },
    settings: { define: () => ({ get: async () => ({}) }) },
    events: { on: (name, handler) => events.set(name, handler) },
    cli: { register: (registration) => { cli = registration; } },
    sdk: {
      threads: {
        list: async () => (++reconciles, []),
        send: async () => ({ ok: true }),
      },
    },
    log: { warn: () => {}, info: () => {} },
  });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(
    [...events.keys()].sort(),
    ["thread.archived", "thread.created", "thread.deleted", "thread.failed", "thread.idle"],
  );
  assert.equal(cli.name, "silent-wake");
  assert.equal(reconciles, 1);
});
