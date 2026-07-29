import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const cli = resolve(dirname(fileURLToPath(import.meta.url)), "../bin/llm-collab");

export default function (pi) {
	let nativeSessionId;

	async function deactivate() {
		if (!nativeSessionId) return;
		const result = await pi.exec(
			cli,
			["session_autobridge.py", "deactivate-pi", "--native-session-id", nativeSessionId],
			{ timeout: 5_000 },
		);
		if (result.code !== 0) throw new Error(result.stderr || "llm-collab Pi deactivation failed");
	}

	pi.on("session_start", async (_event, ctx) => {
		nativeSessionId = ctx.sessionManager.getSessionId();
		await deactivate();
	});
	pi.on("session_before_switch", deactivate);
	pi.on("session_before_fork", deactivate);
	pi.on("session_shutdown", deactivate);
}
