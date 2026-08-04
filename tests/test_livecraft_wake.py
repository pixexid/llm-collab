from __future__ import annotations

import io
import json
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import livecraft_wake  # noqa: E402


class LivecraftWakeTest(unittest.TestCase):
    def test_prompts_exact_native_session_with_exact_drain_command(self):
        payload = {
            "session": {
                "session_id": "SESSION-LIVECRAFT-GLMPI-CHAT-bfe59384",
                "agent_id": "glmpi",
                "project_id": "llm-collab",
                "chat_id": "CHAT-82A03B1D",
                "repo_targets": ["app"],
                "runtime_session_id": "bfe59384-f808-4885-8aed-604774d728fc",
            },
            "message": {"path": "Chats/wake.md"},
        }
        events = []

        def health_side_effect(_url):
            events.append("health")

        def prompt_side_effect(**_kwargs):
            events.append("prompt")
            return {"accepted": True}

        with mock.patch.object(livecraft_wake.sys, "stdin", io.TextIOWrapper(io.BytesIO(
            (json.dumps(payload) + "\n").encode()
        ))), mock.patch.object(
            livecraft_wake, "ensure_livecraft_ready", side_effect=health_side_effect
        ) as health, mock.patch.object(
            livecraft_wake, "_prompt", side_effect=prompt_side_effect
        ) as prompt:
            self.assertEqual(
                livecraft_wake.main([
                    "--backend-url", "http://127.0.0.1:43121",
                    "--runtime-root", "/runtime",
                ]),
                0,
            )
        health.assert_called_once_with("http://127.0.0.1:43121")
        self.assertEqual(events, ["health", "prompt"])
        message = prompt.call_args.kwargs["message"]
        self.assertIn("Packet path: Chats/wake.md", message)
        self.assertIn(
            "LLM_COLLAB_READER_RUNTIME_ID=bfe59384-f808-4885-8aed-604774d728fc",
            message,
        )
        self.assertIn(
            f"{shlex.quote(sys.executable)} /runtime/bin/inbox.py --me glmpi "
            "--session SESSION-LIVECRAFT-GLMPI-CHAT-bfe59384 "
            "--project llm-collab --chat CHAT-82A03B1D --repo-target app --acknowledge --json",
            message,
        )
        self.assertEqual(prompt.call_args.kwargs["native"], "bfe59384-f808-4885-8aed-604774d728fc")


if __name__ == "__main__":
    unittest.main()
