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
const path = require("path");

const root = path.resolve(__dirname, "..");

function readJson(filePath) {
  if (!fs.existsSync(filePath)) return null;
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

const config = readJson(path.join(root, "collab.config.json")) || {};
const agentsPayload = readJson(path.join(root, "agents.json")) || {};
const agents = Array.isArray(agentsPayload.agents) ? agentsPayload.agents : [];

const workspaceName = config.workspace_name || "collab";
const pollSeconds = config.poll_interval_seconds || 15;
const notificationsEnabled = config.notifications_enabled !== false;
const logsDir = path.join(root, "Logs", "watchers");
const python = process.env.PYTHON || "python3";
const watchScript = path.join(root, "bin", "watch_inbox.py");

const watcherAgents = agents.filter(
  (a) =>
    a.activation &&
    a.activation.watcher_enabled === true &&
    a.activation.type !== "human_relay" &&
    a.activation.type !== "human"
);

module.exports = {
  apps: watcherAgents.map((agent) => {
    const appArgs = [
      watchScript,
      "--me", agent.id,
      "--poll-seconds", String(pollSeconds),
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
      env: {
        PYTHONUNBUFFERED: "1",
      },
    };
  }),
};
