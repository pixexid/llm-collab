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
            "binding_id": "binding-livecraft", "binding_generation": 2,
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

    def test_dispatch_wakes_livecraft_before_legacy_materialization(self):
        fingerprint = {
            "cwd": "/repo", "provider": "zai", "model_id": "glm-5.2", "thinking_level": "max",
        }
        session = {
            "session_id": "SESSION-LIVECRAFT-GLMPI-CHAT-bfe59384",
            "agent_id": "glmpi", "project_id": "llm-collab", "chat_id": "CHAT-82A03B1D",
            "repo_targets": ["app"], "endpoint_id": "endpoint_pi_livecraft_local",
            "binding_id": "binding-livecraft", "binding_generation": 2,
            "mode": "auto-read", "wake_strategy": "runtime_trigger",
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
                "target_binding_id": "binding-livecraft", "target_binding_generation": 2,
            },
            "body": "Wake body",
        }
        prepared = ({**session, "processed_messages": [message["path"]]}, "{}")
        with mock.patch.object(bridge, "load_session", return_value=session), \
             mock.patch.object(bridge, "session_is_dispatchable", return_value=(True, "ok")), \
             mock.patch.object(bridge, "matching_unread_messages", return_value=[message]), \
             mock.patch.object(bridge, "message_targets_session", return_value=(True, "test")), \
             mock.patch.object(bridge, "processed_messages", return_value=set()), \
             mock.patch.object(bridge, "should_skip_for_loop_protection", return_value=(False, "ok")), \
             mock.patch.object(bridge, "reserve_message_result", return_value=prepared), \
             mock.patch.object(bridge, "claim_message_activation", return_value=(True, None)), \
             mock.patch.object(bridge, "resolve_effective_action", return_value=("runtime_trigger", "test")), \
             mock.patch.object(bridge, "materialize_selected_runtime_packet") as materialize, \
             mock.patch.object(bridge, "read_pi_session_fingerprint", return_value=fingerprint), \
             mock.patch.object(
                 bridge.subprocess, "run",
                 return_value=CompletedProcess([], 0, "prompted", ""),
             ), \
             mock.patch.object(bridge, "append_event"), \
             mock.patch.object(bridge, "mark_message_processed") as mark_processed:
            result = bridge.dispatch_session("SESSION-LIVECRAFT-GLMPI-CHAT-bfe59384")

        self.assertEqual(1, result["matched_messages"])
        self.assertEqual(0, materialize.call_count)
        self.assertEqual(1, mark_processed.call_count)
        action = result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertFalse(action["runtime_result"]["delivery_accepted"])


if __name__ == "__main__":
    unittest.main()
