import json
import unittest
from pathlib import Path

from scripts.paseo_phase0_probe import (
    classify_lifecycle,
    classify_transport,
    correlation_ids,
    has_stable_correlation,
    parse_json_document,
    parse_mixed_json,
    require_full_agent_id,
)


FIXTURE = Path(__file__).parent / "fixtures" / "paseo_phase0_v0_2_5.json"


class PaseoPhase0ProbeTests(unittest.TestCase):
    def test_json_parsers_reject_trailing_data(self):
        self.assertEqual(parse_json_document('{"status":"idle"}\n'), {"status": "idle"})
        self.assertEqual(parse_mixed_json("Created workspace <REDACTED>\n{\"status\":\"sent\"}\n"), {"status": "sent"})
        with self.assertRaises(json.JSONDecodeError):
            parse_json_document('{"status":"idle"} trailing')
        with self.assertRaises(ValueError):
            parse_mixed_json('{"status":"idle"} trailing')
        with self.assertRaises(ValueError):
            parse_mixed_json('garbage before JSON\n{"status":"idle"}')

    def test_full_id_is_required(self):
        value = "00000000-0000-4000-8000-000000000001"
        self.assertEqual(require_full_agent_id(value), value)
        for invalid in ("cf9536e", "c", "<REDACTED_AGENT_ID>", ""):
            with self.assertRaises(ValueError):
                require_full_agent_id(invalid)

    def test_lifecycle_classification_fails_closed(self):
        self.assertEqual(classify_lifecycle({"Status": "running", "PendingPermissions": []}), "running")
        self.assertEqual(classify_lifecycle({"status": "idle", "pendingPermissions": []}), "idle")
        self.assertEqual(classify_lifecycle({"Status": "running", "PendingPermissions": ["p"]}), "permission")
        self.assertEqual(classify_lifecycle({"error": {"code": "AGENT_CREATE_FAILED"}}), "error")
        self.assertEqual(classify_lifecycle({"Status": "unexpected"}), "unknown")

    def test_transport_boundary_and_correlation(self):
        self.assertEqual(
            classify_transport({"error": {"code": "DAEMON_NOT_RUNNING"}}),
            "rejected_before_submission",
        )
        self.assertEqual(classify_transport({"status": "sent"}), "submitted_best_effort")
        self.assertEqual(classify_transport({"status": "completed"}), "native_completed_best_effort")
        self.assertEqual(classify_transport({"status": "timeout"}), "acceptance_unknown")
        self.assertEqual(correlation_ids({"agentId": "<REDACTED_AGENT_ID>", "status": "sent"}), {})
        self.assertFalse(has_stable_correlation({"agentId": "<REDACTED_AGENT_ID>"}, []))
        self.assertTrue(
            has_stable_correlation(
                {"agentId": "<REDACTED_AGENT_ID>", "runId": "<REDACTED_RUN_ID>"},
                [{"runId": "<REDACTED_RUN_ID>"}],
            )
        )

    def test_fixture_is_versioned_and_redacted(self):
        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(fixture["version"], "0.2.5")
        self.assertEqual(fixture["capture"]["provider"], "pi")
        self.assertFalse(fixture["capture"]["llm_collab_pi_worker_reused"])
        raw = FIXTURE.read_text()
        for forbidden in ("/Users/", "/private/tmp/", "relay", "token", "credential"):
            self.assertNotIn(forbidden, raw.lower())
        self.assertFalse(fixture["transport"]["correlation"]["stable_exact_per_send_id"])


if __name__ == "__main__":
    unittest.main()
