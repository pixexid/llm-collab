"""Focused control-plane tests for the default-off Paseo adapter subject."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from llm_collab.paseo_runtime_adapter import PaseoAdapter, PaseoAdapterIdentity
from llm_collab.runtime_adapter_manifest import ManifestResolutionError, TrustedManifestRegistry
from llm_collab.runtime_adapter_supervisor import StdioSupervisor


ROOT = Path(__file__).resolve().parents[1]


def frame(method: str, params: dict, request_id: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )


def identity() -> PaseoAdapterIdentity:
    return PaseoAdapterIdentity()


def initialize_params() -> dict:
    value = identity()
    return {
        "requested_protocol_version": "1.0",
        "adapter_id": value.adapter_id,
        "adapter_revision": value.adapter_revision,
        "manifest_id": value.manifest_id,
        "manifest_revision": value.manifest_revision,
        "endpoint": value.endpoint(),
    }


def manifest() -> dict:
    return {
        "paseo_cli_v1": {
            "adapter_id": identity().adapter_id,
            "adapter_revision": identity().adapter_revision,
            "manifest_id": identity().manifest_id,
            "manifest_revision": identity().manifest_revision,
            "endpoint": identity().endpoint(),
            "executable": sys.executable,
            "argv": [sys.executable, "-m", "llm_collab.paseo_runtime_adapter"],
            "working_directory": str(ROOT),
            "environment": {"PYTHONUNBUFFERED": "1"},
            "environment_allowlist": ["PYTHONUNBUFFERED"],
        }
    }


class PaseoRuntimeAdapterTests(unittest.TestCase):
    def test_control_plane_round_trip_and_all_mutating_methods_are_unsupported(self) -> None:
        resolved = TrustedManifestRegistry(manifest()).resolve("paseo_cli_v1")
        with StdioSupervisor(resolved) as supervisor:
            initialized = supervisor.request(frame("initialize", initialize_params(), "init-1"))
            self.assertIsNone(initialized.fault)
            init_payload = json.loads(initialized.response)
            self.assertEqual(init_payload["result"]["endpoint"], identity().endpoint())
            capabilities = init_payload["result"]["capability_set"]["capabilities"]
            self.assertEqual(
                {item["capability"] for item in capabilities},
                {"runtime.deliver", "runtime.cancel", "runtime.reconcile"},
            )
            self.assertTrue(all(item["quality"] == "unsupported" for item in capabilities))

            health = supervisor.request(frame("runtime.health", {}, "health-1"))
            self.assertIsNone(health.fault)
            self.assertEqual(json.loads(health.response)["result"]["status"], "healthy")

            malformed_health = supervisor.request(frame("runtime.health", {"agent_id": "wrong"}, "health-2"))
            self.assertEqual(json.loads(malformed_health.response)["error"]["data"]["name"], "INVALID_PARAMS")

            for method in ("runtime.deliver", "runtime.reconcile", "runtime.cancel"):
                refusal = supervisor.request(frame(method, {}, f"{method}-1"))
                self.assertIsNone(refusal.fault)
                self.assertEqual(json.loads(refusal.response)["error"]["data"]["name"], "CAPABILITY_NOT_DECLARED")

            shutdown = supervisor.request(frame("runtime.shutdown", {}, "shutdown-1"))
            self.assertEqual(json.loads(shutdown.response)["result"], {"status": "shutdown_started"})

    def test_initialize_rejects_protocol_and_identity_mismatch_without_starting_paseo(self) -> None:
        adapter = PaseoAdapter()
        bad_version = initialize_params()
        bad_version["requested_protocol_version"] = "9.9"
        self.assertEqual(
            json.loads(adapter.handle_text(frame("initialize", bad_version, "bad-version")))
            ["error"]["data"]["name"],
            "UNSUPPORTED_PROTOCOL_VERSION",
        )

        adapter = PaseoAdapter()
        bad_identity = initialize_params()
        bad_identity["adapter_id"] = "wrong"
        self.assertEqual(
            json.loads(adapter.handle_text(frame("initialize", bad_identity, "bad-identity")))
            ["error"]["data"]["name"],
            "INVALID_PARAMS",
        )

    def test_manifest_cannot_select_untrusted_execution_facts(self) -> None:
        data = manifest()
        data["paseo_cli_v1"]["shell"] = "/bin/sh"
        with self.assertRaises(ManifestResolutionError):
            TrustedManifestRegistry(data).resolve("paseo_cli_v1")

    def test_adapter_module_has_no_paseo_client_or_canonical_side_effects(self) -> None:
        source = (ROOT / "llm_collab" / "paseo_runtime_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("deliver.py", source)
        self.assertNotIn("PASEO_HOME", source)


if __name__ == "__main__":
    unittest.main()
