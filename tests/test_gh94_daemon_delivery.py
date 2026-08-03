from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.canonical.codex_delivery import (
    WorkerDeliveryContext,
    deliver_worker_turn,
    resolve_worker_delivery_context,
)
from llm_collab.daemon.gate import DECLARATION_ID, evaluate_observation_gate
from llm_collab.daemon.server import ProtocolError, parse_dispatch_request
from llm_collab.worker import derive_worker_id


def _session() -> dict[str, object]:
    return {
        "project_id": "paseo",
        "chat_id": "CHAT-94",
        "agent_id": "codex",
        "status": "active",
        "endpoint_id": "endpoint-codex",
        "binding_id": "binding-1",
        "binding_generation": 1,
        "repo_targets": ["app"],
        "runtime": {
            "session_id": "native-94",
            "instance_id": "runtime-94",
            "home": "/tmp/codex-home-94",
        },
    }


def _request() -> dict[str, object]:
    return {
        "worker_id": derive_worker_id(
            workspace_id="ws_94",
            scope_kind="project",
            scope_identity="paseo",
            conversation_id="CHAT-94",
            participant_id="participant_codex",
        ),
        "project_id": "paseo",
        "session": _session(),
        "message": {"path": "Chats/dir/to-codex.md"},
        "endpoint": {"url": "ws://127.0.0.1:4500", "token": None},
        "target": {
            "codex_home": "/tmp/codex-home-94",
            "repo_id": "app",
            "repo_root": "/tmp/repo",
            "cwd": "/tmp/repo",
            "user_agent_prefix": "llm-collab",
        },
        "correlation_id": "corr-94",
        "observed_at_utc": "2026-08-03T00:00:00+00:00",
        "timeout_seconds": 10,
        "model": None,
    }


class WorkerContextTest(unittest.TestCase):
    def test_worker_id_and_runtime_identity_are_exact(self) -> None:
        session = _session()
        context = resolve_worker_delivery_context(
            worker_id=_request()["worker_id"],
            project_id="paseo",
            workspace_id="ws_94",
            session=session,
        )
        self.assertIsInstance(context, WorkerDeliveryContext)
        self.assertEqual("native-94", context.native_session_id)
        with self.assertRaisesRegex(RuntimeError, "worker id"):
            resolve_worker_delivery_context(
                worker_id="worker_wrong",
                project_id="paseo",
                workspace_id="ws_94",
                session=session,
            )


class DispatchEnvelopeTest(unittest.TestCase):
    def test_closed_dispatch_envelope_accepts_locator_only_message(self) -> None:
        payload = json.dumps({"version": 1, "op": "dispatch", "request": _request()}).encode()
        parsed = parse_dispatch_request(payload)
        self.assertEqual("Chats/dir/to-codex.md", parsed["message"]["path"])

    def test_dispatch_envelope_rejects_packet_body_and_bad_timeout(self) -> None:
        for mutate in (
            lambda request: request["message"].update(body="never on the socket"),
            lambda request: request.update(timeout_seconds=181),
        ):
            request = _request()
            mutate(request)
            payload = json.dumps({"version": 1, "op": "dispatch", "request": request}).encode()
            with self.subTest(request=request), self.assertRaises(ProtocolError):
                parse_dispatch_request(payload)


class DispatchGateTest(unittest.TestCase):
    def test_dispatch_needs_observation_and_exact_dispatch_flags(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "declaration.json"
            path.write_text(
                json.dumps(
                    {
                        "declaration_version": 1,
                        "declaration_id": DECLARATION_ID,
                        "features": {
                            "daemon_" + "observation": True,
                            "canonical_" + "writes": True,
                            "runtime_" + "dispatch": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            base = {
                "THREAD_EVENT_RUNNER_ENABLED": "1",
                "THREAD_EVENT_RUNNER_OBSERVE": "1",
            }
            self.assertFalse(evaluate_observation_gate(path, environ=base).dispatch_effective)
            enabled = {**base, "THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD": "1"}
            self.assertTrue(evaluate_observation_gate(path, environ=enabled).dispatch_effective)

    def test_transport_is_not_constructed_when_canonical_control_is_off(self) -> None:
        made: list[str] = []
        context = resolve_worker_delivery_context(
            worker_id=_request()["worker_id"],
            project_id="paseo",
            workspace_id="ws_94",
            session=_session(),
        )
        with patch.dict(os.environ, {}, clear=False):
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "disabled"}):
                result = deliver_worker_turn(
                    object(),
                    workspace_root=Path("/tmp"),
                    context=context,
                    message={"path": "Chats/dir/to-codex.md"},
                    provider=object(),
                    runtime_home=object(),
                    trusted_project_root=object(),
                    observed_at_utc="now",
                    correlation_id="corr",
                    dispatch_enabled=True,
                    make_observe=lambda: made.append("observe") or (lambda _thread: None),
                    make_transport=lambda: made.append("transport") or object(),
                )
        self.assertEqual("gate_disabled", result["outcome"])
        self.assertEqual([], made)


if __name__ == "__main__":
    unittest.main()
