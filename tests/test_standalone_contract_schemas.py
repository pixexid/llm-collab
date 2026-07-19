"""Offline Draft 2020-12 conformance checks for the inert standalone catalog."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import unittest

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
except ImportError as exc:  # canonical acceptance must fail, never skip
    raise RuntimeError(
        "Standalone schema validation requires jsonschema and referencing; "
        "run `pip install -r requirements-dev.txt`."
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "standalone" / "v1"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "standalone" / "v1"
EXPECTED = {
    "WorkspaceV1": "workspace.schema.json",
    "AgentV1": "agent.schema.json",
    "EndpointV1": "endpoint.schema.json",
    "SessionRefV1": "session-ref.schema.json",
    "MessageV1": "message.schema.json",
    "DeliveryV1": "delivery.schema.json",
    "ReceiptV1": "receipt.schema.json",
    "CapabilitySetV1": "capability-set.schema.json",
    "StateEvidenceV1": "state-evidence.schema.json",
    "EventEnvelopeV1": "event-envelope.schema.json",
}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def no_network(uri: str):
    raise LookupError(f"offline standalone schema registry has no resource for {uri}")


CATALOG = load(SCHEMA_DIR / "index.json")
SCHEMAS = {name: load(SCHEMA_DIR / filename) for name, filename in EXPECTED.items()}
REGISTRY = Registry(retrieve=no_network).with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in SCHEMAS.values()
)


def semantic_activation(packet: dict) -> bool:
    return packet.get("activation") is True and packet.get("to") == packet.get("target_agent")


def semantic_envelope(envelope: dict) -> bool:
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > 64 * 1024:
        return False
    if len(envelope["subject"].encode("utf-8")) > 256 or len(envelope["coalescing_key"].encode("utf-8")) > 256:
        return False
    forbidden = {"project", "project_id", "workspace_id", "runtime_home", "chat", "task", "agent", "agent_id", "endpoint_id", "session_ref_id", "native_session_id", "handler", "adapter_implementation", "capability_profile", "command", "executable", "module", "tool", "url", "environment", "lease", "retry", "retention", "feature_flag", "delivery", "path"}
    def visit(value):
        if isinstance(value, dict):
            return not any(key in forbidden for key in value) and all(visit(item) for item in value.values())
        if isinstance(value, list):
            return all(visit(item) for item in value)
        return True
    return visit(envelope["payload"])


def semantic_manifest(manifest: dict, import_record: dict) -> bool:
    if manifest.get("sealed") is not True or import_record["manifest_id"] != manifest["manifest_id"]:
        return False
    keys = {(entry["canonical_locator"], entry["content_hash"], entry["evidence_form_version"], entry["cutoff_policy_revision"]) for entry in manifest["entries"]}
    key = import_record["entry_key"]
    imported_key = (key["canonical_locator"], key["content_hash"], key["evidence_form_version"], key["cutoff_policy_revision"])
    if len(keys) != len(manifest["entries"]) or imported_key not in keys:
        return False
    recorded = base64.b64decode(import_record["recorded_bytes_base64"], validate=True)
    return hashlib.sha256(recorded).hexdigest() == key["content_hash"]


def exact_scope_bundle(objects: list[dict]) -> bool:
    workspace_ids = {item["workspace_id"] for item in objects}
    project_ids = {
        item["scope"].get("project_id")
        for item in objects
        if item["scope"]["kind"] == "project"
    }
    return len(workspace_ids) == 1 and len(project_ids) <= 1


def relationship_lookup(workspace: dict, source: dict, relationship_type: str):
    registry = workspace["registry_revision"]
    repos = {(item["project_id"], item["repo_id"]) for item in workspace["repositories"]}
    matches = []
    seen = set()
    for relation in workspace["relationships"]:
        endpoint_tuple = (relation["source"]["project_id"], relation["source"]["repo_id"]), (relation["target"]["project_id"], relation["target"]["repo_id"])
        if relation["registry_revision"] != registry or any(point not in repos for point in endpoint_tuple):
            return "invalid_registry"
        key = (relation["relationship_type"], *endpoint_tuple)
        if key in seen:
            return "invalid_registry"
        seen.add(key)
        if relation["lifecycle"] == "active" and relation["relationship_type"] == relationship_type and relation["source"] == source:
            matches.append(relation)
    return matches[0] if len(matches) == 1 else ("relationship_missing" if not matches else "relationship_ambiguous")


class StandaloneContractSchemaTest(unittest.TestCase):
    def validator(self, name: str):
        return Draft202012Validator(SCHEMAS[name], registry=REGISTRY, format_checker=FormatChecker())

    def test_meta_validation_catalog_and_local_closure(self):
        self.assertEqual(CATALOG["schema_version"], 1)
        self.assertEqual(CATALOG["schemas"], {name: SCHEMAS[name]["$id"] for name in EXPECTED})
        self.assertEqual({path.name for path in SCHEMA_DIR.glob("*.schema.json")}, set(EXPECTED.values()))
        for schema in SCHEMAS.values():
            Draft202012Validator.check_schema(schema)
            self.assertEqual(REGISTRY.contents(schema["$id"]), schema)
            for ref in json.dumps(schema).split('"$ref": "')[1:]:
                target = ref.split('"', 1)[0]
                if not target.startswith("#"):
                    self.assertIn(target.split("#", 1)[0], CATALOG["schemas"].values())
        with self.assertRaises(LookupError):
            REGISTRY.get_or_retrieve("https://example.invalid/not-in-catalog")

    def test_every_catalog_kind_has_valid_and_invalid_fixture(self):
        for name, filename in EXPECTED.items():
            stem = filename.removesuffix(".schema.json")
            valid = load(FIXTURE_DIR / "valid" / f"{stem}.json")
            invalid = load(FIXTURE_DIR / "invalid" / f"{stem}.json")
            self.assertEqual(list(self.validator(name).iter_errors(valid)), [], name)
            self.assertNotEqual(list(self.validator(name).iter_errors(invalid)), [], name)

    def test_scope_and_cross_object_exact_identity(self):
        objects = [load(FIXTURE_DIR / "valid" / f"{stem}.json") for stem in ("agent", "endpoint", "session-ref", "message", "delivery", "receipt", "state-evidence")]
        self.assertEqual({item["workspace_id"] for item in objects}, {"ws_alpha"})
        self.assertEqual({item["scope"]["project_id"] for item in objects}, {"proj"})
        self.assertTrue(exact_scope_bundle(objects))
        mismatched = copy.deepcopy(objects[-1]); mismatched["scope"]["project_id"] = "other"
        self.assertFalse(exact_scope_bundle([*objects[:-1], mismatched]))

    def test_relationship_registry_is_exact_directed_and_fail_closed(self):
        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        source = {"project_id": "proj", "repo_id": "app"}
        target = {"project_id": "proj", "repo_id": "docs"}
        self.assertIsInstance(relationship_lookup(workspace, source, "documentation_companion"), dict)
        self.assertEqual(relationship_lookup(workspace, target, "documentation_companion"), "relationship_missing")
        ambiguous = copy.deepcopy(workspace); ambiguous["relationships"].append(copy.deepcopy(ambiguous["relationships"][0])); ambiguous["relationships"][-1]["relationship_id"] = "rel_other"
        self.assertEqual(relationship_lookup(ambiguous, source, "documentation_companion"), "invalid_registry")
        stale = copy.deepcopy(workspace); stale["relationships"][0]["registry_revision"] = "old"
        self.assertEqual(relationship_lookup(stale, source, "documentation_companion"), "invalid_registry")
        two_targets = copy.deepcopy(workspace)
        two_targets["repositories"].append({"project_id": "proj", "repo_id": "more_docs"})
        second = copy.deepcopy(two_targets["relationships"][0])
        second["relationship_id"] = "rel_more"
        second["target"]["repo_id"] = "more_docs"
        two_targets["relationships"].append(second)
        self.assertEqual(relationship_lookup(two_targets, source, "documentation_companion"), "relationship_ambiguous")

    def test_outcomes_remain_distinct_and_non_success_is_not_collapsed(self):
        delivery = SCHEMAS["DeliveryV1"]
        outcomes = delivery["properties"]["outcome"]["enum"]
        required = {"ambiguous", "deferred_busy", "rejected_before_acceptance", "pull_pending", "accepted", "completed"}
        self.assertTrue(required.issubset(outcomes))
        self.assertEqual(len(outcomes), len(set(outcomes)))
        self.assertNotEqual("ambiguous", "completed")

    def test_activation_receiver_is_byte_exact_and_malformed_is_invalid(self):
        message = load(FIXTURE_DIR / "valid" / "message.json")
        self.assertTrue(semantic_activation(message["activation_import"]))
        wrong_receiver = copy.deepcopy(message); wrong_receiver["activation_import"]["to"] = "Codex"
        self.assertEqual(list(self.validator("MessageV1").iter_errors(wrong_receiver)), [])
        self.assertFalse(semantic_activation(wrong_receiver["activation_import"]))
        malformed = load(FIXTURE_DIR / "invalid" / "message.json")
        self.assertNotEqual(list(self.validator("MessageV1").iter_errors(malformed)), [])

    def test_sealed_manifest_hash_provenance_and_entry_uniqueness(self):
        evidence = load(FIXTURE_DIR / "valid" / "state-evidence.json")
        manifest = evidence["legacy_manifest"]
        imported = evidence["legacy_import"]
        self.assertTrue(semantic_manifest(manifest, imported))
        replaced = copy.deepcopy(imported); replaced["recorded_bytes_base64"] = "Yg=="
        self.assertFalse(semantic_manifest(manifest, replaced))
        unsealed = copy.deepcopy(manifest); unsealed["sealed"] = False
        self.assertNotEqual(list(self.validator("StateEvidenceV1").iter_errors({**evidence, "legacy_manifest": unsealed})), [])
        entries = [manifest["entries"][0], copy.deepcopy(manifest["entries"][0])]
        keys = {(e["canonical_locator"], e["content_hash"], e["evidence_form_version"], e["cutoff_policy_revision"]) for e in entries}
        self.assertNotEqual(len(entries), len(keys))
        duplicate = copy.deepcopy(manifest); duplicate["entries"].append(copy.deepcopy(duplicate["entries"][0]))
        self.assertFalse(semantic_manifest(duplicate, imported))
        no_match = copy.deepcopy(imported); no_match["entry_key"]["content_hash"] = "b" * 64
        self.assertFalse(semantic_manifest(manifest, no_match))

    def test_event_envelope_is_data_only_and_enforces_utf8_and_serialized_bounds(self):
        envelope = load(FIXTURE_DIR / "valid" / "event-envelope.json")
        self.assertTrue(semantic_envelope(envelope))
        authority = copy.deepcopy(envelope); authority["payload"]["command"] = "unsafe"
        self.assertFalse(semantic_envelope(authority))
        unicode_bound = copy.deepcopy(envelope); unicode_bound["subject"] = "é" * 129
        self.assertEqual(list(self.validator("EventEnvelopeV1").iter_errors(unicode_bound)), [])
        self.assertFalse(semantic_envelope(unicode_bound))
        oversized = copy.deepcopy(envelope); oversized["payload"]["body"] = "a" * (64 * 1024)
        self.assertFalse(semantic_envelope(oversized))

    def test_runtime_directories_do_not_consume_dev_validator_or_catalog(self):
        forbidden = ("jsonschema", "referencing", "schemas/standalone")
        for directory in (ROOT / "bin", ROOT / "scripts"):
            for path in directory.rglob("*"):
                if path.is_file():
                    text = path.read_bytes().decode("utf-8", errors="ignore")
                    self.assertFalse(any(token in text for token in forbidden), path)


if __name__ == "__main__":
    unittest.main()
