from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.canonical.codex_delivery import (
    WorkerDeliveryContext,
    CodexDeliveryError,
    deliver_worker_turn,
    resolve_worker_delivery_context,
)
from llm_collab.canonical.legacy_packet_materialization import (
    LegacyPacketMaterializationRefused,
    MAX_PACKET_BYTES,
    _selected_packet,
)
from llm_collab.daemon.gate import DECLARATION_ID, evaluate_observation_gate
from llm_collab.daemon.client import project_dispatch_session
from llm_collab.daemon.server import (
    ProtocolError,
    _resolve_authoritative_repo,
    parse_dispatch_request,
)
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

    def test_dispatch_projection_excludes_session_history(self) -> None:
        session = _session()
        session["processed_messages"] = ["x"] * 100_000
        projection = project_dispatch_session(session)
        self.assertNotIn("processed_messages", projection)
        self.assertEqual("native-94", projection["session_id"])
        self.assertEqual(
            {"session_id", "instance_id", "home"},
            set(projection["runtime"]),
        )
        self.assertLess(len(json.dumps(projection)), 2048)

    def test_projection_rejects_unbounded_repo_scope(self) -> None:
        session = _session()
        session["repo_targets"] = ["repo"] * 65
        with self.assertRaisesRegex(ValueError, "repo targets"):
            project_dispatch_session(session)


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

    def test_dispatch_envelope_rejects_a_payload_larger_than_the_socket_limit(self) -> None:
        request = _request()
        request["endpoint"]["token"] = "x" * 5000
        payload = json.dumps({"version": 1, "op": "dispatch", "request": request}).encode()
        with self.assertRaisesRegex(ProtocolError, "complete envelope"):
            parse_dispatch_request(payload)


class DispatchAuthorityTest(unittest.TestCase):
    class Store:
        class Paths:
            workspace_id = "ws_94"

        paths = Paths()

        def __init__(self, project_snapshot):
            self.project_snapshot = project_snapshot

        def current_registry_revision(self, *, workspace_id):
            self.assert_workspace(workspace_id)
            return "sha256:" + "ab" * 32

        def get_project_snapshot(self, *, workspace_id, project_id, registry_revision):
            self.assert_workspace(workspace_id)
            return {"snapshot_json": json.dumps(self.project_snapshot)}

        @staticmethod
        def assert_workspace(workspace_id):
            if workspace_id != "ws_94":
                raise AssertionError(workspace_id)

    def test_repo_root_must_match_immutable_project_authority(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            alternate = root / "alternate"
            repo.mkdir()
            alternate.mkdir()
            store = self.Store({"project_id": "paseo", "repos": {"app": str(repo)}})
            session = {"repo_targets": ["app"]}
            bad = {"repo_id": "app", "repo_root": str(alternate), "cwd": str(alternate)}
            with self.assertRaisesRegex(ValueError, "repo_root"):
                _resolve_authoritative_repo(
                    store,
                    workspace_root=root,
                    project_id="paseo",
                    session=session,
                    target=bad,
                )

            good = {"repo_id": "app", "repo_root": str(repo), "cwd": str(repo)}
            self.assertEqual(
                ("app", str(repo.resolve()), str(repo.resolve())),
                _resolve_authoritative_repo(
                    store,
                    workspace_root=root,
                    project_id="paseo",
                    session=session,
                    target=good,
                ),
            )

    def test_relative_projects_root_is_resolved_from_workspace_root(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repo = root / "projects" / "repo"
            repo.mkdir(parents=True)
            (root / "collab.config.json").write_text(json.dumps({"projects_root": "projects"}))
            store = self.Store({"project_id": "paseo", "repos": {"app": "repo"}})
            target = {"repo_id": "app", "repo_root": str(repo), "cwd": str(repo)}
            original = Path.cwd()
            try:
                os.chdir(repo)
                self.assertEqual(
                    ("app", str(repo.resolve()), str(repo.resolve())),
                    _resolve_authoritative_repo(
                        store,
                        workspace_root=root,
                        project_id="paseo",
                        session={"repo_targets": ["app"]},
                        target=target,
                    ),
                )
            finally:
                os.chdir(original)

    def test_selected_repo_must_be_in_packet_scope(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            store = self.Store({"project_id": "paseo", "repos": {"app": str(root)}})
            target = {"repo_id": "app", "repo_root": str(root), "cwd": str(root)}
            with self.assertRaisesRegex(ValueError, "packet repo scope"):
                _resolve_authoritative_repo(
                    store,
                    workspace_root=root.parent,
                    project_id="paseo",
                    session={"repo_targets": ["app", "api"]},
                    target=target,
                    packet_repo_targets=["api"],
                )

    def test_authority_descriptor_chain_survives_path_replacement(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "repo"
            cwd = root / "work"
            cwd.mkdir(parents=True)
            store = self.Store({"project_id": "paseo", "repos": {"app": str(root)}})
            target = {"repo_id": "app", "repo_root": str(root), "cwd": str(cwd)}
            chains = []
            _resolve_authoritative_repo(
                store,
                workspace_root=root.parent,
                project_id="paseo",
                session={"repo_targets": ["app"]},
                target=target,
                descriptor_chain_out=chains,
            )
            chain = chains[0]
            try:
                root.rename(root.with_name("repo-old"))
                (root / "work").mkdir(parents=True)
                self.assertEqual((str(root.resolve()), str(cwd.resolve())), chain.canonical_paths())
            finally:
                chain.close()


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

    def test_binding_join_precedes_first_materialization(self) -> None:
        context = resolve_worker_delivery_context(
            worker_id=_request()["worker_id"],
            project_id="paseo",
            workspace_id="ws_94",
            session=_session(),
        )
        with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
            with patch(
                "llm_collab.canonical.codex_delivery._require_exact_join",
                side_effect=CodexDeliveryError("binding mismatch"),
            ) as join, patch(
                "llm_collab.canonical.codex_delivery.materialize_selected_legacy_packet",
                side_effect=AssertionError("materialization preceded exact join"),
            ):
                with self.assertRaisesRegex(CodexDeliveryError, "binding mismatch"):
                    deliver_worker_turn(
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
                        make_observe=lambda: None,
                        make_transport=lambda: None,
                    )
                join.assert_called_once()


class PacketReadBoundTest(unittest.TestCase):
    def test_selected_packet_read_is_bounded(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            packet_dir = root / "Chats" / "dir"
            packet_dir.mkdir(parents=True)
            (packet_dir / "packet.md").write_bytes(b"x" * (MAX_PACKET_BYTES + 1))
            with self.assertRaises(LegacyPacketMaterializationRefused):
                _selected_packet(root, {"path": "Chats/dir/packet.md"})


if __name__ == "__main__":
    unittest.main()
