from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import _session_autobridge as bridge  # noqa: E402


class LivecraftRuntimeTriggerTest(unittest.TestCase):
    def test_livecraft_wake_keeps_durable_packet_for_worker_drain(self):
        fingerprint = {
            "cwd": "/repo", "provider": "zai", "model_id": "glm-5.2", "thinking_level": "max",
        }
        session = {
            "session_id": "SESSION-LIVECRAFT-GLMPI-CHAT-bfe59384",
            "agent_id": "glmpi", "project_id": "llm-collab", "chat_id": "CHAT-82A03B1D",
            "repo_targets": ["app"], "endpoint_id": "endpoint_pi_livecraft_local",
            "runtime": {
                "family": "pi", "session_id": "bfe59384-f808-4885-8aed-604774d728fc",
                "session_source": "/pi/session.jsonl", "home": "/pi",
                "command": ["python3", "/runtime/bin/livecraft_wake.py"],
            },
            "pi_fingerprint": fingerprint,
        }
        message = {
            "path": "Chats/wake.md",
            "frontmatter": {
                "from": "claude", "to": "glmpi", "project_id": "llm-collab",
                "chat_id": "CHAT-82A03B1D", "repo_targets": ["app"],
            },
            "body": "Wake body",
        }
        with mock.patch.object(bridge, "read_pi_session_fingerprint", return_value=fingerprint), \
             mock.patch.object(
                 bridge.subprocess, "run",
                 return_value=CompletedProcess([], 0, "prompted", ""),
             ) as run:
            result = bridge.execute_runtime_trigger(session, message)
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["delivery_accepted"])
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["session"]["repo_targets"], ["app"])
        self.assertEqual(payload["message"]["path"], "Chats/wake.md")


if __name__ == "__main__":
    unittest.main()
