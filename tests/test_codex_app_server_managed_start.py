import json
import unittest

from llm_collab.codex_app_server_live_probe import READ_ONLY_REQUEST_METHODS
from llm_collab.codex_app_server_managed_start import (
    ManagedCodexStartConfig,
    ManagedCodexStartError,
    ManagedCodexStartTransport,
)
from llm_collab.session_lifecycle import ManagedStartOrphaned, ManagedStartResponseLost


THREAD_ID = "019f9452-6954-7301-bff9-db1c47432bc8"
CWD = "/trusted/project"


def thread(thread_id=THREAD_ID):
    return {
        "cliVersion": "0.146.0",
        "createdAt": 1,
        "cwd": CWD,
        "ephemeral": True,
        "id": thread_id,
        "modelProvider": "openai",
        "preview": "",
        "sessionId": "session-1",
        "source": "appServer",
        "status": {"type": "idle"},
        "turns": [],
        "updatedAt": 1,
    }


def start_result(thread_id=THREAD_ID):
    return {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": CWD,
        "model": "gpt-test",
        "modelProvider": "openai",
        "sandbox": {"type": "readOnly"},
        "thread": thread(thread_id),
    }


class FakeAppServer:
    def __init__(self, *, initialize=None, start=None, read=None,
                 start_response_id=None, read_response_id=None):
        self.initialize = initialize if initialize is not None else {
            "codexHome": "/trusted/codex-home",
            "userAgent": "llm-collab/0.146.0",
        }
        self.start = start if start is not None else start_result()
        self.read = read if read is not None else {"thread": thread()}
        self.start_response_id = start_response_id
        self.read_response_id = read_response_id
        self.requests = []
        self.notifications = []

    def exchange(self, frame):
        self.requests.append(frame)
        method = frame["method"]
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": frame["id"], "result": self.initialize}
        if method == "thread/start":
            response_id = self.start_response_id or frame["id"]
            return self.start if isinstance(self.start, str) else {
                "jsonrpc": "2.0", "id": response_id, "result": self.start
            }
        if method == "thread/read":
            response_id = self.read_response_id or frame["id"]
            return self.read if isinstance(self.read, str) else {
                "jsonrpc": "2.0", "id": response_id, "result": self.read
            }
        raise AssertionError(f"unexpected method {method}")

    def notify(self, frame):
        self.notifications.append(frame)


def config():
    return ManagedCodexStartConfig(
        endpoint_id="endpoint_codex",
        runtime_instance_id="runtime_one",
        runtime_home_id="home_hash",
        runtime_home_realpath="/trusted/codex-home",
        project_id="project_app",
        repo_id="repo_app",
        canonical_cwd=CWD,
        provider_revision="revision_1",
        model="gpt-test",
        model_provider="openai",
        approval_policy="never",
        sandbox_request="read-only",
        sandbox_response={"type": "readOnly"},
    )


