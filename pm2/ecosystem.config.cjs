/**
 * PM2 ecosystem config for llm-collab inbox watchers.
 *
 * Reads agents.json dynamically — add a new agent with
 * activation.watcher_enabled: true and PM2 will pick it up on next reload.
 *
 * App naming: {workspace_name}-{agent_id}
 * (workspace_name is read from collab.config.json)
 */

const fs = require("fs");
const os = require("os");
const path = require("path");

const root = path.resolve(__dirname, "..");

// Mirrors TOKEN_BLANK_CHARS in bin/pm2_watchers.py. String.trim() must NOT be used here:
// it strips U+FEFF, which the CLI treats as content, and keeps U+0085, which the CLI
// treats as blank -- inverted from Python on BOTH, so the two gates disagreed in opposite
// directions about the same file. This is Rust's Unicode White_Space set, which is what
// the CLI itself trims.
const TOKEN_BLANK_PATTERN =
  /[\t\n\v\f\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/g;

function readJson(filePath) {
  if (!fs.existsSync(filePath)) return null;
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

const config = readJson(path.join(root, "collab.config.json")) || {};
const agentsPayload = readJson(path.join(root, "agents.json")) || {};
const agents = Array.isArray(agentsPayload.agents) ? agentsPayload.agents : [];

const workspaceName = config.workspace_name || "collab";
const pollSeconds = config.poll_interval_seconds || 15;
const refusalRecheckWindowDays = 7;
const notificationsEnabled = config.notifications_enabled !== false;
const logsDir = path.join(root, "Logs", "watchers");
const python = process.env.PYTHON || "python3";
const watchScript = path.join(root, "bin", "watch_inbox.py");

function buildWatcherEnv(agent) {
  const env = {
    PYTHONUNBUFFERED: "1",
  };

  if (agent.id === "codex") {
    env.LLM_COLLAB_CODEX_UI_REFRESH_METHOD =
      process.env.LLM_COLLAB_CODEX_UI_REFRESH_METHOD || "cdp";
    env.LLM_COLLAB_CODEX_CDP_PORT =
      process.env.LLM_COLLAB_CODEX_CDP_PORT || "9223";
  }

  return env;
}

const watcherAgents = agents.filter(
  (a) =>
    a.activation &&
    a.activation.watcher_enabled === true
);

// Codex delivery transport: a sidecar `codex app-server` on a localhost WebSocket,
// sharing the desktop app's CODEX_HOME so turn/start reaches the same threads.
// ponytail: presence of the token file is the enable switch — no new config surface.
// Add a codex_app_server block to collab.config.json only if per-agent ports are ever needed.
// The single path invariant for the sidecar. Every path that is later COMPARED or
// LAUNCHED must pass through this: absolute (resolved against the repository root, not
// the invoker's cwd), redundant segments collapsed, no trailing separator, and symlinks
// deliberately NOT resolved because discovery matches the launched spelling literally.
//
// Five separate defects came from normalizing one side of a two-sided comparison:
// validating a token path the spawned app would read differently, canonicalizing a
// runtime home at registration while launching it verbatim, and resolving relative
// overrides against different bases in the manager and here. One function, applied
// everywhere, is the fix for the class rather than the instances.
function canonicalPath(value, base) {
  if (!value) return value;
  let text = String(value).trim();
  // Mirror Python's os.path.expanduser: Node's path.resolve does NOT expand `~`, so
  // `~/.codex` resolved to <repo>/~/.codex here while Python produced $HOME/.codex.
  // The two sides then disagreed on the one input a human is most likely to type.
  if (text === "~" || text.startsWith("~/")) {
    const home = os.homedir();
    text = text === "~" ? home : path.join(home, text.slice(2));
  }
  const resolved = path.resolve(base || root, text);
  return resolved.length > 1 ? resolved.replace(/\/+$/, "") : resolved;
}

function codexAppServerApps() {
  // The reservation must live HERE, not only in bin/pm2_watchers.py: this config
  // maps watcher agents and appends the transport independently, so a registered
  // agent literally named `codex-appserver` would emit two apps with one PM2 name.
  // Skip rather than throw — throwing would take the watchers down with it — and
  // warn, while the Python manager exits loudly on the path operators actually use.
  // Full roster, not just watcherAgents: an agent registered with
  // watcher_enabled:false still owns that identity, and the direct ecosystem path
  // would otherwise start the transport under a collaborator's PM2 name.
  const reserved = agents.filter((a) => a.id === "codex-appserver");
  if (reserved.length > 0) {
    console.error(
      "[ecosystem] agents.json registers 'codex-appserver', a reserved transport " +
        "sidecar id; skipping the sidecar to avoid a duplicate PM2 app name. " +
        "Rename the agent."
    );
    return [];
  }

  // Resolve BEFORE validating. A relative override is validated against the
  // invoker's cwd, but the spawned app runs with cwd: root and would receive the
  // unchanged relative path -- so the gate could check one file while Codex opens
  // another. Resolve against root so validation and launch see the same path.
  const configuredToken = process.env.LLM_COLLAB_CODEX_APP_SERVER_TOKEN_FILE;
  const tokenFile = canonicalPath(
    configuredToken || path.join(root, ".secrets", "codex_app_server_ws_token")
  );
  const codexBin = canonicalPath(
    process.env.LLM_COLLAB_CODEX_BIN ||
      "/Applications/ChatGPT.app/Contents/Resources/codex"
  );
  if (!fs.existsSync(codexBin)) return [];
  // Fail closed on an insecure bearer token. The listener is loopback-only, but on a
  // multi-user host any local account that can read this file can invoke App Server
  // operations against the operator's real Codex account. A path containing
  // whitespace is also refused: delivery discovery parses flattened `ps` output and
  // would truncate it, then attempt the authenticated socket with no token at all.
  let stat;
  try {
    stat = fs.statSync(tokenFile);
  } catch {
    return [];
  }
  if (!stat.isFile()) return [];
  // process.getuid is POSIX-only. Calling it on Windows throws during module
  // evaluation, which would prevent the ENTIRE ecosystem from loading -- including
  // unrelated inbox watchers -- instead of merely disabling an unsupported sidecar.
  if (typeof process.getuid !== "function") return [];
  if (stat.uid !== process.getuid()) return [];
  if ((stat.mode & 0o077) !== 0) return [];
  if (/\s/.test(tokenFile)) return [];
  // An empty or whitespace-only token passes every path and mode check above, and the
  // Codex CLI then exits immediately with "websocket auth secret must not be empty",
  // leaving PM2 to exhaust its restart budget while the manager still reports the
  // transport configured. Mirrors sidecar_token_is_secure().
  // Strict decoding, matching sidecar_token_is_secure(). readFileSync(..., "utf8")
  // substitutes U+FFFD for invalid bytes, so a truncated or binary token reads as
  // non-blank here while the CLI exits before listening with "stream did not contain
  // valid UTF-8". TextDecoder with fatal:true refuses instead of substituting.
  try {
    // ignoreBOM:true means "do not treat a leading BOM specially", i.e. KEEP it. The
    // default strips it, so a BOM-only token decoded to "" here while Python's
    // bytes.decode kept the character -- the same file, content to one gate and blank to
    // the other, one layer below the trim divergence.
    const decoded = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true })
      .decode(fs.readFileSync(tokenFile));
    // The token must be usable by the DELIVERY CLIENT too, not merely accepted by the CLI:
    // the client sends it in an HTTP Authorization header, where a BOM cannot be encoded and
    // U+001C is stripped away to nothing. Mirrors token_is_usable() in bin/pm2_watchers.py.
    const secret = decoded.replace(TOKEN_BLANK_PATTERN, "");
    if (!secret) return [];
    if (!/^[\x21-\x7e]+$/.test(secret)) return [];
  } catch {
    return [];
  }

  const port = process.env.LLM_COLLAB_CODEX_APP_SERVER_PORT || "8767";
  // Canonical here too: discovery compares CODEX_HOME literally against the process
  // command line, so launching `/x/.codex/` while a session stored `/x/.codex` yields
  // no endpoint and no diagnostic.
  const codexHome = canonicalPath(
    process.env.LLM_COLLAB_CODEX_HOME || path.join(process.env.HOME || "", ".codex")
  );

  return [
    {
      name: `${workspaceName}-codex-appserver`,
      cwd: root,
      script: codexBin,
      args: [
        "app-server",
        "--listen", `ws://127.0.0.1:${port}`,
        "--ws-auth", "capability-token",
        "--ws-token-file", tokenFile,
      ],
      autorestart: true,
      watch: false,
      time: true,
      max_restarts: 10,
      min_uptime: "5s",
      out_file: path.join(logsDir, "codex-appserver.pm2.out.log"),
      error_file: path.join(logsDir, "codex-appserver.pm2.err.log"),
      merge_logs: false,
      env: { CODEX_HOME: codexHome },
    },
  ];
}

const watcherApps = watcherAgents.map((agent) => {
    const appArgs = [
      watchScript,
      "--me", agent.id,
      "--poll-seconds", String(pollSeconds),
      "--refusal-recheck-window-days", String(refusalRecheckWindowDays),
      "--skip-existing",
    ];
    if (notificationsEnabled) appArgs.push("--notify");

    return {
      name: `${workspaceName}-${agent.id}`,
      cwd: root,
      script: python,
      args: appArgs,
      autorestart: true,
      watch: false,
      time: true,
      max_restarts: 10,
      min_uptime: "5s",
      out_file: path.join(logsDir, `${agent.id}.pm2.out.log`),
      error_file: path.join(logsDir, `${agent.id}.pm2.err.log`),
      merge_logs: false,
      env: buildWatcherEnv(agent),
    };
});

module.exports = {
  apps: [...watcherApps, ...codexAppServerApps()],
  // Exported for the cross-language parity test: it must exercise THIS function, not a
  // copy of it. A test that reimplements the logic it checks cannot detect drift, which
  // is the entire failure mode the parity test exists to catch. PM2 reads only `apps`.
  canonicalPath,
  TOKEN_BLANK_PATTERN,
};
