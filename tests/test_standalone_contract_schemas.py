"""Offline Draft 2020-12 conformance checks for the inert standalone catalog."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import posixpath
import re
import unicodedata
from pathlib import Path
import unittest

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
except ImportError as exc:  # canonical acceptance must fail, never skip
    raise RuntimeError(
        "Standalone schema validation requires jsonschema and referencing; "
        "run `pip install -r requirements-dev.txt`."
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "standalone" / "v1"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "standalone" / "v1"
CATALOG_ID = "https://llm-collab.dev/schemas/standalone/v1/index.json"
DRAFT_ID = "https://json-schema.org/draft/2020-12/schema"
WORKSPACE_ROOT = "/srv/workspaces"
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
EXPECTED_IDS = {
    name: f"https://llm-collab.dev/schemas/standalone/v1/{filename}"
    for name, filename in EXPECTED.items()
}
CATALOG_KEYS = {"schema_version", "catalog_id", "draft", "schemas"}
ACTIVATION_FIELDS = (
    "project",
    "chat",
    "task",
    "worktree",
    "branch",
    "target_agent",
)
TRUSTED_NATIVE_AUTHORITY = (
    "native_adapter",
    "r1",
    "native_delivery",
    "r1",
)


def reject_constant(value: str):
    raise ValueError(f"non-JSON numeric constant {value}")


def reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def strict_json_loads(text: str):
    return json.loads(
        text,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_pairs,
    )


def load(path: Path):
    return strict_json_loads(path.read_text(encoding="utf-8"))


def no_network(uri: str):
    raise LookupError(f"offline standalone schema registry has no resource for {uri}")


CATALOG = load(SCHEMA_DIR / "index.json")
SCHEMAS = {name: load(SCHEMA_DIR / filename) for name, filename in EXPECTED.items()}


def make_registry(schemas=SCHEMAS, catalog=CATALOG):
    resources = [
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
    ]
    resources.append(
        (
            catalog["catalog_id"],
            Resource.from_contents(
                catalog,
                default_specification=DRAFT202012,
            ),
        )
    )
    return Registry(retrieve=no_network).with_resources(resources)


REGISTRY = make_registry()


def iter_refs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                yield item
            else:
                yield from iter_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_refs(item)


def references_resolve(registry, schemas) -> bool:
    try:
        for schema in schemas.values():
            resolver = registry.resolver(base_uri=schema["$id"])
            for ref in iter_refs(schema):
                resolver.lookup(ref)
    except Exception:
        return False
    return True


def catalog_error(catalog, schemas, filenames=None):
    if not isinstance(catalog, dict) or set(catalog) != CATALOG_KEYS:
        return "catalog_shape"
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
        return "catalog_schema_version"
    if catalog["catalog_id"] != CATALOG_ID or not isinstance(catalog["catalog_id"], str):
        return "catalog_id"
    if catalog["draft"] != DRAFT_ID or not isinstance(catalog["draft"], str):
        return "catalog_draft"
    if not isinstance(catalog["schemas"], dict) or catalog["schemas"] != EXPECTED_IDS:
        return "catalog_mappings"
    ids = [schema.get("$id") for schema in schemas.values()]
    if len(ids) != len(set(ids)):
        return "duplicate_schema_id"
    if set(schemas) != set(EXPECTED):
        return "schema_name_set"
    if any(schemas[name].get("$id") != EXPECTED_IDS[name] for name in EXPECTED):
        return "schema_id_mapping"
    if filenames is not None and set(filenames) != set(EXPECTED.values()):
        return "schema_file_set"
    return None


def has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def strict_json_value(value, *, depth=0, max_depth=64) -> bool:
    if depth > max_depth:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return not has_surrogate(value)
    if isinstance(value, list):
        return all(
            strict_json_value(item, depth=depth + 1, max_depth=max_depth)
            for item in value
        )
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and not has_surrogate(key)
            and strict_json_value(item, depth=depth + 1, max_depth=max_depth)
            for key, item in value.items()
        )
    return False


def canonical_bytes(value):
    if not strict_json_value(value):
        raise ValueError("value is not strict JSON")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_without(value, field="integrity") -> str:
    projection = copy.deepcopy(value)
    projection.pop(field, None)
    return digest(projection)


def seal_state_evidence(evidence):
    evidence["integrity"] = f"sha256:{digest_without(evidence)}"
    return evidence


def state_evidence_error(evidence):
    try:
        if not isinstance(evidence, dict):
            return "evidence_type"
        if evidence.get("integrity") != f"sha256:{digest_without(evidence)}":
            return "evidence_integrity"
        if evidence.get("quality") == "best_effort" and evidence.get("state") in {
            "accepted",
            "completed",
        }:
            return "best_effort_positive_state"
        if evidence.get("state") in {"accepted", "completed"}:
            if evidence.get("evidence_kind") not in {
                "native_delivery_state",
                "exact_session_acknowledgment",
            }:
                return "positive_evidence_kind"
            authority = evidence.get("authority", {})
            if authority.get("authority_kind") not in {
                "native_runtime",
                "trusted_adapter",
            }:
                return "positive_authority_kind"
    except (KeyError, TypeError, ValueError, UnicodeError):
        return "evidence_malformed"
    return None


def canonical_absolute_path(value):
    return (
        isinstance(value, str)
        and value != "/"
        and value.startswith("/")
        and not value.startswith("//")
        and "//" not in value
        and posixpath.normpath(value) == value
        and not any(
            ord(character) < 32
            or ord(character) == 127
            or character in "\x85\u2028\u2029"
            for character in value
        )
    )


def canonical_relative_path(value):
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and "//" not in value
        and posixpath.normpath(value) == value
        and not any(
            ord(character) < 32
            or ord(character) == 127
            or character in "\x85\u2028\u2029"
            for character in value
        )
    )


def workspace_registry_error(workspace):
    try:
        projects = [item["project_id"] for item in workspace["projects"]]
        if len(projects) != len(set(projects)):
            return "duplicate_project_id"
        project_set = set(projects)
        repo_tuples = []
        repo_paths = []
        for repository in workspace["repositories"]:
            project_id = repository["project_id"]
            if project_id not in project_set:
                return "unregistered_repository_project"
            if not canonical_relative_path(repository["relative_path"]):
                return "repository_path_not_canonical"
            repo_tuples.append((project_id, repository["repo_id"]))
            repo_paths.append((project_id, repository["relative_path"]))
        if len(repo_tuples) != len(set(repo_tuples)):
            return "duplicate_repository_tuple"
        if len(repo_paths) != len(set(repo_paths)):
            return "duplicate_repository_path"
        repo_set = set(repo_tuples)
        relationship_ids = []
        relationship_tuples = []
        for relationship in workspace["relationships"]:
            relationship_ids.append(relationship["relationship_id"])
            source = (
                relationship["source"]["project_id"],
                relationship["source"]["repo_id"],
            )
            target = (
                relationship["target"]["project_id"],
                relationship["target"]["repo_id"],
            )
            if source not in repo_set or target not in repo_set:
                return "unresolved_relationship_endpoint"
            if relationship["registry_revision"] != workspace["registry_revision"]:
                return "stale_registry_revision"
            relationship_tuples.append(
                (relationship["relationship_type"], source, target)
            )
        if len(relationship_ids) != len(set(relationship_ids)):
            return "duplicate_relationship_id"
        if len(relationship_tuples) != len(set(relationship_tuples)):
            return "duplicate_relationship_tuple"
    except (KeyError, TypeError):
        return "registry_malformed"
    return None


def relationship_lookup(
    workspace,
    *,
    relationship_id=None,
    source=None,
    relationship_type=None,
):
    registry_error = workspace_registry_error(workspace)
    if registry_error:
        return registry_error
    active = [
        relationship
        for relationship in workspace["relationships"]
        if relationship["lifecycle"] == "active"
    ]
    if relationship_id is not None:
        matches = [
            relationship
            for relationship in active
            if relationship["relationship_id"] == relationship_id
        ]
    else:
        matches = [
            relationship
            for relationship in active
            if relationship["source"] == source
            and relationship["relationship_type"] == relationship_type
        ]
    if not matches:
        return "relationship_missing"
    if len(matches) != 1:
        return "relationship_ambiguous"
    return matches[0]


def repository_root(workspace, repo_ref, workspace_root=WORKSPACE_ROOT):
    matches = [
        repository
        for repository in workspace["repositories"]
        if repository["project_id"] == repo_ref.get("project_id")
        and repository["repo_id"] == repo_ref.get("repo_id")
    ]
    if len(matches) != 1:
        return None
    value = f"{workspace_root}/{matches[0]['relative_path']}"
    return value if canonical_absolute_path(value) else None


def binding_error(binding, outer_project, workspace, workspace_root=WORKSPACE_ROOT):
    try:
        if binding["project_id"] != outer_project:
            return "nested_project_mismatch"
        root = repository_root(workspace, binding, workspace_root)
        if root is None:
            return "repository_not_registered"
        cwd = binding["canonical_cwd"]
        if not canonical_absolute_path(cwd):
            return "canonical_cwd_malformed"
        if cwd != root and not cwd.startswith(f"{root}/"):
            return "canonical_cwd_outside_repository"
    except (KeyError, TypeError):
        return "repository_binding_malformed"
    return None


def scope_bundle_error(objects, workspace, workspace_root=WORKSPACE_ROOT):
    if workspace_registry_error(workspace):
        return "workspace_registry_invalid"
    project_set = {item["project_id"] for item in workspace["projects"]}
    for item in objects:
        try:
            if item["workspace_id"] != workspace["workspace_id"]:
                return "workspace_mismatch"
            scope = item["scope"]
            project_id = scope.get("project_id")
            if scope["kind"] == "project":
                if project_id not in project_set:
                    return "outer_project_unregistered"
            elif scope["kind"] == "workspace":
                if project_id is not None:
                    return "workspace_scope_has_project"
            else:
                return "scope_discriminator"
            evidence = item.get("evidence")
            if evidence is not None:
                if (
                    evidence.get("workspace_id") != item["workspace_id"]
                    or evidence.get("scope") != scope
                ):
                    return "nested_evidence_scope_mismatch"
            packet = item.get("activation_import")
            if packet is not None and packet.get("project") != project_id:
                return "activation_project_mismatch"
            binding = item.get("repository_binding")
            if binding is not None:
                error = binding_error(binding, project_id, workspace, workspace_root)
                if error:
                    return error
        except (KeyError, TypeError):
            return "scope_bundle_malformed"
    return None


def activation_error(
    message,
    expected_identity,
    claiming_target,
    workspace,
    repository_ref,
    workspace_root=WORKSPACE_ROOT,
):
    try:
        packet = message["activation_import"]
        required = {"activation", "to", *ACTIVATION_FIELDS}
        if set(packet) != required or packet["activation"] is not True:
            return "activation_shape"
        if any(not isinstance(packet[field], str) for field in (*ACTIVATION_FIELDS, "to")):
            return "activation_identity_type"
        if not canonical_absolute_path(packet["worktree"]):
            return "activation_worktree_not_canonical"
        for field in ACTIVATION_FIELDS:
            if packet[field] != expected_identity[field]:
                return f"activation_tuple_mismatch:{field}"
        if packet["to"] != claiming_target or packet["target_agent"] != claiming_target:
            return "activation_claiming_target_mismatch"
        if (
            message["workspace_id"] != workspace["workspace_id"]
            or message["scope"] != {
                "kind": "project",
                "project_id": packet["project"],
            }
        ):
            return "activation_outer_scope_mismatch"
        if workspace_registry_error(workspace):
            return "activation_registry_invalid"
        if repository_ref.get("project_id") != packet["project"]:
            return "activation_repository_project_mismatch"
        if repository_root(workspace, repository_ref, workspace_root) != packet["worktree"]:
            return "activation_worktree_registry_mismatch"
    except (KeyError, TypeError):
        return "activation_malformed"
    return None


FORBIDDEN_PAYLOAD_KEYS = {
    "project", "projects", "project_id", "project_home",
    "workspace", "workspaces", "workspace_id", "workspace_home", "workspace_root",
    "runtime_home", "runtime_home_id", "runtime_home_realpath",
    "native_target", "native_target_id", "native_thread", "native_thread_id",
    "native_session", "native_session_id", "session", "sessions", "session_ref",
    "session_ref_id", "chat", "chats", "chat_id", "task", "tasks", "task_id",
    "agent", "agents", "agent_id", "endpoint", "endpoints", "endpoint_id",
    "route", "routes", "routing", "target", "target_id", "target_agent",
    "recipient", "recipients", "handler", "handler_name",
    "handler_implementation", "adapter_implementation", "implementation",
    "capability_profile", "capability_profiles", "capability_profile_id",
    "command", "commands", "executable", "executables", "module", "modules",
    "tool", "tools", "url", "urls", "uri", "uris", "network", "environment",
    "env", "cwd", "path", "paths", "filesystem_root", "filesystem_roots",
    "file_root", "file_roots", "lease", "leases", "fence", "fencing",
    "fence_token", "retry", "retries", "retry_policy", "reconciliation",
    "reconcile", "reconciliation_policy", "retention", "retention_policy",
    "feature_flag", "feature_flags", "delivery", "deliveries", "delivery_state",
    "subscription_id", "subscription_revision", "received_at_utc",
    "receive_time", "content_hash",
}


def normalize_payload_key(key):
    normalized = unicodedata.normalize("NFKC", key)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    normalized = normalized.casefold().replace("-", "_").replace(".", "_")
    return re.sub(r"_+", "_", normalized).strip("_")


def payload_error(value, *, depth=0):
    if depth > 6:
        return "payload_depth"
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else "payload_non_json_number"
    if isinstance(value, str):
        if has_surrogate(value):
            return "payload_invalid_unicode"
        return None if len(value) <= 4096 else "payload_string_size"
    if isinstance(value, list):
        if len(value) > 32:
            return "payload_collection_size"
        for item in value:
            error = payload_error(item, depth=depth + 1)
            if error:
                return error
        return None
    if isinstance(value, dict):
        if len(value) > 32:
            return "payload_collection_size"
        for key, item in value.items():
            if not isinstance(key, str) or has_surrogate(key):
                return "payload_property_name"
            if not key or len(key) > 128:
                return "payload_property_name"
            normalized = normalize_payload_key(key)
            if normalized in FORBIDDEN_PAYLOAD_KEYS:
                return f"forbidden_payload_key:{normalized}"
            error = payload_error(item, depth=depth + 1)
            if error:
                return error
        return None
    return "payload_non_json_type"


def envelope_error(envelope):
    try:
        encoded = canonical_bytes(envelope)
        if len(encoded) > 64 * 1024:
            return "envelope_size"
        if len(envelope["subject"].encode("utf-8")) > 256:
            return "subject_utf8_size"
        if len(envelope["coalescing_key"].encode("utf-8")) > 256:
            return "coalescing_key_utf8_size"
        return payload_error(envelope["payload"])
    except (KeyError, TypeError, ValueError, UnicodeError):
        return "envelope_malformed"


def entry_key(entry):
    return (
        entry["canonical_locator"],
        entry["content_hash"],
        entry["evidence_form_version"],
        entry["cutoff_policy_revision"],
    )


def recalculate_legacy_integrity(evidence):
    for entry in evidence["legacy_manifest"]["entries"]:
        entry["integrity"] = digest_without(entry)
    publication = evidence["legacy_manifest"]["publication"]
    publication["integrity"] = digest_without(publication)
    manifest = evidence["legacy_manifest"]
    projection = {
        key: manifest[key]
        for key in (
            "manifest_id",
            "cutoff_policy_revision",
            "entries",
            "publication",
        )
    }
    manifest["seal"]["value"] = digest(projection)
    evidence["legacy_import"]["integrity"] = digest_without(
        evidence["legacy_import"]
    )
    seal_state_evidence(evidence)
    return evidence


def manifest_error(evidence, workspace):
    try:
        evidence_error = state_evidence_error(evidence)
        if evidence_error:
            return evidence_error
        manifest = evidence["legacy_manifest"]
        imported = evidence["legacy_import"]
        publication = manifest["publication"]
        outer_project = evidence["scope"]["project_id"]
        if (
            evidence["evidence_kind"] != "compatibility_import"
            or evidence["quality"] != "best_effort"
            or evidence["authority"]["authority_kind"] != "trusted_importer"
        ):
            return "legacy_quality_escalation"
        if (
            evidence["workspace_id"] != workspace["workspace_id"]
            or outer_project not in {
                project["project_id"] for project in workspace["projects"]
            }
        ):
            return "legacy_outer_scope"
        if manifest["sealed"] is not True or manifest["seal"]["algorithm"] != "sha256":
            return "manifest_not_sealed"
        if imported["manifest_id"] != manifest["manifest_id"]:
            return "manifest_id_mismatch"
        if (
            imported["cutoff_policy_revision"]
            != manifest["cutoff_policy_revision"]
            or imported["entry_key"]["cutoff_policy_revision"]
            != manifest["cutoff_policy_revision"]
        ):
            return "cutoff_revision_mismatch"
        if publication["workspace_id"] != evidence["workspace_id"]:
            return "publication_workspace_mismatch"
        if publication["project_id"] != outer_project:
            return "publication_project_mismatch"
        if publication["registry_revision"] != workspace["registry_revision"]:
            return "publication_registry_mismatch"
        if (
            publication["cutoff_policy_revision"]
            != manifest["cutoff_policy_revision"]
        ):
            return "publication_cutoff_mismatch"
        boundaries = [
            publication["source_boundary"],
            imported["source_boundary"],
            *[
                entry["source_boundary"]
                for entry in manifest["entries"]
            ],
        ]
        if any(boundary.get("immutable") is not True for boundary in boundaries):
            return "source_boundary_not_immutable"
        if publication["integrity"] != digest_without(publication):
            return "publication_integrity"
        keys = [entry_key(entry) for entry in manifest["entries"]]
        if len(keys) != len(set(keys)):
            return "duplicate_manifest_entry_key"
        imported_key = entry_key(imported["entry_key"])
        matches = [
            entry
            for entry in manifest["entries"]
            if entry_key(entry) == imported_key
        ]
        if len(matches) != 1:
            return "manifest_entry_lookup"
        entry = matches[0]
        if not canonical_absolute_path(entry["canonical_locator"]):
            return "legacy_locator_not_canonical"
        if (
            entry["source_workspace_id"] != evidence["workspace_id"]
            or entry["source_project_id"] != outer_project
            or entry["source_registry_revision"] != workspace["registry_revision"]
        ):
            return "entry_source_scope_mismatch"
        if entry["source_boundary"] != publication["source_boundary"]:
            return "entry_publication_boundary_mismatch"
        if entry["integrity"] != digest_without(entry):
            return "entry_integrity"
        projection = {
            key: manifest[key]
            for key in (
                "manifest_id",
                "cutoff_policy_revision",
                "entries",
                "publication",
            )
        }
        if manifest["seal"]["value"] != digest(projection):
            return "manifest_seal"
        if imported["entry_key"]["canonical_locator"] != evidence["subject"]["legacy_locator"]:
            return "enclosing_locator_mismatch"
        if imported["source_boundary"] != entry["source_boundary"]:
            return "import_source_boundary_mismatch"
        if imported["source_transaction_id"] != entry["transaction_id"]:
            return "source_transaction_mismatch"
        if imported["source_provenance_id"] != entry["provenance_id"]:
            return "source_provenance_mismatch"
        if imported["importer"] != entry["trusted_importer"]:
            return "trusted_importer_mismatch"
        authority = evidence["authority"]
        if (
            imported["importer"]["identity"] != authority["identity"]
            or imported["importer"]["revision"]
            != authority["implementation_revision"]
        ):
            return "enclosing_importer_mismatch"
        if imported["import_transaction_id"] in {
            entry["transaction_id"],
            publication["publication_transaction_id"],
        }:
            return "import_transaction_not_distinct"
        if imported["import_provenance_id"] in {
            entry["provenance_id"],
            publication["provenance_id"],
        }:
            return "import_provenance_not_distinct"
        if imported["integrity"] != digest_without(imported):
            return "import_integrity"
        recorded = base64.b64decode(
            imported["recorded_bytes_base64"],
            validate=True,
        )
        decoded = recorded.decode("utf-8")
        if imported["recorded_content_type"] == "application/json":
            strict_json_loads(decoded)
        if hashlib.sha256(recorded).hexdigest() != entry["content_hash"]:
            return "recorded_bytes_hash"
    except Exception:
        return "legacy_manifest_malformed"
    return None


def capability_set_error(capability_set):
    try:
        identities = [
            capability["capability"]
            for capability in capability_set["capabilities"]
        ]
        if len(identities) != len(set(identities)):
            return "duplicate_capability_identity"
        for capability in capability_set["capabilities"]:
            if capability["quality"] == "unsupported" and (
                "constraints" in capability or "evidence" in capability
            ):
                return "unsupported_positive_claim"
    except (KeyError, TypeError):
        return "capability_set_malformed"
    return None


def session_ref_error(
    session_ref,
    workspace,
    trusted_authority=(
        "native_adapter",
        "r1",
        "native_session_binding",
        "r1",
    ),
):
    try:
        if scope_bundle_error([session_ref], workspace):
            return "session_scope_binding"
        evidence = session_ref["evidence"]
        evidence_error = state_evidence_error(evidence)
        if evidence_error:
            return evidence_error
        if (
            evidence["evidence_kind"] != "exact_session_binding"
            or evidence["quality"] != "authoritative"
        ):
            return "session_evidence_kind"
        subject = evidence["subject"]
        expected = {
            "endpoint_id": session_ref["endpoint_id"],
            "session_ref_id": session_ref["session_ref_id"],
            "native_session_id": session_ref["native_session_id"],
            "repository_binding": session_ref["repository_binding"],
        }
        if subject != expected:
            return "session_subject_mismatch"
        authority = evidence["authority"]
        actual_authority = (
            authority["identity"],
            authority["implementation_revision"],
            authority["capability_profile_id"],
            authority["capability_profile_revision"],
        )
        if actual_authority != trusted_authority:
            return "session_authority_revision"
    except (KeyError, TypeError):
        return "session_ref_malformed"
    return None


def outcome_error(record, trusted_authority=TRUSTED_NATIVE_AUTHORITY):
    try:
        evidence = record["evidence"]
        evidence_error = state_evidence_error(evidence)
        if evidence_error:
            return evidence_error
        state = record.get("outcome", record.get("state"))
        if state in {
            "ambiguous",
            "deferred_busy",
            "rejected_before_acceptance",
            "pull_pending",
            "accepted",
            "completed",
        } and evidence["state"] != state:
            return "outcome_evidence_state"
        if (
            evidence["workspace_id"] != record["workspace_id"]
            or evidence["scope"] != record["scope"]
        ):
            return "outcome_evidence_scope"
        subject = evidence["subject"]
        for field in ("message_id", "delivery_id", "attempt_id", "endpoint_id"):
            if subject.get(field) != record[field]:
                return f"outcome_subject_mismatch:{field}"
        if evidence["correlation_id"] != record["attempt_id"]:
            return "outcome_attempt_correlation"
        if state in {"accepted", "completed"}:
            if subject.get("session_ref_id") != record.get("session_ref_id"):
                return "outcome_subject_mismatch:session_ref_id"
            authority = evidence["authority"]
            actual_authority = (
                authority["identity"],
                authority["implementation_revision"],
                authority["capability_profile_id"],
                authority["capability_profile_revision"],
            )
            if actual_authority != trusted_authority:
                return "outcome_authority_revision"
    except (KeyError, TypeError):
        return "outcome_malformed"
    return None


def make_delivery_outcome(outcome):
    delivery = load(FIXTURE_DIR / "valid" / "delivery.json")
    delivery["outcome"] = outcome
    delivery["evidence"]["state"] = outcome
    if outcome in {
        "ambiguous",
        "deferred_busy",
        "rejected_before_acceptance",
        "pull_pending",
    }:
        delivery["evidence"]["quality"] = "best_effort"
        delivery["evidence"]["evidence_kind"] = "adapter_observation"
        delivery["evidence"]["authority"]["authority_kind"] = "trusted_adapter"
        delivery.pop("session_ref_id", None)
        delivery["evidence"]["subject"].pop("session_ref_id", None)
    else:
        delivery["evidence"]["quality"] = "authoritative"
        delivery["evidence"]["evidence_kind"] = "native_delivery_state"
    seal_state_evidence(delivery["evidence"])
    return delivery


def make_receipt_state(state):
    receipt = load(FIXTURE_DIR / "valid" / "receipt.json")
    receipt["state"] = state
    receipt["evidence"]["state"] = state
    if state in {
        "ambiguous",
        "deferred_busy",
        "rejected_before_acceptance",
        "pull_pending",
    }:
        receipt["evidence"]["quality"] = "best_effort"
        receipt["evidence"]["evidence_kind"] = "adapter_observation"
        receipt["evidence"]["authority"]["authority_kind"] = "trusted_adapter"
        receipt.pop("session_ref_id", None)
        receipt["evidence"]["subject"].pop("session_ref_id", None)
    else:
        receipt["evidence"]["quality"] = "authoritative"
        receipt["evidence"]["evidence_kind"] = "exact_session_acknowledgment"
    seal_state_evidence(receipt["evidence"])
    return receipt


class StandaloneContractSchemaTest(unittest.TestCase):
    def validator(self, name):
        return Draft202012Validator(
            SCHEMAS[name],
            registry=REGISTRY,
            format_checker=FormatChecker(),
        )

    def errors(self, name, instance):
        return list(self.validator(name).iter_errors(instance))

    def assert_schema_rejects(self, name, instance, label):
        errors = self.errors(name, instance)
        self.assertTrue(errors, f"{name} accepted invalid {label}")

    def test_catalog_is_exact_data_resource_and_all_references_resolve_offline(self):
        filenames = [
            path.name for path in SCHEMA_DIR.glob("*.schema.json")
        ]
        self.assertIsNone(catalog_error(CATALOG, SCHEMAS, filenames))
        self.assertEqual(REGISTRY.contents(CATALOG_ID), CATALOG)
        self.assertEqual(len(filenames), 10)
        for schema in SCHEMAS.values():
            Draft202012Validator.check_schema(schema)
            self.assertEqual(REGISTRY.contents(schema["$id"]), schema)
        self.assertTrue(references_resolve(REGISTRY, SCHEMAS))

        mutations = []
        unknown = copy.deepcopy(CATALOG)
        unknown["required"] = ["schemas"]
        mutations.append((unknown, "catalog_shape"))
        missing = copy.deepcopy(CATALOG)
        missing.pop("draft")
        mutations.append((missing, "catalog_shape"))
        wrong_version = copy.deepcopy(CATALOG)
        wrong_version["schema_version"] = "1"
        mutations.append((wrong_version, "catalog_schema_version"))
        wrong_mapping = copy.deepcopy(CATALOG)
        wrong_mapping["schemas"]["MessageV1"] = EXPECTED_IDS["AgentV1"]
        mutations.append((wrong_mapping, "catalog_mappings"))
        missing_mapping = copy.deepcopy(CATALOG)
        missing_mapping["schemas"].pop("MessageV1")
        mutations.append((missing_mapping, "catalog_mappings"))
        for mutated, expected_error in mutations:
            with self.subTest(expected_error=expected_error):
                self.assertEqual(
                    catalog_error(mutated, SCHEMAS, filenames),
                    expected_error,
                )

        duplicate_ids = copy.deepcopy(SCHEMAS)
        duplicate_ids["MessageV1"]["$id"] = duplicate_ids["AgentV1"]["$id"]
        self.assertEqual(
            catalog_error(CATALOG, duplicate_ids, filenames),
            "duplicate_schema_id",
        )
        local_fragment = copy.deepcopy(SCHEMAS)
        local_fragment["MessageV1"]["properties"]["scope"]["$ref"] = (
            "#/$defs/not-present"
        )
        self.assertFalse(references_resolve(make_registry(local_fragment), local_fragment))
        missing_resource = copy.deepcopy(SCHEMAS)
        missing_resource["MessageV1"]["properties"]["scope"]["$ref"] = (
            "https://example.invalid/missing.schema.json"
        )
        self.assertFalse(
            references_resolve(make_registry(missing_resource), missing_resource)
        )

    def test_every_kind_has_valid_invalid_required_and_unknown_coverage(self):
        for name, filename in EXPECTED.items():
            stem = filename.removesuffix(".schema.json")
            valid = load(FIXTURE_DIR / "valid" / f"{stem}.json")
            invalid = load(FIXTURE_DIR / "invalid" / f"{stem}.json")
            with self.subTest(name=name, case="valid"):
                self.assertEqual(self.errors(name, valid), [])
            with self.subTest(name=name, case="invalid_fixture"):
                self.assert_schema_rejects(name, invalid, "fixture")
            required = SCHEMAS[name]["required"][1]
            missing = copy.deepcopy(valid)
            missing.pop(required)
            with self.subTest(name=name, case="required"):
                self.assert_schema_rejects(name, missing, f"missing {required}")
            unknown = copy.deepcopy(valid)
            unknown["unknown_semantic_field"] = True
            with self.subTest(name=name, case="unknown"):
                self.assert_schema_rejects(name, unknown, "unknown field")

    def test_ids_and_scope_discriminator_fail_closed(self):
        id_fields = {
            "WorkspaceV1": "workspace_id",
            "AgentV1": "agent_id",
            "EndpointV1": "endpoint_id",
            "SessionRefV1": "session_ref_id",
            "MessageV1": "message_id",
            "DeliveryV1": "delivery_id",
            "ReceiptV1": "receipt_id",
            "CapabilitySetV1": "capability_set_id",
            "StateEvidenceV1": "evidence_id",
            "EventEnvelopeV1": "source_event_id",
        }
        malformed = ("", "bad\ninjected", "bad\u0085injected", 7)
        for name, field in id_fields.items():
            filename = EXPECTED[name].removesuffix(".schema.json")
            valid = load(FIXTURE_DIR / "valid" / f"{filename}.json")
            for value in malformed:
                candidate = copy.deepcopy(valid)
                candidate[field] = value
                with self.subTest(name=name, field=field, value=repr(value)):
                    self.assert_schema_rejects(name, candidate, "malformed ID")

        agent = load(FIXTURE_DIR / "valid" / "agent.json")
        for scope in (
            {"kind": "project"},
            {"kind": "workspace", "project_id": "proj"},
            {"kind": "unknown"},
            {},
        ):
            candidate = copy.deepcopy(agent)
            candidate["scope"] = scope
            with self.subTest(scope=scope):
                self.assert_schema_rejects("AgentV1", candidate, "scope")

    def test_activation_exact_tuple_scope_registry_and_lexical_matrix(self):
        message = load(FIXTURE_DIR / "valid" / "message.json")
        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        expected = copy.deepcopy(message["activation_import"])
        expected.pop("activation")
        expected.pop("to")
        repository = {"project_id": "proj", "repo_id": "app"}
        self.assertIsNone(
            activation_error(
                message,
                expected,
                "codex",
                workspace,
                repository,
            )
        )
        self.assertEqual(message["activation_import"]["branch"], "feature/native-proof")

        for field in ACTIVATION_FIELDS:
            candidate = copy.deepcopy(message)
            candidate["activation_import"][field] = f"different-{field}"
            expected_error = (
                "activation_worktree_not_canonical"
                if field == "worktree"
                else f"activation_tuple_mismatch:{field}"
            )
            with self.subTest(field=field):
                self.assertEqual(
                    activation_error(
                        candidate,
                        expected,
                        "codex",
                        workspace,
                        repository,
                    ),
                    expected_error,
                )

        wrong_receiver = copy.deepcopy(message)
        wrong_receiver["activation_import"]["to"] = "Codex"
        self.assertEqual(
            activation_error(
                wrong_receiver,
                expected,
                "codex",
                workspace,
                repository,
            ),
            "activation_claiming_target_mismatch",
        )
        wrong_scope = copy.deepcopy(message)
        wrong_scope["scope"]["project_id"] = "other"
        self.assertEqual(
            activation_error(
                wrong_scope,
                expected,
                "codex",
                workspace,
                repository,
            ),
            "activation_outer_scope_mismatch",
        )
        wrong_repo = {"project_id": "proj", "repo_id": "docs"}
        self.assertEqual(
            activation_error(
                message,
                expected,
                "codex",
                workspace,
                wrong_repo,
            ),
            "activation_worktree_registry_mismatch",
        )

        for path in (
            "/",
            "/srv/./app",
            "/srv/../app",
            "/srv//app",
            "/srv/app/",
            "//srv/app",
        ):
            candidate = copy.deepcopy(message)
            candidate["activation_import"]["worktree"] = path
            with self.subTest(path=path):
                self.assert_schema_rejects(
                    "MessageV1",
                    candidate,
                    "non-canonical worktree",
                )
        for field, value in (
            ("branch", "release\ninjected"),
            ("chat", 4),
            ("target_agent", None),
            ("task", "   "),
        ):
            candidate = copy.deepcopy(message)
            candidate["activation_import"][field] = value
            with self.subTest(field=field):
                self.assert_schema_rejects(
                    "MessageV1",
                    candidate,
                    "activation identity type/control",
                )
        malformed = load(FIXTURE_DIR / "invalid" / "message.json")
        self.assert_schema_rejects(
            "MessageV1",
            malformed,
            "activation malformed-never-downgrades",
        )

    def test_scope_bundle_checks_nested_project_repository_and_evidence(self):
        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        objects = [
            load(FIXTURE_DIR / "valid" / f"{stem}.json")
            for stem in (
                "agent",
                "endpoint",
                "session-ref",
                "message",
                "delivery",
                "receipt",
                "capability-set",
                "state-evidence",
            )
        ]
        self.assertIsNone(scope_bundle_error(objects, workspace))
        cases = []
        mismatch = copy.deepcopy(objects)
        mismatch[0]["workspace_id"] = "ws_other"
        cases.append((mismatch, "workspace_mismatch"))
        mismatch = copy.deepcopy(objects)
        mismatch[3]["activation_import"]["project"] = "other"
        cases.append((mismatch, "activation_project_mismatch"))
        mismatch = copy.deepcopy(objects)
        mismatch[2]["repository_binding"]["project_id"] = "other"
        cases.append((mismatch, "nested_project_mismatch"))
        mismatch = copy.deepcopy(objects)
        mismatch[2]["repository_binding"]["repo_id"] = "missing"
        cases.append((mismatch, "repository_not_registered"))
        mismatch = copy.deepcopy(objects)
        mismatch[2]["repository_binding"]["canonical_cwd"] = "/other/repo"
        cases.append((mismatch, "canonical_cwd_outside_repository"))
        mismatch = copy.deepcopy(objects)
        mismatch[4]["evidence"]["scope"]["project_id"] = "other"
        seal_state_evidence(mismatch[4]["evidence"])
        cases.append((mismatch, "nested_evidence_scope_mismatch"))
        for candidate, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                self.assertEqual(
                    scope_bundle_error(candidate, workspace),
                    expected_error,
                )

    def test_workspace_registry_uniqueness_resolution_revision_and_lookup(self):
        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        source = {"project_id": "proj", "repo_id": "app"}
        target = {"project_id": "proj", "repo_id": "docs"}
        self.assertIsNone(workspace_registry_error(workspace))
        self.assertEqual(
            relationship_lookup(workspace, relationship_id="rel_docs")[
                "target"
            ],
            target,
        )
        self.assertEqual(
            relationship_lookup(workspace, relationship_id="rel_missing"),
            "relationship_missing",
        )
        self.assertEqual(
            relationship_lookup(
                workspace,
                source=target,
                relationship_type="documentation_companion",
            ),
            "relationship_missing",
        )

        cases = []
        duplicate = copy.deepcopy(workspace)
        duplicate["projects"].append({"project_id": "proj"})
        cases.append((duplicate, "duplicate_project_id"))
        duplicate = copy.deepcopy(workspace)
        duplicate["repositories"].append(
            {
                "project_id": "proj",
                "repo_id": "app",
                "relative_path": "repos/other",
            }
        )
        cases.append((duplicate, "duplicate_repository_tuple"))
        duplicate = copy.deepcopy(workspace)
        duplicate["repositories"].append(
            {
                "project_id": "proj",
                "repo_id": "other",
                "relative_path": "repos/app",
            }
        )
        cases.append((duplicate, "duplicate_repository_path"))
        unregistered = copy.deepcopy(workspace)
        unregistered["repositories"].append(
            {
                "project_id": "other",
                "repo_id": "app",
                "relative_path": "other/app",
            }
        )
        cases.append((unregistered, "unregistered_repository_project"))
        unresolved = copy.deepcopy(workspace)
        unresolved["relationships"][0]["target"]["repo_id"] = "missing"
        cases.append((unresolved, "unresolved_relationship_endpoint"))
        stale = copy.deepcopy(workspace)
        stale["relationships"][0]["registry_revision"] = "old"
        cases.append((stale, "stale_registry_revision"))
        duplicate = copy.deepcopy(workspace)
        duplicate["relationships"].append(
            copy.deepcopy(duplicate["relationships"][0])
        )
        cases.append((duplicate, "duplicate_relationship_id"))
        duplicate = copy.deepcopy(workspace)
        duplicate["relationships"].append(
            copy.deepcopy(duplicate["relationships"][0])
        )
        duplicate["relationships"][-1]["relationship_id"] = "rel_other"
        cases.append((duplicate, "duplicate_relationship_tuple"))
        for candidate, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                self.assertEqual(
                    workspace_registry_error(candidate),
                    expected_error,
                )

        for path in (
            "/absolute",
            ".",
            "..",
            "repos/./app",
            "repos/../app",
            "repos//app",
            "repos/app/",
        ):
            candidate = copy.deepcopy(workspace)
            candidate["repositories"][0]["relative_path"] = path
            with self.subTest(path=path):
                self.assert_schema_rejects(
                    "WorkspaceV1",
                    candidate,
                    "repository relative path",
                )

        ambiguous = copy.deepcopy(workspace)
        ambiguous["repositories"].append(
            {
                "project_id": "proj",
                "repo_id": "more_docs",
                "relative_path": "repos/more-docs",
            }
        )
        second = copy.deepcopy(ambiguous["relationships"][0])
        second["relationship_id"] = "rel_more"
        second["target"]["repo_id"] = "more_docs"
        ambiguous["relationships"].append(second)
        self.assertEqual(
            relationship_lookup(
                ambiguous,
                source=source,
                relationship_type="documentation_companion",
            ),
            "relationship_ambiguous",
        )

    def test_event_envelope_optional_time_forbidden_aliases_and_benign_keys(self):
        envelope = load(FIXTURE_DIR / "valid" / "event-envelope.json")
        envelope.pop("source_time_utc")
        self.assertEqual(self.errors("EventEnvelopeV1", envelope), [])
        self.assertIsNone(envelope_error(envelope))
        with_time = copy.deepcopy(envelope)
        with_time["source_time_utc"] = "2026-07-19T00:00:00Z"
        self.assertEqual(self.errors("EventEnvelopeV1", with_time), [])

        aliases = {
            "commands": "commands",
            "cwd": "cwd",
            "handler_name": "handler_name",
            "AdapterImplementation": "adapter_implementation",
            "implementation": "implementation",
            "retry-policy": "retry_policy",
            "delivery.state": "delivery_state",
            "filesystem-roots": "filesystem_roots",
            "NativeSessionID": "native_session_id",
            "subscription-id": "subscription_id",
        }
        for key, normalized in aliases.items():
            candidate = copy.deepcopy(envelope)
            candidate["payload"] = {"nested": [{"safe": {key: "unsafe"}}]}
            with self.subTest(key=key):
                self.assertEqual(
                    envelope_error(candidate),
                    f"forbidden_payload_key:{normalized}",
                )
                self.assert_schema_rejects(
                    "EventEnvelopeV1",
                    candidate,
                    "forbidden authority key",
                )

        benign = copy.deepcopy(envelope)
        benign["payload"] = {
            "project_summary": "benign",
            "retry_countdown": 2,
            "implementation_notes": "benign",
            "tooling_note": "benign",
            "pathology": "benign",
        }
        self.assertEqual(self.errors("EventEnvelopeV1", benign), [])
        self.assertIsNone(envelope_error(benign))

    def test_event_envelope_utf8_size_encoding_depth_and_collection_bounds(self):
        envelope = load(FIXTURE_DIR / "valid" / "event-envelope.json")
        unicode_bound = copy.deepcopy(envelope)
        unicode_bound["subject"] = "é" * 129
        self.assertEqual(self.errors("EventEnvelopeV1", unicode_bound), [])
        self.assertEqual(envelope_error(unicode_bound), "subject_utf8_size")
        unicode_bound = copy.deepcopy(envelope)
        unicode_bound["coalescing_key"] = "é" * 129
        self.assertEqual(
            envelope_error(unicode_bound),
            "coalescing_key_utf8_size",
        )
        oversized = copy.deepcopy(envelope)
        oversized["payload"] = {
            f"chunk_{index}": "a" * 4096
            for index in range(17)
        }
        self.assertEqual(self.errors("EventEnvelopeV1", oversized), [])
        self.assertEqual(envelope_error(oversized), "envelope_size")
        collection = copy.deepcopy(envelope)
        collection["payload"] = {"items": list(range(33))}
        self.assert_schema_rejects(
            "EventEnvelopeV1",
            collection,
            "collection cap",
        )
        self.assertEqual(envelope_error(collection), "payload_collection_size")
        deep = copy.deepcopy(envelope)
        value = "leaf"
        for _ in range(8):
            value = {"nested": value}
        deep["payload"] = {"root": value}
        self.assert_schema_rejects("EventEnvelopeV1", deep, "depth cap")
        self.assertEqual(envelope_error(deep), "payload_depth")
        long_name = copy.deepcopy(envelope)
        long_name["payload"] = {"x" * 129: True}
        self.assert_schema_rejects(
            "EventEnvelopeV1",
            long_name,
            "property-name cap",
        )
        self.assertEqual(envelope_error(long_name), "payload_property_name")
        invalid_unicode = copy.deepcopy(envelope)
        invalid_unicode["payload"] = {"text": "\ud800"}
        self.assert_schema_rejects(
            "EventEnvelopeV1",
            invalid_unicode,
            "invalid Unicode",
        )
        self.assertEqual(
            envelope_error(invalid_unicode),
            "envelope_malformed",
        )
        non_finite = copy.deepcopy(envelope)
        non_finite["payload"] = {"value": float("nan")}
        self.assertEqual(
            envelope_error(non_finite),
            "envelope_malformed",
        )

    def test_manifest_seal_bytes_scope_provenance_and_malformed_fail_closed(self):
        evidence = load(FIXTURE_DIR / "valid" / "state-evidence.json")
        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        self.assertIsNone(manifest_error(evidence, workspace))
        cases = []

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["seal"]["value"] = "f" * 64
        seal_state_evidence(candidate)
        cases.append((candidate, "manifest_seal"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["manifest_id"] = "manifest_other"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "manifest_id_mismatch"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["cutoff_policy_revision"] = "p2"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "cutoff_revision_mismatch"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["entries"].append(
            copy.deepcopy(candidate["legacy_manifest"]["entries"][0])
        )
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "duplicate_manifest_entry_key"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["publication"]["workspace_id"] = "ws_other"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "publication_workspace_mismatch"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["entries"][0]["source_project_id"] = "other"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "entry_source_scope_mismatch"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["source_boundary"]["identity"] = "snapshot2"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "import_source_boundary_mismatch"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["source_transaction_id"] = "other_tx"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "source_transaction_mismatch"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["source_provenance_id"] = "other_prov"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "source_provenance_mismatch"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["importer"]["identity"] = "other_importer"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "trusted_importer_mismatch"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["import_transaction_id"] = "source_tx1"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "import_transaction_not_distinct"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["import_provenance_id"] = "source_prov1"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "import_provenance_not_distinct"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["recorded_bytes_base64"] = (
            "eyJjbGFpbSI6ImRpZmZlcmVudCJ9"
        )
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "recorded_bytes_hash"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["entries"][0][
            "canonical_locator"
        ] = "/sealed/../evidence.json"
        candidate["legacy_import"]["entry_key"][
            "canonical_locator"
        ] = "/sealed/../evidence.json"
        candidate["subject"]["legacy_locator"] = "/sealed/../evidence.json"
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "legacy_locator_not_canonical"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["source_boundary"]["immutable"] = False
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "source_boundary_not_immutable"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["entries"][0]["integrity"] = "f" * 64
        seal_state_evidence(candidate)
        cases.append((candidate, "entry_integrity"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["publication"]["integrity"] = "f" * 64
        seal_state_evidence(candidate)
        cases.append((candidate, "publication_integrity"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["integrity"] = "f" * 64
        seal_state_evidence(candidate)
        cases.append((candidate, "import_integrity"))

        candidate = copy.deepcopy(evidence)
        candidate["integrity"] = "sha256:" + "f" * 64
        cases.append((candidate, "evidence_integrity"))

        for candidate, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                self.assertEqual(
                    manifest_error(candidate, workspace),
                    expected_error,
                )

        malformed_cases = []
        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["recorded_bytes_base64"] = "!!!!"
        recalculate_legacy_integrity(candidate)
        malformed_cases.append(candidate)
        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["recorded_bytes_base64"] = "ew=="
        recalculate_legacy_integrity(candidate)
        malformed_cases.append(candidate)
        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["recorded_bytes_base64"] = "/w=="
        recalculate_legacy_integrity(candidate)
        malformed_cases.append(candidate)
        for candidate in malformed_cases:
            self.assertEqual(
                manifest_error(candidate, workspace),
                "legacy_manifest_malformed",
            )
        for excluded_time_field in (
            "created_utc",
            "mtime",
            "produced_at_utc",
        ):
            timestamp_claim = copy.deepcopy(evidence)
            timestamp_claim["legacy_import"][excluded_time_field] = (
                "2020-01-01T00:00:00Z"
            )
            self.assert_schema_rejects(
                "StateEvidenceV1",
                timestamp_claim,
                f"{excluded_time_field} cutoff claim",
            )
        escalated = copy.deepcopy(evidence)
        escalated["quality"] = "authoritative"
        seal_state_evidence(escalated)
        self.assert_schema_rejects(
            "StateEvidenceV1",
            escalated,
            "retired evidence quality escalation",
        )
        self.assertEqual(
            manifest_error(escalated, workspace),
            "legacy_quality_escalation",
        )

    def test_delivery_outcomes_are_materially_valid_and_state_bound(self):
        for outcome in (
            "ambiguous",
            "deferred_busy",
            "rejected_before_acceptance",
            "pull_pending",
            "accepted",
            "completed",
        ):
            delivery = make_delivery_outcome(outcome)
            with self.subTest(outcome=outcome, case="valid"):
                self.assertEqual(self.errors("DeliveryV1", delivery), [])
                self.assertIsNone(outcome_error(delivery))
            mismatch = copy.deepcopy(delivery)
            mismatch["evidence"]["state"] = (
                (
                    "ambiguous"
                    if outcome != "ambiguous"
                    else "deferred_busy"
                )
                if outcome
                in {
                    "ambiguous",
                    "deferred_busy",
                    "rejected_before_acceptance",
                    "pull_pending",
                }
                else ("completed" if outcome == "accepted" else "accepted")
            )
            seal_state_evidence(mismatch["evidence"])
            with self.subTest(outcome=outcome, case="state_mismatch"):
                self.assert_schema_rejects(
                    "DeliveryV1",
                    mismatch,
                    "outcome/evidence state mismatch",
                )
                self.assertEqual(
                    outcome_error(mismatch),
                    "outcome_evidence_state",
                )

    def test_receipt_outcomes_are_materially_valid_and_state_bound(self):
        for state in (
            "ambiguous",
            "deferred_busy",
            "rejected_before_acceptance",
            "pull_pending",
            "accepted",
            "completed",
        ):
            receipt = make_receipt_state(state)
            with self.subTest(state=state, case="valid"):
                self.assertEqual(self.errors("ReceiptV1", receipt), [])
                self.assertIsNone(outcome_error(receipt))
            mismatch = copy.deepcopy(receipt)
            mismatch["evidence"]["state"] = (
                (
                    "ambiguous"
                    if state != "ambiguous"
                    else "deferred_busy"
                )
                if state
                in {
                    "ambiguous",
                    "deferred_busy",
                    "rejected_before_acceptance",
                    "pull_pending",
                }
                else ("completed" if state == "accepted" else "accepted")
            )
            seal_state_evidence(mismatch["evidence"])
            with self.subTest(state=state, case="state_mismatch"):
                self.assert_schema_rejects(
                    "ReceiptV1",
                    mismatch,
                    "receipt/evidence state mismatch",
                )
                self.assertEqual(
                    outcome_error(mismatch),
                    "outcome_evidence_state",
                )

    def test_positive_delivery_and_receipt_require_exact_authoritative_binding(self):
        delivery = load(FIXTURE_DIR / "valid" / "delivery.json")
        receipt = load(FIXTURE_DIR / "valid" / "receipt.json")
        self.assertIsNone(outcome_error(delivery))
        self.assertIsNone(outcome_error(receipt))

        for record, schema_name in (
            (delivery, "DeliveryV1"),
            (receipt, "ReceiptV1"),
        ):
            for field in (
                "message_id",
                "delivery_id",
                "attempt_id",
                "endpoint_id",
                "session_ref_id",
            ):
                candidate = copy.deepcopy(record)
                different_id = {
                    "message_id": "msg_other",
                    "delivery_id": "delivery_other",
                    "attempt_id": "attempt_other",
                    "endpoint_id": "endpoint_other",
                    "session_ref_id": "session_other",
                }[field]
                candidate["evidence"]["subject"][field] = different_id
                seal_state_evidence(candidate["evidence"])
                with self.subTest(schema=schema_name, field=field):
                    self.assertEqual(self.errors(schema_name, candidate), [])
                    self.assertEqual(
                        outcome_error(candidate),
                        f"outcome_subject_mismatch:{field}",
                    )
            candidate = copy.deepcopy(record)
            candidate["evidence"]["correlation_id"] = "attempt_other"
            seal_state_evidence(candidate["evidence"])
            self.assertEqual(
                outcome_error(candidate),
                "outcome_attempt_correlation",
            )
            candidate = copy.deepcopy(record)
            candidate["evidence"]["authority"][
                "capability_profile_revision"
            ] = "r2"
            seal_state_evidence(candidate["evidence"])
            self.assertEqual(
                outcome_error(candidate),
                "outcome_authority_revision",
            )

            best_effort = copy.deepcopy(record)
            best_effort["evidence"]["quality"] = "best_effort"
            seal_state_evidence(best_effort["evidence"])
            self.assert_schema_rejects(
                schema_name,
                best_effort,
                "best-effort positive outcome",
            )
            shell_exit = copy.deepcopy(record)
            shell_exit["evidence"]["evidence_kind"] = "shell_exit"
            seal_state_evidence(shell_exit["evidence"])
            self.assert_schema_rejects(
                schema_name,
                shell_exit,
                "shell-exit positive outcome",
            )
            self_asserted = copy.deepcopy(record)
            self_asserted["evidence"]["authority"][
                "authority_kind"
            ] = "self_asserted"
            seal_state_evidence(self_asserted["evidence"])
            self.assert_schema_rejects(
                schema_name,
                self_asserted,
                "self-asserted positive outcome",
            )

    def test_session_ref_requires_exact_native_repository_and_authority_binding(self):
        session_ref = load(FIXTURE_DIR / "valid" / "session-ref.json")
        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        self.assertIsNone(session_ref_error(session_ref, workspace))
        for field in ("endpoint_id", "session_ref_id", "native_session_id"):
            candidate = copy.deepcopy(session_ref)
            candidate["evidence"]["subject"][field] = f"different_{field}"
            seal_state_evidence(candidate["evidence"])
            with self.subTest(field=field):
                self.assertEqual(
                    session_ref_error(candidate, workspace),
                    "session_subject_mismatch",
                )
        candidate = copy.deepcopy(session_ref)
        candidate["evidence"]["subject"]["repository_binding"]["repo_id"] = (
            "docs"
        )
        seal_state_evidence(candidate["evidence"])
        self.assertEqual(
            session_ref_error(candidate, workspace),
            "session_subject_mismatch",
        )
        candidate = copy.deepcopy(session_ref)
        candidate["evidence"]["authority"]["implementation_revision"] = "r2"
        seal_state_evidence(candidate["evidence"])
        self.assertEqual(
            session_ref_error(candidate, workspace),
            "session_authority_revision",
        )
        best_effort = copy.deepcopy(session_ref)
        best_effort["evidence"]["quality"] = "best_effort"
        seal_state_evidence(best_effort["evidence"])
        self.assert_schema_rejects(
            "SessionRefV1",
            best_effort,
            "best-effort exact session",
        )

    def test_endpoint_and_capability_constraints_are_bounded_and_fail_closed(self):
        endpoint = load(FIXTURE_DIR / "valid" / "endpoint.json")
        capability_set = load(
            FIXTURE_DIR / "valid" / "capability-set.json"
        )
        self.assertEqual(self.errors("EndpointV1", endpoint), [])
        self.assertIsNone(capability_set_error(capability_set))
        self.assertEqual(endpoint["capability_set_id"], capability_set["capability_set_id"])

        for field in ("platform", "configuration_ref"):
            candidate = copy.deepcopy(endpoint)
            candidate.pop(field)
            self.assert_schema_rejects(
                "EndpointV1",
                candidate,
                f"missing {field}",
            )
        secret = copy.deepcopy(endpoint)
        secret["configuration_ref"]["secret"] = "copied-secret"
        self.assert_schema_rejects(
            "EndpointV1",
            secret,
            "copied secret",
        )
        extension = copy.deepcopy(endpoint)
        extension["extensions"] = {"x_command": "unsafe"}
        self.assert_schema_rejects(
            "EndpointV1",
            extension,
            "semantic extension",
        )

        for positive_field in ("constraints", "evidence"):
            unsupported = copy.deepcopy(capability_set)
            unsupported_capability = unsupported["capabilities"][-1]
            unsupported_capability[positive_field] = (
                {"access_mode": "read_write"}
                if positive_field == "constraints"
                else {
                    "evidence_kind": "profile_attestation",
                    "source_id": "native_adapter",
                    "source_revision": "r1",
                    "integrity": "sha256:" + "a" * 64,
                }
            )
            with self.subTest(positive_field=positive_field):
                self.assert_schema_rejects(
                    "CapabilitySetV1",
                    unsupported,
                    "unsupported positive claim",
                )
                self.assertEqual(
                    capability_set_error(unsupported),
                    "unsupported_positive_claim",
                )
        duplicate = copy.deepcopy(capability_set)
        duplicate["capabilities"].append(
            copy.deepcopy(duplicate["capabilities"][0])
        )
        duplicate["capabilities"][-1]["quality"] = "best_effort"
        duplicate["capabilities"][-1].pop("evidence")
        self.assertEqual(self.errors("CapabilitySetV1", duplicate), [])
        self.assertEqual(
            capability_set_error(duplicate),
            "duplicate_capability_identity",
        )

    def test_runtime_directories_do_not_consume_dev_validator_or_catalog(self):
        forbidden = ("jsonschema", "referencing", "schemas/standalone")
        for directory in (ROOT / "bin", ROOT / "scripts"):
            for path in directory.rglob("*"):
                if path.is_file():
                    text = path.read_bytes().decode("utf-8", errors="ignore")
                    self.assertFalse(
                        any(token in text for token in forbidden),
                        path,
                    )


if __name__ == "__main__":
    unittest.main()