class ManagedCodexStartTransportTests(unittest.TestCase):
    def test_real_0146_shape_uses_one_connection_and_returns_child1_candidate(self):
        fake = FakeAppServer()
        candidate = ManagedCodexStartTransport(fake, config=config())("start_local")

        self.assertEqual(
            ["initialize", "thread/start", "thread/read"],
            [frame["method"] for frame in fake.requests],
        )
        self.assertEqual(["initialized"], [frame["method"] for frame in fake.notifications])
        self.assertEqual({"threadId": THREAD_ID, "includeTurns": False}, fake.requests[2]["params"])
        self.assertEqual("managed_thread_start", candidate["creation_provenance"]["source"])
        self.assertNotIn("server_correlation_id", candidate["creation_provenance"])
        self.assertEqual(THREAD_ID, candidate["native_thread_id"])
        self.assertEqual(THREAD_ID, candidate["read_back"]["native_thread_id"])

    def test_jsonrpc_request_id_is_transport_local(self):
        fake = FakeAppServer()
        candidate = ManagedCodexStartTransport(fake, config=config())("reservation-id")

        self.assertEqual("llm-collab-2", fake.requests[1]["id"])
        self.assertNotIn("reservation-id", json.dumps(candidate))
        self.assertNotEqual(fake.requests[1]["id"], candidate["native_thread_id"])

    def test_read_only_probe_allowlist_remains_closed(self):
        self.assertNotIn("thread/start", READ_ONLY_REQUEST_METHODS)
        self.assertEqual(("initialize", "model/list"), READ_ONLY_REQUEST_METHODS)

    def test_start_jsonrpc_error_is_rejected(self):
        fake = FakeAppServer(start={"error": {"code": -1}})
        # The fake result is intentionally not a valid result envelope; use a raw
        # envelope so the shared parser sees the native JSON-RPC error.
        fake.start = '{"jsonrpc":"2.0","id":"llm-collab-2","error":{"code":-1}}'
        with self.assertRaises(ManagedStartResponseLost):
            ManagedCodexStartTransport(fake, config=config())("start")

    def test_runtime_home_mismatch_fails_before_thread_start(self):
        fake = FakeAppServer(initialize={
            "codexHome": "/foreign/home",
            "userAgent": "llm-collab/0.146.0",
        })
        with self.assertRaisesRegex(ManagedCodexStartError, "runtime home"):
            ManagedCodexStartTransport(fake, config=config())("start")
        self.assertEqual(["initialize"], [frame["method"] for frame in fake.requests])

    def test_cli_version_drift_after_start_is_retry_suppressing(self):
        drifted = thread()
        drifted["cliVersion"] = "0.147.0"
        fake = FakeAppServer(start={**start_result(), "thread": drifted})
        with self.assertRaises(ManagedStartOrphaned) as caught:
            ManagedCodexStartTransport(fake, config=config())("start")
        self.assertEqual(THREAD_ID, caught.exception.native_session_id)

    def test_mismatched_start_response_id_is_rejected(self):
        fake = FakeAppServer(start_response_id="other-id")
        with self.assertRaises(ManagedStartResponseLost):
            ManagedCodexStartTransport(fake, config=config())("start")

    def test_mismatched_read_response_id_is_rejected(self):
        fake = FakeAppServer(read_response_id="other-id")
        with self.assertRaises(ManagedStartOrphaned) as caught:
            ManagedCodexStartTransport(fake, config=config())("start")
        self.assertEqual(THREAD_ID, caught.exception.native_session_id)

    def test_readback_id_mismatch_is_rejected(self):
        fake = FakeAppServer(read={"thread": thread("different-thread")})
        with self.assertRaises(ManagedStartOrphaned) as caught:
            ManagedCodexStartTransport(fake, config=config())("start")
        self.assertEqual(THREAD_ID, caught.exception.native_session_id)

    def test_missing_create_field_is_rejected_and_is_load_bearing(self):
        malformed = start_result()
        del malformed["sandbox"]
        fake = FakeAppServer(start=malformed)
        with self.assertRaises(ManagedStartOrphaned) as caught:
            ManagedCodexStartTransport(fake, config=config())("start")
        self.assertEqual(THREAD_ID, caught.exception.native_session_id)

    def test_attach_shaped_result_is_rejected(self):
        attach_shaped = {"thread": thread()}
        fake = FakeAppServer(start=attach_shaped)
        with self.assertRaises(ManagedStartOrphaned):
            ManagedCodexStartTransport(fake, config=config())("start")

    def test_ambiguous_multiple_threads_are_rejected(self):
        ambiguous = start_result()
        ambiguous["threads"] = [thread(), thread("another-thread")]
        fake = FakeAppServer(start=ambiguous)
        with self.assertRaises(ManagedStartOrphaned):
            ManagedCodexStartTransport(fake, config=config())("start")

    def test_duplicate_nested_response_member_is_rejected(self):
        raw = (
            '{"jsonrpc":"2.0","id":"llm-collab-2","result":'
            '{"approvalPolicy":"never","approvalPolicy":"never"}}'
        )
        fake = FakeAppServer(start=raw)
        with self.assertRaises(ManagedStartResponseLost):
            ManagedCodexStartTransport(fake, config=config())("start")

    def test_server_initiated_request_is_rejected(self):
        raw = '{"jsonrpc":"2.0","id":"server-1","method":"item/permissions/requestApproval","params":{}}'
        fake = FakeAppServer(start=raw)
        with self.assertRaises(ManagedStartResponseLost):
            ManagedCodexStartTransport(fake, config=config())("start")

    def test_connection_is_single_use(self):
        fake = FakeAppServer()
        transport = ManagedCodexStartTransport(fake, config=config())
        transport("start")
        with self.assertRaisesRegex(ManagedCodexStartError, "single-use"):
            transport("start-again")


if __name__ == "__main__":
    unittest.main()
