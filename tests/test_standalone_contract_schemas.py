"""Offline Draft 2020-12 conformance checks for the inert standalone catalog."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import posixpath
import re
import sys
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
sys.path.insert(0, str(ROOT / "bin"))

from _activation_identity import (  # noqa: E402 - import current v2 authority
    frontmatter_roundtrips,
    lease_identity,
    normalized_identity_field,
)


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
SAFE_INTEGER_MAX = 9007199254740991
CANONICAL_JSON_ALGORITHM = (
    "UTF-8; object keys sorted by Unicode code-point order; array order "
    "preserved; comma/colon separators and no insignificant whitespace; "
    "exact unnormalized Unicode scalar strings with JSON escapes only for "
    "quote and backslash; lowercase true/false/null; "
    "integers only in [-9007199254740991,9007199254740991]; duplicate keys, "
    "integer lexical -0 normalized to JSON integer 0 before hashing; "
    "surrogates, C0/DEL/NEL/U+2028/U+2029, floats, exponents, NaN, and "
    "Infinity rejected"
)
TRUSTED_COMPATIBILITY_POLICY = {
    "manifest_id": "manifest_one",
    "cutoff_policy_revision": "p1",
    "workspace_id": "ws_alpha",
    "project_id": "proj",
    "registry_revision": "rev1",
    "source_boundary": {
        "kind": "source_snapshot",
        "identity": "snapshot1",
        "immutable": True,
    },
    "publisher": ("publisher", "r1"),
    "importer": ("importer", "r1"),
    "authority_profile": ("compatibility_reader", "p1"),
    "entry_keys": (
        (
            "/sealed/evidence.json",
            "984845d2117bd645aba91ea1c0fc3993bf0f5086570d162f1a970655bba5a1fb",
            "v1",
            "p1",
        ),
        (
            "/sealed/non-selected.json",
            "924224abc36c26736e79bf6feefbd842b00db9e618116b21e4637ac2c8c8d01f",
            "v1",
            "p1",
        ),
    ),
    "manifest_seal": "aa7c1413b87f3b726af230611646153f9e06e7b4f1b9593711450f63d2dfbabf",
}
TRUSTED_GRAPH_CATALOG = {
    "endpoint_registrations": {
        "endpoint_one": {
            "agent_id": "agent_codex",
            "capability_set_id": "caps_one",
            "adapter": ("native_adapter", "r1"),
            "configuration_ref": (
                "endpoint_registry",
                "r1",
                "endpoint_one_config",
            ),
        }
    },
    "adapter_profiles": {
        ("native_adapter", "r1"): {
            ("native_session_binding", "r1"),
            ("native_delivery", "r1"),
        }
    },
    "compatibility_policy": TRUSTED_COMPATIBILITY_POLICY,
}
STATE_EVIDENCE_STATES = (
    "persisted",
    "routed",
    "injected",
    "visible",
    "accepted",
    "processing",
    "acknowledged",
    "completed",
    "rejected_before_acceptance",
    "ambiguous",
    "pull_pending",
    "deferred_busy",
)
PENDING_DELIVERY_STATES = {
    "persisted",
    "routed",
    "injected",
    "visible",
    "processing",
    "acknowledged",
}


def reject_constant(value: str):
    raise ValueError(f"non-JSON numeric constant {value}")


def reject_float(value: str):
    raise ValueError(f"non-canonical JSON number {value}")


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
        parse_float=reject_float,
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


def has_forbidden_json_character(value: str) -> bool:
    return has_surrogate(value) or any(
        ord(character) < 32
        or ord(character) == 127
        or character in "\x85\u2028\u2029"
        for character in value
    )


def strict_json_value(value, *, depth=0, max_depth=64) -> bool:
    if depth > max_depth:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -SAFE_INTEGER_MAX <= value <= SAFE_INTEGER_MAX
    if isinstance(value, float):
        return False
    if isinstance(value, str):
        return not has_forbidden_json_character(value)
    if isinstance(value, list):
        return all(
            strict_json_value(item, depth=depth + 1, max_depth=max_depth)
            for item in value
        )
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and not has_forbidden_json_character(key)
            and strict_json_value(item, depth=depth + 1, max_depth=max_depth)
            for key, item in value.items()
        )
    return False


def extension_error(value):
    try:
        if isinstance(value, list):
            for item in value:
                error = extension_error(item)
                if error:
                    return error
            return None
        if not isinstance(value, dict):
            return None
        if "extensions" in value:
            extensions = value["extensions"]
            if not isinstance(extensions, dict) or len(extensions) > 8:
                return "extension_shape"
            for key, item in extensions.items():
                if not re.fullmatch(r"x_note_[A-Za-z][A-Za-z0-9_-]{0,55}", key):
                    return "extension_name"
                if item is None or isinstance(item, bool):
                    continue
                if type(item) is int:
                    if -SAFE_INTEGER_MAX <= item <= SAFE_INTEGER_MAX:
                        continue
                    return "extension_integer_range"
                if isinstance(item, str):
                    if len(item) <= 512 and not has_forbidden_json_character(item):
                        continue
                    return "extension_string"
                return "extension_scalar"
        for item in value.values():
            error = extension_error(item)
            if error:
                return error
    except Exception:
        return "extension_malformed"
    return None


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
        legacy_fields = {"legacy_manifest", "legacy_import"}
        present_legacy_fields = legacy_fields.intersection(evidence)
        if evidence.get("evidence_kind") == "compatibility_import":
            if present_legacy_fields != legacy_fields:
                return "compatibility_legacy_fields"
            if "legacy_locator" not in evidence.get("subject", {}):
                return "compatibility_legacy_locator"
            if (
                evidence.get("quality") != "best_effort"
                or evidence.get("authority", {}).get("authority_kind")
                != "trusted_importer"
            ):
                return "legacy_quality_escalation"
        elif present_legacy_fields:
            return "unexpected_legacy_fields"
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
        if workspace["scope"] != {"kind": "workspace"}:
            return "workspace_scope"
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
        if not isinstance(outer_project, str):
            return "repository_binding_requires_project_scope"
        if binding["project_id"] != outer_project:
            return "nested_project_mismatch"
        root = repository_root(workspace, binding, workspace_root)
        if root is None:
            return "repository_not_registered"
        other_project_roots = {
            repository_root(
                workspace,
                {
                    "project_id": repository["project_id"],
                    "repo_id": repository["repo_id"],
                },
                workspace_root,
            )
            for repository in workspace["repositories"]
            if repository["project_id"] != outer_project
        }
        cwd = binding["canonical_cwd"]
        if not canonical_absolute_path(cwd):
            return "canonical_cwd_malformed"
        if cwd != root and not cwd.startswith(f"{root}/"):
            return "canonical_cwd_outside_repository"
        if any(
            other_root is not None
            and (
                other_root == root
                or cwd == other_root
                or cwd.startswith(f"{other_root}/")
            )
            for other_root in other_project_roots
        ):
            return "repository_binding_cross_project_alias"
    except (KeyError, TypeError):
        return "repository_binding_malformed"
    return None


def scope_bundle_error(objects, workspace, workspace_root=WORKSPACE_ROOT):
    if workspace_registry_error(workspace):
        return "workspace_registry_invalid"
    project_set = {item["project_id"] for item in workspace["projects"]}
    graph_projects = set()
    for item in objects:
        try:
            if item["workspace_id"] != workspace["workspace_id"]:
                return "workspace_mismatch"
            scope = item["scope"]
            project_id = scope.get("project_id")
            if scope["kind"] == "project":
                if project_id not in project_set:
                    return "outer_project_unregistered"
                graph_projects.add(project_id)
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
    if len(graph_projects) != 1:
        return "project_identity_count"
    return None


def activation_error(
    message,
    expected_identity,
    claiming_target,
):
    try:
        packet = message["activation_import"]
        required = {"activation", "to", *ACTIVATION_FIELDS}
        if set(packet) != required or packet["activation"] is not True:
            return "activation_shape"
        if any(not isinstance(packet[field], str) for field in (*ACTIVATION_FIELDS, "to")):
            return "activation_identity_type"
        for field in (*ACTIVATION_FIELDS, "to"):
            if field != "worktree" and is_decimal_integer_lexical(packet[field]):
                return f"activation_noncanonical_serialized:{field}"
        identity = lease_identity(packet)
        for field in ACTIVATION_FIELDS:
            if identity[field] != packet[field]:
                return f"activation_noncanonical_serialized:{field}"
        canonical_to = normalized_identity_field("target_agent", packet["to"])
        if canonical_to != packet["to"]:
            return "activation_noncanonical_serialized:to"
        for field in ACTIVATION_FIELDS:
            if packet[field] != expected_identity[field]:
                return f"activation_tuple_mismatch:{field}"
        if packet["to"] != claiming_target or packet["target_agent"] != claiming_target:
            return "activation_claiming_target_mismatch"
        if (
            message["scope"] != {
                "kind": "project",
                "project_id": packet["project"],
            }
        ):
            return "activation_outer_scope_mismatch"
    except (KeyError, TypeError, ValueError):
        return "activation_malformed"
    return None


def is_decimal_integer_lexical(value):
    """Recognize signed Unicode-decimal integer text with single underscores."""
    if not isinstance(value, str):
        return False
    body = value[1:] if value.startswith(("+", "-")) else value
    if not body:
        return False
    needs_digit = True
    for character in body:
        if character == "_":
            if needs_digit:
                return False
            needs_digit = True
        elif character.isdecimal():
            needs_digit = False
        else:
            return False
    return not needs_digit


FORBIDDEN_PAYLOAD_KEYS = {
    "project", "projects", "project_id", "project_home",
    "workspace", "workspaces", "workspace_id", "workspace_home", "workspace_root",
    "runtime_home", "runtime_home_id", "runtime_home_realpath",
    "native_target", "native_target_id", "native_thread", "native_thread_id",
    "native_session", "native_session_id", "session", "sessions", "session_ref",
    "session_ref_id", "chat", "chats", "chat_id", "task", "tasks", "task_id",
    "agent", "agents", "agent_id", "endpoint", "endpoints", "endpoint_id",
    "route", "routes", "routing", "target", "target_id", "target_agent",
    "recipient", "recipients", "adapter", "adapter_name", "adapter_revision",
    "adapter_version", "handler", "handler_name", "handler_version",
    "handler_revision", "handler_implementation", "adapter_implementation",
    "implementation", "capability_profile", "capability_profiles",
    "capability_profile_id", "capability_profile_revision", "profile_id",
    "profile_revision",
    "command", "commands", "executable", "executables", "module", "modules",
    "tool", "tools", "url", "urls", "uri", "uris", "network", "environment",
    "env", "cwd", "path", "paths", "filesystem_root", "filesystem_roots",
    "file_root", "file_roots", "lease", "leases", "fence", "fencing",
    "fence_token", "retry", "retries", "retry_policy", "reconciliation",
    "reconcile", "reconciliation_policy", "retention", "retention_policy",
    "feature_flag", "feature_flags", "policy", "policies", "routing_policy",
    "delivery_policy", "lease_policy", "fencing_policy", "capability_policy",
    "feature_policy", "delivery", "deliveries", "delivery_state",
    "subscription_id", "subscription_revision", "received_at_utc",
    "receive_time", "receive_time_utc", "content_hash", "envelope_hash",
    "identity", "exact_identity", "revision", "registry_revision",
}
FORBIDDEN_PAYLOAD_COMPACT_KEYS = {
    key.replace("_", ""): key
    for key in FORBIDDEN_PAYLOAD_KEYS
}


def normalize_payload_key(key):
    normalized = unicodedata.normalize("NFKC", key)
    normalized = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", normalized)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    normalized = normalized.casefold()
    return re.sub(r"[\W_]+", "_", normalized).strip("_")


def canonical_payload_authority_key(key):
    normalized = normalize_payload_key(key)
    if normalized in FORBIDDEN_PAYLOAD_KEYS:
        return normalized
    return FORBIDDEN_PAYLOAD_COMPACT_KEYS.get(normalized.replace("_", ""))


def payload_error(value, *, depth=0):
    if depth > 6:
        return "payload_depth"
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return (
            None
            if -SAFE_INTEGER_MAX <= value <= SAFE_INTEGER_MAX
            else "payload_integer_range"
        )
    if isinstance(value, float):
        return "payload_non_canonical_number"
    if isinstance(value, str):
        if has_forbidden_json_character(value):
            return "payload_invalid_unicode_or_control"
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
            if not isinstance(key, str) or has_forbidden_json_character(key):
                return "payload_property_name"
            if not key or len(key) > 128:
                return "payload_property_name"
            authority_key = canonical_payload_authority_key(key)
            if authority_key is not None:
                return f"forbidden_payload_key:{authority_key}"
            error = payload_error(item, depth=depth + 1)
            if error:
                return error
        return None
    return "payload_non_json_type"


def envelope_error(envelope):
    try:
        payload_result = payload_error(envelope["payload"])
        if payload_result:
            return payload_result
        encoded = canonical_bytes(envelope)
        if len(encoded) > 64 * 1024:
            return "envelope_size"
        if len(envelope["subject"].encode("utf-8")) > 256:
            return "subject_utf8_size"
        if len(envelope["coalescing_key"].encode("utf-8")) > 256:
            return "coalescing_key_utf8_size"
        return None
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


def manifest_error(evidence, workspace, trusted_policy):
    try:
        evidence_error = state_evidence_error(evidence)
        if evidence_error:
            return evidence_error
        manifest = evidence["legacy_manifest"]
        imported = evidence["legacy_import"]
        publication = manifest["publication"]
        authority = evidence["authority"]
        outer_project = evidence["scope"]["project_id"]
        if (
            evidence["evidence_kind"] != "compatibility_import"
            or evidence["quality"] != "best_effort"
            or authority["authority_kind"] != "trusted_importer"
        ):
            return "legacy_quality_escalation"
        if (
            evidence["workspace_id"] != trusted_policy["workspace_id"]
            or evidence["workspace_id"] != workspace["workspace_id"]
            or outer_project != trusted_policy["project_id"]
            or outer_project
            not in {project["project_id"] for project in workspace["projects"]}
        ):
            return "legacy_outer_scope"
        if (
            manifest["manifest_id"] != trusted_policy["manifest_id"]
            or manifest["cutoff_policy_revision"]
            != trusted_policy["cutoff_policy_revision"]
        ):
            return "untrusted_manifest_policy"
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
        if publication["workspace_id"] != trusted_policy["workspace_id"]:
            return "publication_workspace_mismatch"
        if publication["project_id"] != trusted_policy["project_id"]:
            return "publication_project_mismatch"
        if (
            publication["registry_revision"]
            != trusted_policy["registry_revision"]
            or publication["registry_revision"] != workspace["registry_revision"]
        ):
            return "publication_registry_mismatch"
        if (
            publication["cutoff_policy_revision"]
            != manifest["cutoff_policy_revision"]
        ):
            return "publication_cutoff_mismatch"
        if publication["source_boundary"] != trusted_policy["source_boundary"]:
            return "publication_boundary_authority"
        publisher = publication["publisher"]
        if (
            publisher["identity"],
            publisher["revision"],
        ) != trusted_policy["publisher"]:
            return "untrusted_manifest_publisher"
        if publication["integrity"] != digest_without(publication):
            return "publication_integrity"
        keys = []
        entry_transactions = set()
        entry_provenances = set()
        for entry in manifest["entries"]:
            key = entry_key(entry)
            keys.append(key)
            if (
                key[3] != trusted_policy["cutoff_policy_revision"]
                or not canonical_absolute_path(key[0])
            ):
                return "manifest_entry_key"
            if (
                entry["source_workspace_id"] != trusted_policy["workspace_id"]
                or entry["source_project_id"] != trusted_policy["project_id"]
                or entry["source_registry_revision"]
                != trusted_policy["registry_revision"]
            ):
                return "entry_source_scope_mismatch"
            if entry["source_boundary"] != trusted_policy["source_boundary"]:
                return "entry_publication_boundary_mismatch"
            importer_authority = entry["trusted_importer"]
            if (
                importer_authority["identity"],
                importer_authority["revision"],
            ) != trusted_policy["importer"]:
                return "entry_untrusted_importer"
            if entry["integrity"] != digest_without(entry):
                return "entry_integrity"
            entry_transactions.add(entry["transaction_id"])
            entry_provenances.add(entry["provenance_id"])
        if len(keys) != len(set(keys)):
            return "duplicate_manifest_entry_key"
        if tuple(keys) != trusted_policy["entry_keys"]:
            return "manifest_entry_set"
        imported_key = entry_key(imported["entry_key"])
        matches = [
            entry
            for entry in manifest["entries"]
            if entry_key(entry) == imported_key
        ]
        if len(matches) != 1:
            return "manifest_entry_lookup"
        entry = matches[0]
        projection = {
            key: manifest[key]
            for key in (
                "manifest_id",
                "cutoff_policy_revision",
                "entries",
                "publication",
            )
        }
        calculated_seal = digest(projection)
        if manifest["seal"]["value"] != calculated_seal:
            return "manifest_seal"
        if manifest["seal"]["value"] != trusted_policy["manifest_seal"]:
            return "untrusted_manifest_seal"
        if imported["entry_key"]["canonical_locator"] != evidence["subject"]["legacy_locator"]:
            return "enclosing_locator_mismatch"
        if imported["source_boundary"] != trusted_policy["source_boundary"]:
            return "import_source_boundary_mismatch"
        if imported["source_transaction_id"] != entry["transaction_id"]:
            return "source_transaction_mismatch"
        if imported["source_provenance_id"] != entry["provenance_id"]:
            return "source_provenance_mismatch"
        importer = imported["importer"]
        if (
            importer["identity"],
            importer["revision"],
        ) != trusted_policy["importer"]:
            return "trusted_importer_mismatch"
        if (
            (authority["identity"], authority["implementation_revision"])
            != trusted_policy["importer"]
            or (
                authority["capability_profile_id"],
                authority["capability_profile_revision"],
            )
            != trusted_policy["authority_profile"]
        ):
            return "enclosing_importer_mismatch"
        if evidence["correlation_id"] != imported["import_transaction_id"]:
            return "import_transaction_correlation"
        if imported["import_transaction_id"] in entry_transactions | {
            publication["publication_transaction_id"]
        }:
            return "import_transaction_not_distinct"
        if imported["import_provenance_id"] in entry_provenances | {
            publication["provenance_id"]
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
            quality = capability["quality"]
            constraints = capability.get("constraints")
            evidence = capability.get("evidence")
            if quality == "unsupported" and (
                "constraints" in capability or "evidence" in capability
            ):
                return "unsupported_positive_claim"
            if quality != "unsupported" and not isinstance(evidence, dict):
                return "capability_attestation"
            if quality == "authoritative" and not isinstance(constraints, dict):
                return "capability_attestation"
    except (KeyError, TypeError):
        return "capability_set_malformed"
    return None


CAPABILITY_QUALITY_RANK = {
    "unsupported": 0,
    "best_effort": 1,
    "authoritative": 2,
}


def capability_profile_error(
    capability_set,
    profile_key,
    adapter_key,
    evidence_quality,
    *,
    require_authoritative=False,
):
    capability_error = capability_set_error(capability_set)
    if capability_error:
        return capability_error
    if capability_set.get("revision") != profile_key[1]:
        return "capability_set_revision"
    matches = [
        capability
        for capability in capability_set["capabilities"]
        if capability["capability"] == profile_key[0]
    ]
    if len(matches) != 1:
        return "capability_profile_missing"
    capability = matches[0]
    capability_quality = capability.get("quality")
    if capability_quality == "unsupported":
        return "capability_quality"
    if (
        capability_quality not in CAPABILITY_QUALITY_RANK
        or evidence_quality not in CAPABILITY_QUALITY_RANK
        or CAPABILITY_QUALITY_RANK[evidence_quality]
        > CAPABILITY_QUALITY_RANK[capability_quality]
        or (require_authoritative and capability_quality != "authoritative")
    ):
        return "capability_quality"
    constraints = capability.get("constraints")
    if constraints is not None and not isinstance(constraints, dict):
        return "capability_constraints"
    evidence = capability.get("evidence")
    if not isinstance(evidence, dict) or (
        capability_quality == "authoritative" and not isinstance(constraints, dict)
    ):
        return "capability_attestation"
    if (
        evidence.get("source_id"),
        evidence.get("source_revision"),
    ) != adapter_key:
        return "capability_attestation"
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
        if workspace_registry_error(workspace):
            return "session_workspace_registry"
        if session_ref["workspace_id"] != workspace["workspace_id"]:
            return "session_workspace_mismatch"
        scope = session_ref["scope"]
        if scope["kind"] == "project":
            outer_project = scope["project_id"]
            if outer_project not in {
                project["project_id"] for project in workspace["projects"]
            }:
                return "session_project_unregistered"
        elif scope == {"kind": "workspace"}:
            outer_project = None
        else:
            return "session_scope_binding"
        evidence = session_ref["evidence"]
        if (
            evidence.get("workspace_id") != session_ref["workspace_id"]
            or evidence.get("scope") != scope
        ):
            return "session_evidence_scope"
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
        }
        binding = session_ref.get("repository_binding")
        if binding is not None:
            binding_result = binding_error(binding, outer_project, workspace)
            if binding_result:
                return binding_result
            expected["repository_binding"] = binding
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
        if "outcome" in record:
            state = record["outcome"]
            state_matches = (
                evidence["state"] in PENDING_DELIVERY_STATES
                if state == "pending"
                else evidence["state"] == state
            )
        else:
            state = record["state"]
            state_matches = evidence["state"] == state
        if not state_matches:
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
        if (
            "session_ref_id" in subject or "session_ref_id" in record
        ) and subject.get("session_ref_id") != record.get("session_ref_id"):
            return "outcome_subject_mismatch:session_ref_id"
        if evidence["correlation_id"] != record["attempt_id"]:
            return "outcome_attempt_correlation"
        if state in {"accepted", "completed"} and trusted_authority is not None:
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


GRAPH_KIND_IDS = {
    "AgentV1": "agent_id",
    "EndpointV1": "endpoint_id",
    "SessionRefV1": "session_ref_id",
    "MessageV1": "message_id",
    "DeliveryV1": "delivery_id",
    "ReceiptV1": "receipt_id",
    "CapabilitySetV1": "capability_set_id",
    "StateEvidenceV1": "evidence_id",
}


def graph_record_kind(record):
    if "evidence_id" in record and "evidence_kind" in record:
        return "StateEvidenceV1"
    if "receipt_id" in record:
        return "ReceiptV1"
    if "delivery_id" in record:
        return "DeliveryV1"
    if "message_id" in record:
        return "MessageV1"
    if "native_session_id" in record and "session_ref_id" in record:
        return "SessionRefV1"
    if "adapter_name" in record and "endpoint_id" in record:
        return "EndpointV1"
    if "roles" in record and "agent_id" in record:
        return "AgentV1"
    if "capabilities" in record and "capability_set_id" in record:
        return "CapabilitySetV1"
    return None


def evidence_reference_error(
    evidence,
    indices,
    attempts,
    workspace,
    graph_project,
):
    """Resolve every supplied StateEvidence subject reference exactly."""
    try:
        evidence_result = state_evidence_error(evidence)
        if evidence_result:
            return evidence_result
        if (
            evidence["workspace_id"] != workspace["workspace_id"]
            or evidence["scope"]
            != {"kind": "project", "project_id": graph_project}
        ):
            return "evidence_scope"
        subject = evidence["subject"]
        message = None
        if "message_id" in subject:
            message = indices["MessageV1"].get(subject["message_id"])
            if message is None:
                return "message_missing"
        delivery = None
        if "delivery_id" in subject:
            delivery = indices["DeliveryV1"].get(subject["delivery_id"])
            if delivery is None:
                return "delivery_missing"
        attempt_delivery = None
        if "attempt_id" in subject:
            attempt_delivery = attempts.get(subject["attempt_id"])
            if attempt_delivery is None:
                return "attempt_missing"
            if evidence["correlation_id"] != subject["attempt_id"]:
                return "attempt_correlation"
        endpoint = None
        if "endpoint_id" in subject:
            endpoint = indices["EndpointV1"].get(subject["endpoint_id"])
            if endpoint is None:
                return "endpoint_missing"
        session = None
        if "session_ref_id" in subject:
            session = indices["SessionRefV1"].get(subject["session_ref_id"])
            if session is None:
                return "session_missing"
        if delivery is not None:
            for field in (
                "message_id",
                "attempt_id",
                "endpoint_id",
                "session_ref_id",
            ):
                if field in subject and subject[field] != delivery.get(field):
                    return f"delivery_mismatch:{field}"
        if attempt_delivery is not None and (
            delivery is not None and attempt_delivery is not delivery
        ):
            return "attempt_delivery_mismatch"
        if session is not None:
            if endpoint is not None and session["endpoint_id"] != endpoint["endpoint_id"]:
                return "session_endpoint_mismatch"
            if "native_session_id" in subject and (
                subject["native_session_id"] != session["native_session_id"]
            ):
                return "native_session_mismatch"
            if "repository_binding" in subject and (
                subject["repository_binding"] != session.get("repository_binding")
            ):
                return "session_repository_mismatch"
        elif "native_session_id" in subject:
            return "native_session_without_session_ref"
        binding = subject.get("repository_binding")
        if binding is not None:
            binding_result = binding_error(
                binding,
                graph_project,
                workspace,
            )
            if binding_result:
                return binding_result
        if evidence["evidence_kind"] == "exact_session_binding":
            if session is None:
                return "exact_session_missing"
            expected_subject = {
                "endpoint_id": session["endpoint_id"],
                "session_ref_id": session["session_ref_id"],
                "native_session_id": session["native_session_id"],
            }
            if "repository_binding" in session:
                expected_subject["repository_binding"] = session[
                    "repository_binding"
                ]
            if subject != expected_subject:
                return "exact_session_subject_mismatch"
        if message is not None and delivery is not None and (
            delivery["message_id"] != message["message_id"]
        ):
            return "message_delivery_mismatch"
    except (KeyError, TypeError):
        return "evidence_reference_malformed"
    return None


def reachable_graph_error(
    records,
    workspace,
    trusted_catalog,
    *,
    schema_catalog=SCHEMAS,
    registry=REGISTRY,
):
    """Validate one project-scoped offline authority graph; fail closed."""
    try:
        if catalog_error(CATALOG, schema_catalog) or not references_resolve(
            registry, schema_catalog
        ):
            return "graph_schema_catalog"
        if workspace_registry_error(workspace):
            return "graph_workspace_registry"
        indices = {kind: {} for kind in GRAPH_KIND_IDS}
        graph_projects = set()
        for record in records:
            kind = graph_record_kind(record)
            if kind is None:
                return "graph_record_kind"
            validator = Draft202012Validator(
                schema_catalog[kind],
                registry=registry,
                format_checker=FormatChecker(),
            )
            if next(validator.iter_errors(record), None) is not None:
                return f"graph_schema:{kind}"
            id_field = GRAPH_KIND_IDS[kind]
            record_id = record[id_field]
            if record_id in indices[kind]:
                return f"graph_duplicate_id:{id_field}"
            indices[kind][record_id] = record
            if record["workspace_id"] != workspace["workspace_id"]:
                return "graph_workspace_mismatch"
            scope = record["scope"]
            if kind in {
                "EndpointV1",
                "SessionRefV1",
                "MessageV1",
                "DeliveryV1",
                "ReceiptV1",
                "StateEvidenceV1",
            }:
                if scope["kind"] != "project":
                    return f"graph_project_scope_required:{kind}"
                graph_projects.add(scope["project_id"])
            elif scope["kind"] == "project":
                graph_projects.add(scope["project_id"])
            elif scope != {"kind": "workspace"}:
                return f"graph_scope:{kind}"
        if len(graph_projects) != 1:
            return "graph_project_identity_count"
        graph_project = next(iter(graph_projects))
        if graph_project not in {
            project["project_id"] for project in workspace["projects"]
        }:
            return "graph_project_unregistered"

        endpoints = indices["EndpointV1"]
        agents = indices["AgentV1"]
        capability_sets = indices["CapabilitySetV1"]
        sessions = indices["SessionRefV1"]
        messages = indices["MessageV1"]
        deliveries = indices["DeliveryV1"]
        receipts = indices["ReceiptV1"]
        standalone_evidence = indices["StateEvidenceV1"]
        if not all(
            (
                endpoints,
                agents,
                capability_sets,
                sessions,
                messages,
                deliveries,
                receipts,
                standalone_evidence,
            )
        ):
            return "graph_required_kind_missing"

        for capability_set in capability_sets.values():
            capability_error = capability_set_error(capability_set)
            if capability_error:
                return f"graph_capability_set:{capability_error}"

        referenced_capability_sets = set()
        capability_set_adapters = {}
        for endpoint_id, endpoint in endpoints.items():
            registration = trusted_catalog["endpoint_registrations"].get(
                endpoint_id
            )
            if registration is None:
                return "graph_endpoint_unregistered"
            if endpoint["agent_id"] not in agents:
                return "graph_agent_missing"
            if endpoint["capability_set_id"] not in capability_sets:
                return "graph_capability_set_missing"
            referenced_capability_sets.add(endpoint["capability_set_id"])
            capability_scope = capability_sets[endpoint["capability_set_id"]][
                "scope"
            ]
            if capability_scope not in (
                {"kind": "workspace"},
                {"kind": "project", "project_id": graph_project},
            ):
                return "graph_capability_scope"
            actual_registration = {
                "agent_id": endpoint["agent_id"],
                "capability_set_id": endpoint["capability_set_id"],
                "adapter": (
                    endpoint["adapter_name"],
                    endpoint["adapter_revision"],
                ),
                "configuration_ref": (
                    endpoint["configuration_ref"]["registry_id"],
                    endpoint["configuration_ref"]["revision"],
                    endpoint["configuration_ref"]["reference"],
                ),
            }
            if actual_registration != registration:
                return "graph_endpoint_registration_mismatch"
            capability_set_id = endpoint["capability_set_id"]
            adapter_key = (
                endpoint["adapter_name"],
                endpoint["adapter_revision"],
            )
            bound_adapter = capability_set_adapters.get(capability_set_id)
            if bound_adapter is not None and bound_adapter != adapter_key:
                return "graph_capability_set_adapter_conflict"
            capability_set_adapters[capability_set_id] = adapter_key
            for capability in capability_sets[capability_set_id]["capabilities"]:
                if capability["quality"] == "unsupported":
                    continue
                attestation = capability.get("evidence")
                if not isinstance(attestation, dict) or (
                    attestation.get("source_id"),
                    attestation.get("source_revision"),
                ) != adapter_key:
                    return "graph_capability_set_attestation"
        for capability_set_id, capability_set in capability_sets.items():
            if (
                capability_set["scope"] == {"kind": "workspace"}
                and capability_set_id not in referenced_capability_sets
            ):
                return "graph_workspace_capability_unbound"

        attempts = {}
        for delivery in deliveries.values():
            attempt_id = delivery["attempt_id"]
            if attempt_id in attempts:
                return "graph_duplicate_id:attempt_id"
            attempts[attempt_id] = delivery

        evidence_index = {}
        for evidence_id, evidence in standalone_evidence.items():
            evidence_index[evidence_id] = evidence
        for kind, kind_records in indices.items():
            if kind == "StateEvidenceV1":
                continue
            for record in kind_records.values():
                evidence = record.get("evidence")
                if evidence is None:
                    continue
                evidence_id = evidence["evidence_id"]
                if evidence_id in evidence_index:
                    return "graph_duplicate_id:evidence_id"
                evidence_index[evidence_id] = evidence
                if (
                    evidence["workspace_id"] != record["workspace_id"]
                    or evidence["scope"] != record["scope"]
                ):
                    return "graph_evidence_scope"
        for evidence in evidence_index.values():
            reference_error = evidence_reference_error(
                evidence,
                indices,
                attempts,
                workspace,
                graph_project,
            )
            if reference_error:
                return f"graph_evidence_reference:{reference_error}"
            if evidence["evidence_kind"] == "compatibility_import":
                compatibility_policy = trusted_catalog.get(
                    "compatibility_policy"
                )
                if not isinstance(compatibility_policy, dict):
                    return "graph_compatibility_policy_missing"
                compatibility_error = manifest_error(
                    evidence,
                    workspace,
                    compatibility_policy,
                )
                if compatibility_error:
                    return (
                        "graph_compatibility_manifest:"
                        f"{compatibility_error}"
                    )

        for session in sessions.values():
            endpoint = endpoints.get(session["endpoint_id"])
            if endpoint is None:
                return "graph_session_endpoint_missing"
            evidence = session["evidence"]
            if session_ref_error(
                session,
                workspace,
                trusted_authority=(
                    endpoint["adapter_name"],
                    endpoint["adapter_revision"],
                    "native_session_binding",
                    capability_sets[endpoint["capability_set_id"]]["revision"],
                ),
            ):
                return "graph_session_evidence"
            authority = evidence["authority"]
            adapter_key = (
                endpoint["adapter_name"],
                endpoint["adapter_revision"],
            )
            profile_key = (
                authority["capability_profile_id"],
                authority["capability_profile_revision"],
            )
            if profile_key not in trusted_catalog["adapter_profiles"].get(
                adapter_key, set()
            ):
                return "graph_session_profile"
            capability_set = capability_sets[endpoint["capability_set_id"]]
            if capability_profile_error(
                capability_set,
                profile_key,
                adapter_key,
                evidence["quality"],
                require_authoritative=True,
            ):
                return "graph_session_capability"

        for delivery in deliveries.values():
            message = messages.get(delivery["message_id"])
            endpoint = endpoints.get(delivery["endpoint_id"])
            if message is None:
                return "graph_message_missing"
            if endpoint is None:
                return "graph_delivery_endpoint_missing"
            if endpoint["agent_id"] not in message["recipients"]:
                return "graph_recipient_endpoint_agent"
            error = outcome_error(delivery, trusted_authority=None)
            if error:
                return f"graph_delivery_evidence:{error}"
            session_id = delivery.get("session_ref_id")
            session = sessions.get(session_id) if session_id is not None else None
            if session_id is not None and session is None:
                return "graph_delivery_session_missing"
            if session is not None and session["endpoint_id"] != endpoint["endpoint_id"]:
                return "graph_delivery_session_endpoint"
            if delivery["outcome"] in {"accepted", "completed"} and session is None:
                return "graph_positive_session_missing"
            capability_set = capability_sets[endpoint["capability_set_id"]]
            authority = delivery["evidence"]["authority"]
            adapter_key = (
                endpoint["adapter_name"],
                endpoint["adapter_revision"],
            )
            profile_key = (
                authority["capability_profile_id"],
                authority["capability_profile_revision"],
            )
            if (
                authority["identity"],
                authority["implementation_revision"],
            ) != adapter_key:
                return "graph_delivery_adapter_authority"
            if profile_key not in trusted_catalog["adapter_profiles"].get(
                adapter_key, set()
            ):
                return "graph_delivery_profile"
            if capability_profile_error(
                capability_set,
                profile_key,
                adapter_key,
                delivery["evidence"]["quality"],
                require_authoritative=delivery["outcome"]
                in {"accepted", "completed"},
            ):
                return "graph_delivery_capability"

        for receipt in receipts.values():
            delivery = deliveries.get(receipt["delivery_id"])
            endpoint = endpoints.get(receipt["endpoint_id"])
            if delivery is None:
                return "graph_receipt_delivery_missing"
            if endpoint is None:
                return "graph_receipt_endpoint_missing"
            for field in ("message_id", "attempt_id", "endpoint_id"):
                if receipt[field] != delivery[field]:
                    return f"graph_receipt_delivery_mismatch:{field}"
            if receipt.get("session_ref_id") != delivery.get("session_ref_id"):
                return "graph_receipt_delivery_mismatch:session_ref_id"
            error = outcome_error(receipt, trusted_authority=None)
            if error:
                return f"graph_receipt_evidence:{error}"
            if receipt["state"] in {"accepted", "completed"}:
                session = sessions.get(receipt.get("session_ref_id"))
                if session is None or session["endpoint_id"] != endpoint["endpoint_id"]:
                    return "graph_receipt_session"
            authority = receipt["evidence"]["authority"]
            adapter_key = (
                endpoint["adapter_name"],
                endpoint["adapter_revision"],
            )
            profile_key = (
                authority["capability_profile_id"],
                authority["capability_profile_revision"],
            )
            if (
                authority["identity"],
                authority["implementation_revision"],
            ) != adapter_key:
                return "graph_receipt_adapter_authority"
            if profile_key not in trusted_catalog["adapter_profiles"].get(
                adapter_key, set()
            ):
                return "graph_receipt_profile"
            capability_set = capability_sets[endpoint["capability_set_id"]]
            if capability_profile_error(
                capability_set,
                profile_key,
                adapter_key,
                receipt["evidence"]["quality"],
                require_authoritative=receipt["state"]
                in {"accepted", "completed"},
            ):
                return "graph_receipt_capability"
    except Exception:
        return "graph_malformed"
    return None


def replace_project_identity(value, project_id, source_project_id="proj"):
    if isinstance(value, dict):
        return {
            key: (
                project_id
                if key in {"project_id", "project", "source_project_id"}
                and item == source_project_id
                else replace_project_identity(item, project_id, source_project_id)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            replace_project_identity(item, project_id, source_project_id)
            for item in value
        ]
    return value


def make_reachable_graph(project_id="proj"):
    workspace = replace_project_identity(
        load(FIXTURE_DIR / "valid" / "workspace.json"), project_id
    )
    records = [
        replace_project_identity(
            load(FIXTURE_DIR / "valid" / f"{stem}.json"), project_id
        )
        for stem in (
            "agent",
            "capability-set",
            "endpoint",
            "session-ref",
            "message",
            "delivery",
            "receipt",
            "state-evidence",
        )
    ]
    for record in records:
        if graph_record_kind(record) == "StateEvidenceV1":
            if record["evidence_kind"] == "compatibility_import":
                recalculate_legacy_integrity(record)
            else:
                seal_state_evidence(record)
        elif "evidence" in record:
            seal_state_evidence(record["evidence"])
    return workspace, records


def make_delivery_outcome(outcome, evidence_state=None):
    delivery = load(FIXTURE_DIR / "valid" / "delivery.json")
    evidence_state = (
        "persisted"
        if outcome == "pending" and evidence_state is None
        else evidence_state or outcome
    )
    delivery["outcome"] = outcome
    delivery["evidence"]["state"] = evidence_state
    if outcome not in {"accepted", "completed"} and evidence_state not in {
        "accepted",
        "completed",
    }:
        delivery["evidence"]["quality"] = "best_effort"
        delivery["evidence"]["evidence_kind"] = "adapter_observation"
        delivery["evidence"]["authority"]["authority_kind"] = "trusted_adapter"
    else:
        delivery["evidence"]["quality"] = "authoritative"
        delivery["evidence"]["evidence_kind"] = "native_delivery_state"
    seal_state_evidence(delivery["evidence"])
    return delivery


def make_receipt_state(state, evidence_state=None):
    receipt = load(FIXTURE_DIR / "valid" / "receipt.json")
    evidence_state = evidence_state or state
    receipt["state"] = state
    receipt["evidence"]["state"] = evidence_state
    if state not in {"accepted", "completed"} and evidence_state not in {
        "accepted",
        "completed",
    }:
        receipt["evidence"]["quality"] = "best_effort"
        receipt["evidence"]["evidence_kind"] = "adapter_observation"
        receipt["evidence"]["authority"]["authority_kind"] = "trusted_adapter"
    else:
        receipt["evidence"]["quality"] = "authoritative"
        receipt["evidence"]["evidence_kind"] = "exact_session_acknowledgment"
    seal_state_evidence(receipt["evidence"])
    return receipt


NONTERMINAL_OUTCOMES = (
    "ambiguous",
    "deferred_busy",
    "rejected_before_acceptance",
    "pull_pending",
)


def make_best_effort_graph(record_kind, state, *, workspace_capability=False):
    workspace, records = make_reachable_graph()
    catalog = copy.deepcopy(TRUSTED_GRAPH_CATALOG)
    graph = {graph_record_kind(record): record for record in records}
    capability_set = graph["CapabilitySetV1"]
    if workspace_capability:
        capability_set["scope"] = {"kind": "workspace"}
    capability_set["capabilities"].append(
        {
            "capability": "adapter_observation",
            "quality": "best_effort",
            "evidence": {
                "evidence_kind": "profile_attestation",
                "source_id": "native_adapter",
                "source_revision": "r1",
                "integrity": "sha256:" + "c" * 64,
            },
        }
    )
    catalog["adapter_profiles"][("native_adapter", "r1")].add(
        ("adapter_observation", "r1")
    )
    record = graph[record_kind]
    record["outcome" if record_kind == "DeliveryV1" else "state"] = state
    evidence = record["evidence"]
    evidence["state"] = state
    evidence["quality"] = "best_effort"
    evidence["evidence_kind"] = "adapter_observation"
    evidence["authority"].update(
        {
            "authority_kind": "trusted_adapter",
            "capability_profile_id": "adapter_observation",
            "capability_profile_revision": "r1",
        }
    )
    seal_state_evidence(evidence)
    return workspace, records, catalog


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

    def test_canonical_json_bytes_are_cross_language_frozen_and_fail_closed(self):
        state_description = SCHEMAS["StateEvidenceV1"]["description"]
        event_description = SCHEMAS["EventEnvelopeV1"]["description"]
        for required_text in (
            "UTF-8",
            "Unicode code-point order",
            "array order",
            "no insignificant whitespace",
            "unnormalized Unicode scalar",
            "integers",
            "duplicate keys",
            "NaN",
            "Infinity",
            "integer lexical -0",
        ):
            self.assertIn(required_text, state_description)
        self.assertIn("same frozen UTF-8", event_description)
        self.assertIn("safe-integer-only", event_description)
        self.assertIn("integer lexical -0", event_description)
        self.assertIn("UTF-8", CANONICAL_JSON_ALGORITHM)
        self.assertIn("integer lexical -0", CANONICAL_JSON_ALGORITHM)
        self.assertEqual(
            canonical_bytes({"é": "雪", "a": [2, 1]}),
            '{"a":[2,1],"é":"雪"}'.encode("utf-8"),
        )
        self.assertNotEqual(
            canonical_bytes({"text": "é"}),
            canonical_bytes({"text": "e\u0301"}),
        )
        parsed_negative_zero = strict_json_loads('{"value":-0}')
        self.assertEqual(parsed_negative_zero, {"value": 0})
        self.assertIs(type(parsed_negative_zero["value"]), int)
        self.assertEqual(canonical_bytes(parsed_negative_zero), b'{"value":0}')
        self.assertEqual(
            canonical_bytes(parsed_negative_zero),
            canonical_bytes(strict_json_loads('{"value":0}')),
        )
        self.assertEqual(
            digest(parsed_negative_zero),
            "23d7b286bd429460b92a2a1c21b6afc34110446c5034c17363fda363aa0a7c5d",
        )
        for value in (
            {"value": 1.0},
            {"value": float("nan")},
            {"value": SAFE_INTEGER_MAX + 1},
            {"value": "nul\x00text"},
            {"value": "line\u2028text"},
            {"\ud800": "key"},
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    canonical_bytes(value)
        with self.assertRaises(ValueError):
            strict_json_loads('{"duplicate":1,"duplicate":2}')
        with self.assertRaises(ValueError):
            strict_json_loads('{"value":NaN}')
        for noncanonical_number in ('{"value":1.0}', '{"value":1e0}'):
            with self.assertRaises(ValueError):
                strict_json_loads(noncanonical_number)

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

    def test_extensions_and_payload_scalars_are_bounded_for_all_ten_schemas(self):
        huge_integer = 10**10000
        for name, filename in EXPECTED.items():
            stem = filename.removesuffix(".schema.json")
            valid = load(FIXTURE_DIR / "valid" / f"{stem}.json")
            if name == "EventEnvelopeV1":
                boundary = copy.deepcopy(valid)
                boundary["payload"] = {"value": SAFE_INTEGER_MAX}
                self.assertEqual(self.errors(name, boundary), [])
                for value in (SAFE_INTEGER_MAX + 1, 1.5, float("nan")):
                    candidate = copy.deepcopy(valid)
                    candidate["payload"] = {"value": value}
                    with self.subTest(name=name, value=type(value).__name__):
                        self.assert_schema_rejects(
                            name, candidate, "bounded payload number"
                        )
                huge = copy.deepcopy(valid)
                huge["payload"] = {"value": huge_integer}
                self.assertEqual(envelope_error(huge), "payload_integer_range")
                continue
            boundary = copy.deepcopy(valid)
            boundary["extensions"] = {
                "x_note_integer": SAFE_INTEGER_MAX,
                "x_note_text": "é" * 512,
                "x_note_boolean": True,
                "x_note_null": None,
            }
            self.assertEqual(self.errors(name, boundary), [])
            self.assertIsNone(extension_error(boundary))
            self.assertLess(len(canonical_bytes(boundary["extensions"])), 8192)
            for value in (
                SAFE_INTEGER_MAX + 1,
                1.5,
                float("nan"),
                float("inf"),
                "x" * 513,
                "nul\x00value",
            ):
                candidate = copy.deepcopy(valid)
                candidate["extensions"] = {"x_note_bad": value}
                with self.subTest(name=name, value=type(value).__name__):
                    self.assert_schema_rejects(
                        name, candidate, "bounded extension scalar"
                    )
            for value, expected_error in (
                (huge_integer, "extension_integer_range"),
                (1.0, "extension_scalar"),
            ):
                candidate = copy.deepcopy(valid)
                candidate["extensions"] = {"x_note_bad": value}
                self.assertEqual(extension_error(candidate), expected_error)
                with self.assertRaises(ValueError):
                    canonical_bytes(candidate["extensions"])

        capability_set = load(FIXTURE_DIR / "valid" / "capability-set.json")
        capability_set["capabilities"][0]["extensions"] = {
            "x_note_nested": SAFE_INTEGER_MAX + 1
        }
        self.assert_schema_rejects(
            "CapabilitySetV1", capability_set, "nested capability extension"
        )
        self.assertEqual(
            extension_error(capability_set), "extension_integer_range"
        )
        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        for container in (
            workspace["projects"][0],
            workspace["repositories"][0],
            workspace["relationships"][0],
        ):
            container["extensions"] = {"x_note_nested": "x" * 513}
        self.assert_schema_rejects(
            "WorkspaceV1", workspace, "nested registry extensions"
        )
        self.assertEqual(extension_error(workspace), "extension_string")

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

        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        self.assertEqual(workspace["scope"], {"kind": "workspace"})
        for scope in (
            None,
            {"kind": "project", "project_id": "proj"},
            {"kind": "workspace", "project_id": "proj"},
        ):
            candidate = copy.deepcopy(workspace)
            if scope is None:
                candidate.pop("scope")
            else:
                candidate["scope"] = scope
            with self.subTest(workspace_scope=scope):
                self.assert_schema_rejects(
                    "WorkspaceV1", candidate, "workspace discriminator"
                )

    def test_activation_exact_tuple_scope_registry_and_lexical_matrix(self):
        message = load(FIXTURE_DIR / "valid" / "message.json")
        expected = copy.deepcopy(message["activation_import"])
        expected.pop("activation")
        expected.pop("to")
        self.assertIsNone(activation_error(message, expected, "codex"))
        self.assertEqual(message["activation_import"]["branch"], "feature/native-proof")

        for field in ACTIVATION_FIELDS:
            candidate = copy.deepcopy(message)
            candidate["activation_import"][field] = (
                "/different-worktree"
                if field == "worktree"
                else f"different-{field}"
            )
            with self.subTest(field=field):
                self.assertEqual(
                    activation_error(candidate, expected, "codex"),
                    f"activation_tuple_mismatch:{field}",
                )

        wrong_receiver = copy.deepcopy(message)
        wrong_receiver["activation_import"]["to"] = "Codex"
        self.assertEqual(
            activation_error(wrong_receiver, expected, "codex"),
            "activation_claiming_target_mismatch",
        )
        wrong_scope = copy.deepcopy(message)
        wrong_scope["scope"]["project_id"] = "other"
        self.assertEqual(
            activation_error(wrong_scope, expected, "codex"),
            "activation_outer_scope_mismatch",
        )
        workspace_scope = copy.deepcopy(message)
        workspace_scope["scope"] = {"kind": "workspace"}
        self.assert_schema_rejects(
            "MessageV1", workspace_scope, "activation workspace scope"
        )
        self.assertEqual(
            activation_error(workspace_scope, expected, "codex"),
            "activation_outer_scope_mismatch",
        )

        for path in (
            "/",
            "/Users/pixexid/Projects/llm-collab-worktrees/codex/isolated-lane",
        ):
            candidate = copy.deepcopy(message)
            candidate["activation_import"]["worktree"] = path
            candidate_expected = copy.deepcopy(expected)
            candidate_expected["worktree"] = path
            identity = {
                field: candidate["activation_import"][field]
                for field in ACTIVATION_FIELDS
            }
            with self.subTest(path=path, boundary="valid_current_helper"):
                self.assertEqual(self.errors("MessageV1", candidate), [])
                self.assertEqual(lease_identity(identity), identity)
                self.assertIsNone(
                    activation_error(candidate, candidate_expected, "codex")
                )

        for path in (
            "relative/lane",
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
            with self.subTest(path=path, boundary="current_helper"):
                bad_identity = copy.deepcopy(expected)
                bad_identity["worktree"] = path
                with self.assertRaises(ValueError):
                    lease_identity(bad_identity)
        for branch in (
            "main",
            "feature/native-proof",
            "claude/native-proof",
            "codex/native-proof",
            "feature branch",
            "x" * 2049,
        ):
            candidate = copy.deepcopy(message)
            candidate["activation_import"]["branch"] = branch
            self.assertEqual(self.errors("MessageV1", candidate), [])
            self.assertEqual(normalized_identity_field("branch", branch), branch)
            self.assertTrue(frontmatter_roundtrips(branch))

        def helper_accepts_serialized(field, value):
            if not isinstance(value, str):
                return False
            if field == "worktree":
                identity = copy.deepcopy(expected)
                identity[field] = value
                try:
                    return lease_identity(identity)[field] == value
                except ValueError:
                    return False
            helper_field = "target_agent" if field == "to" else field
            try:
                return normalized_identity_field(helper_field, value) == value
            except ValueError:
                return False

        parity_values = (
            "ordinary-text",
            "x" * 2049,
            "true",
            "FALSE",
            "null",
            "0",
            "+12",
            "-7",
            "[x]",
            "  canonical-after-trim",
            "canonical-before-trim  ",
            "control\x00value",
            "line\u2028break",
            7,
            None,
        )
        for field in (*ACTIVATION_FIELDS, "to"):
            if field == "worktree":
                continue
            for value in parity_values:
                candidate = copy.deepcopy(message)
                candidate["activation_import"][field] = value
                schema_accepts = not self.errors("MessageV1", candidate)
                helper_accepts = helper_accepts_serialized(field, value)
                with self.subTest(field=field, value=repr(value)):
                    self.assertEqual(schema_accepts, helper_accepts)

        identity_pattern = SCHEMAS["MessageV1"]["$defs"]["identityString"][
            "pattern"
        ]
        self.assertNotIn("(?i", identity_pattern)
        self.assertIn("[Tt][Rr][Uu][Ee]", identity_pattern)
        self.assertIn("[Ff][Aa][Ll][Ss][Ee]", identity_pattern)
        self.assertIn("[Nn][Uu][Ll][Ll]", identity_pattern)

        for digit_count in (3, 4300, 4301, 5000):
            for digit_kind, digit in (
                ("ascii", "9"),
                ("unicode_decimal", "\u0669"),
            ):
                digits = digit * digit_count
                for sign in ("", "+", "-"):
                    for underscored in (False, True):
                        body = "_".join(digits) if underscored else digits
                        value = sign + body
                        candidate = copy.deepcopy(message)
                        candidate["activation_import"]["branch"] = value
                        with self.subTest(
                            digit_count=digit_count,
                            digit_kind=digit_kind,
                            sign=sign or "unsigned",
                            underscored=underscored,
                        ):
                            self.assertTrue(is_decimal_integer_lexical(value))
                            if digit_kind == "ascii":
                                self.assert_schema_rejects(
                                    "MessageV1",
                                    candidate,
                                    "ASCII integer lexical identity",
                                )
                            else:
                                self.assertEqual(
                                    self.errors("MessageV1", candidate),
                                    [],
                                    "portable schema intentionally leaves Unicode "
                                    "decimal classification to the semantic gate",
                                )
                            self.assertEqual(
                                activation_error(candidate, expected, "codex"),
                                "activation_noncanonical_serialized:branch",
                            )

        for value in ("1__2", "_12", "12_", "+_12", "-"):
            candidate = copy.deepcopy(message)
            candidate["activation_import"]["branch"] = value
            with self.subTest(non_integer_lexical=value):
                self.assertFalse(is_decimal_integer_lexical(value))
                self.assertEqual(self.errors("MessageV1", candidate), [])
                self.assertTrue(frontmatter_roundtrips(value))
        with self.assertRaises(ValueError):
            normalized_identity_field("task", "   ")
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

        multi_project_workspace = copy.deepcopy(workspace)
        multi_project_workspace["projects"].append({"project_id": "other"})
        mixed = copy.deepcopy(objects)
        mixed[0]["scope"]["project_id"] = "other"
        self.assertEqual(
            scope_bundle_error(mixed, multi_project_workspace),
            "project_identity_count",
        )

    def test_reachable_graph_resolves_exact_catalog_authority_and_project(self):
        workspace, records = make_reachable_graph()
        self.assertIsNone(
            reachable_graph_error(records, workspace, TRUSTED_GRAPH_CATALOG)
        )
        self.assertEqual(
            reachable_graph_error(
                records,
                workspace,
                TRUSTED_GRAPH_CATALOG,
                schema_catalog={},
            ),
            "graph_schema_catalog",
        )
        self.assertEqual(
            reachable_graph_error(records, workspace, {}),
            "graph_malformed",
        )

        resolved_standalone = copy.deepcopy(
            next(
                record
                for record in records
                if graph_record_kind(record) == "DeliveryV1"
            )["evidence"]
        )
        resolved_standalone["evidence_id"] = "evidence_standalone_delivery"
        seal_state_evidence(resolved_standalone)
        records_with_resolved_standalone = [
            *copy.deepcopy(records),
            resolved_standalone,
        ]
        self.assertIsNone(
            reachable_graph_error(
                records_with_resolved_standalone,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            )
        )

        workspace_capability_records = copy.deepcopy(records)
        workspace_capability = next(
            record
            for record in workspace_capability_records
            if graph_record_kind(record) == "CapabilitySetV1"
        )
        workspace_capability["scope"] = {"kind": "workspace"}
        self.assertIsNone(
            reachable_graph_error(
                workspace_capability_records,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            )
        )

        def by_kind(items):
            return {graph_record_kind(item): item for item in items}

        for capability_scope in ("project", "workspace"):
            for attestation_field, wrong_value in (
                ("source_id", "other_adapter"),
                ("source_revision", "r2"),
            ):
                candidate_records = copy.deepcopy(records)
                capability_set = by_kind(candidate_records)["CapabilitySetV1"]
                if capability_scope == "workspace":
                    capability_set["scope"] = {"kind": "workspace"}
                unused_capability = next(
                    capability
                    for capability in capability_set["capabilities"]
                    if capability["capability"] == "ui_visibility"
                )
                unused_capability["evidence"][attestation_field] = wrong_value
                with self.subTest(
                    unused_capability_attestation=attestation_field,
                    capability_scope=capability_scope,
                ):
                    self.assertEqual(
                        self.errors("CapabilitySetV1", capability_set),
                        [],
                    )
                    self.assertEqual(
                        reachable_graph_error(
                            candidate_records,
                            workspace,
                            TRUSTED_GRAPH_CATALOG,
                        ),
                        "graph_capability_set_attestation",
                    )

        for capability_scope in ("project", "workspace"):
            candidate_records = copy.deepcopy(records)
            graph = by_kind(candidate_records)
            if capability_scope == "workspace":
                graph["CapabilitySetV1"]["scope"] = {"kind": "workspace"}
            second_endpoint = copy.deepcopy(graph["EndpointV1"])
            second_endpoint.update(
                {
                    "endpoint_id": "endpoint_two",
                    "adapter_name": "other_adapter",
                    "adapter_revision": "r2",
                }
            )
            second_endpoint["configuration_ref"]["reference"] = (
                "endpoint_two_config"
            )
            candidate_records.append(second_endpoint)
            candidate_catalog = copy.deepcopy(TRUSTED_GRAPH_CATALOG)
            candidate_catalog["endpoint_registrations"]["endpoint_two"] = {
                "agent_id": second_endpoint["agent_id"],
                "capability_set_id": second_endpoint["capability_set_id"],
                "adapter": ("other_adapter", "r2"),
                "configuration_ref": (
                    second_endpoint["configuration_ref"]["registry_id"],
                    second_endpoint["configuration_ref"]["revision"],
                    second_endpoint["configuration_ref"]["reference"],
                ),
            }
            with self.subTest(
                incompatible_endpoint_adapters=capability_scope
            ):
                self.assertEqual(
                    reachable_graph_error(
                        candidate_records,
                        workspace,
                        candidate_catalog,
                    ),
                    "graph_capability_set_adapter_conflict",
                )

        for omitted_field in ("legacy_manifest", "legacy_import"):
            candidate_records = copy.deepcopy(records)
            compatibility_evidence = by_kind(candidate_records)[
                "StateEvidenceV1"
            ]
            compatibility_evidence.pop(omitted_field)
            seal_state_evidence(compatibility_evidence)
            with self.subTest(
                compatibility_omitted_record=omitted_field
            ):
                self.assertEqual(
                    reachable_graph_error(
                        candidate_records,
                        workspace,
                        TRUSTED_GRAPH_CATALOG,
                    ),
                    "graph_schema:StateEvidenceV1",
                )

        candidate_records = copy.deepcopy(records)
        compatibility_evidence = by_kind(candidate_records)["StateEvidenceV1"]
        compatibility_evidence["subject"].pop("legacy_locator")
        seal_state_evidence(compatibility_evidence)
        self.assertEqual(
            reachable_graph_error(
                candidate_records,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_schema:StateEvidenceV1",
        )

        candidate_records = copy.deepcopy(records)
        compatibility_evidence = by_kind(candidate_records)["StateEvidenceV1"]
        compatibility_evidence["legacy_manifest"]["seal"]["value"] = "f" * 64
        seal_state_evidence(compatibility_evidence)
        self.assertEqual(
            reachable_graph_error(
                candidate_records,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_compatibility_manifest:manifest_seal",
        )

        candidate_records = copy.deepcopy(records)
        compatibility_evidence = by_kind(candidate_records)["StateEvidenceV1"]
        compatibility_evidence["legacy_manifest"]["entries"][1][
            "transaction_id"
        ] = "forged_tx"
        compatibility_evidence["legacy_manifest"]["entries"][1][
            "provenance_id"
        ] = "forged_provenance"
        recalculate_legacy_integrity(compatibility_evidence)
        self.assertEqual(
            reachable_graph_error(
                candidate_records,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_compatibility_manifest:untrusted_manifest_seal",
        )

        missing_policy_catalog = copy.deepcopy(TRUSTED_GRAPH_CATALOG)
        missing_policy_catalog.pop("compatibility_policy")
        self.assertEqual(
            reachable_graph_error(records, workspace, missing_policy_catalog),
            "graph_compatibility_policy_missing",
        )
        for policy_field, wrong_value, expected_error in (
            (
                "manifest_seal",
                "f" * 64,
                "untrusted_manifest_seal",
            ),
            (
                "cutoff_policy_revision",
                "p2",
                "untrusted_manifest_policy",
            ),
            (
                "registry_revision",
                "r2",
                "publication_registry_mismatch",
            ),
        ):
            wrong_policy_catalog = copy.deepcopy(TRUSTED_GRAPH_CATALOG)
            wrong_policy_catalog["compatibility_policy"][
                policy_field
            ] = wrong_value
            with self.subTest(wrong_compatibility_policy=policy_field):
                self.assertEqual(
                    reachable_graph_error(
                        records,
                        workspace,
                        wrong_policy_catalog,
                    ),
                    f"graph_compatibility_manifest:{expected_error}",
                )

        embedded_records = copy.deepcopy(records)
        embedded_graph = by_kind(embedded_records)
        embedded_delivery = embedded_graph["DeliveryV1"]
        embedded_receipt = embedded_graph["ReceiptV1"]
        embedded_evidence = copy.deepcopy(
            embedded_graph["StateEvidenceV1"]
        )
        embedded_evidence["evidence_id"] = "evidence_embedded_legacy"
        embedded_evidence["legacy_import"]["import_transaction_id"] = (
            "attempt_legacy"
        )
        embedded_evidence["correlation_id"] = "attempt_legacy"
        recalculate_legacy_integrity(embedded_evidence)
        embedded_evidence["subject"].update(
            {
                "message_id": embedded_delivery["message_id"],
                "delivery_id": embedded_delivery["delivery_id"],
                "attempt_id": "attempt_legacy",
                "endpoint_id": embedded_delivery["endpoint_id"],
                "session_ref_id": embedded_delivery["session_ref_id"],
            }
        )
        seal_state_evidence(embedded_evidence)
        embedded_delivery["outcome"] = "pending"
        embedded_delivery["attempt_id"] = "attempt_legacy"
        embedded_delivery["evidence"] = embedded_evidence
        embedded_receipt["attempt_id"] = "attempt_legacy"
        embedded_receipt["evidence"]["subject"]["attempt_id"] = (
            "attempt_legacy"
        )
        embedded_receipt["evidence"]["correlation_id"] = "attempt_legacy"
        seal_state_evidence(embedded_receipt["evidence"])
        self.assertEqual(
            reachable_graph_error(
                embedded_records,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_delivery_adapter_authority",
        )
        embedded_delivery["evidence"]["legacy_manifest"]["seal"][
            "value"
        ] = "f" * 64
        seal_state_evidence(embedded_delivery["evidence"])
        self.assertEqual(
            reachable_graph_error(
                embedded_records,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_compatibility_manifest:manifest_seal",
        )

        for record_kind in ("DeliveryV1", "ReceiptV1"):
            for state in NONTERMINAL_OUTCOMES:
                for workspace_capability in (False, True):
                    (
                        candidate_workspace,
                        candidate_records,
                        candidate_catalog,
                    ) = make_best_effort_graph(
                        record_kind,
                        state,
                        workspace_capability=workspace_capability,
                    )
                    with self.subTest(
                        record_kind=record_kind,
                        state=state,
                        capability_scope=(
                            "workspace" if workspace_capability else "project"
                        ),
                    ):
                        self.assertIsNone(
                            reachable_graph_error(
                                candidate_records,
                                candidate_workspace,
                                candidate_catalog,
                            )
                        )

        for record_kind in ("DeliveryV1", "ReceiptV1"):
            for workspace_capability in (False, True):
                (
                    candidate_workspace,
                    candidate_records,
                    candidate_catalog,
                ) = make_best_effort_graph(
                    record_kind,
                    "ambiguous",
                    workspace_capability=workspace_capability,
                )
                graph = by_kind(candidate_records)
                capability_set = graph["CapabilitySetV1"]
                capability = next(
                    item
                    for item in capability_set["capabilities"]
                    if item["capability"] == "adapter_observation"
                )
                capability.pop("evidence")
                with self.subTest(
                    record_kind=record_kind,
                    missing_attestation=True,
                    capability_scope=(
                        "workspace" if workspace_capability else "project"
                    ),
                ):
                    self.assert_schema_rejects(
                        "CapabilitySetV1",
                        capability_set,
                        "unattested best-effort matched capability",
                    )
                    self.assertEqual(
                        capability_set_error(capability_set),
                        "capability_attestation",
                    )
                    self.assertEqual(
                        capability_profile_error(
                            capability_set,
                            ("adapter_observation", "r1"),
                            ("native_adapter", "r1"),
                            graph[record_kind]["evidence"]["quality"],
                        ),
                        "capability_attestation",
                    )
                    self.assertEqual(
                        reachable_graph_error(
                            candidate_records,
                            candidate_workspace,
                            candidate_catalog,
                        ),
                        "graph_schema:CapabilitySetV1",
                    )

        for record_kind in ("DeliveryV1", "ReceiptV1"):
            graph_error = (
                "graph_delivery_capability"
                if record_kind == "DeliveryV1"
                else "graph_receipt_capability"
            )
            authority_error = (
                "graph_delivery_adapter_authority"
                if record_kind == "DeliveryV1"
                else "graph_receipt_adapter_authority"
            )
            profile_error = (
                "graph_delivery_profile"
                if record_kind == "DeliveryV1"
                else "graph_receipt_profile"
            )
            negative_capability_cases = []

            candidate_workspace, candidate_records, candidate_catalog = (
                make_best_effort_graph(record_kind, "ambiguous")
            )
            graph = by_kind(candidate_records)
            capability = next(
                item
                for item in graph["CapabilitySetV1"]["capabilities"]
                if item["capability"] == "adapter_observation"
            )
            capability["quality"] = "unsupported"
            capability.pop("constraints", None)
            capability.pop("evidence")
            negative_capability_cases.append(
                (
                    "unsupported",
                    candidate_workspace,
                    candidate_records,
                    candidate_catalog,
                    graph_error,
                )
            )

            candidate_workspace, candidate_records, candidate_catalog = (
                make_best_effort_graph(record_kind, "ambiguous")
            )
            graph = by_kind(candidate_records)
            graph["CapabilitySetV1"]["capabilities"] = [
                item
                for item in graph["CapabilitySetV1"]["capabilities"]
                if item["capability"] != "adapter_observation"
            ]
            negative_capability_cases.append(
                (
                    "missing",
                    candidate_workspace,
                    candidate_records,
                    candidate_catalog,
                    graph_error,
                )
            )

            candidate_workspace, candidate_records, candidate_catalog = (
                make_best_effort_graph(record_kind, "ambiguous")
            )
            graph = by_kind(candidate_records)
            graph[record_kind]["evidence"]["authority"]["identity"] = (
                "attacker_adapter"
            )
            seal_state_evidence(graph[record_kind]["evidence"])
            negative_capability_cases.append(
                (
                    "wrong_adapter",
                    candidate_workspace,
                    candidate_records,
                    candidate_catalog,
                    authority_error,
                )
            )

            candidate_workspace, candidate_records, candidate_catalog = (
                make_best_effort_graph(record_kind, "ambiguous")
            )
            graph = by_kind(candidate_records)
            graph[record_kind]["evidence"]["authority"][
                "capability_profile_id"
            ] = "unregistered_observation"
            seal_state_evidence(graph[record_kind]["evidence"])
            negative_capability_cases.append(
                (
                    "wrong_profile",
                    candidate_workspace,
                    candidate_records,
                    candidate_catalog,
                    profile_error,
                )
            )

            candidate_workspace, candidate_records, candidate_catalog = (
                make_best_effort_graph(record_kind, "ambiguous")
            )
            graph = by_kind(candidate_records)
            graph[record_kind]["evidence"]["authority"][
                "capability_profile_revision"
            ] = "r2"
            candidate_catalog["adapter_profiles"][("native_adapter", "r1")].add(
                ("adapter_observation", "r2")
            )
            seal_state_evidence(graph[record_kind]["evidence"])
            negative_capability_cases.append(
                (
                    "wrong_profile_revision",
                    candidate_workspace,
                    candidate_records,
                    candidate_catalog,
                    graph_error,
                )
            )

            candidate_workspace, candidate_records, candidate_catalog = (
                make_best_effort_graph(record_kind, "ambiguous")
            )
            graph = by_kind(candidate_records)
            capability = next(
                item
                for item in graph["CapabilitySetV1"]["capabilities"]
                if item["capability"] == "adapter_observation"
            )
            capability["evidence"]["source_revision"] = "r2"
            negative_capability_cases.append(
                (
                    "wrong_attestation_revision",
                    candidate_workspace,
                    candidate_records,
                    candidate_catalog,
                    "graph_capability_set_attestation",
                )
            )

            candidate_workspace, candidate_records, candidate_catalog = (
                make_best_effort_graph(record_kind, "ambiguous")
            )
            graph = by_kind(candidate_records)
            graph[record_kind]["evidence"]["quality"] = "authoritative"
            seal_state_evidence(graph[record_kind]["evidence"])
            negative_capability_cases.append(
                (
                    "evidence_above_ceiling",
                    candidate_workspace,
                    candidate_records,
                    candidate_catalog,
                    graph_error,
                )
            )

            for (
                label,
                candidate_workspace,
                candidate_records,
                candidate_catalog,
                expected_error,
            ) in negative_capability_cases:
                with self.subTest(record_kind=record_kind, negative=label):
                    self.assertEqual(
                        reachable_graph_error(
                            candidate_records,
                            candidate_workspace,
                            candidate_catalog,
                        ),
                        expected_error,
                    )

        cases = []
        candidate = [
            record
            for record in copy.deepcopy(records)
            if graph_record_kind(record) != "StateEvidenceV1"
        ]
        cases.append((candidate, workspace, "graph_required_kind_missing"))

        candidate = copy.deepcopy(records)
        duplicate_evidence = copy.deepcopy(by_kind(candidate)["DeliveryV1"]["evidence"])
        candidate.append(duplicate_evidence)
        cases.append((candidate, workspace, "graph_duplicate_id:evidence_id"))

        candidate = copy.deepcopy(records)
        by_kind(candidate)["StateEvidenceV1"]["quality"] = "untrusted"
        cases.append((candidate, workspace, "graph_schema:StateEvidenceV1"))

        candidate = copy.deepcopy(records)
        candidate.append(copy.deepcopy(by_kind(candidate)["EndpointV1"]))
        cases.append((candidate, workspace, "graph_duplicate_id:endpoint_id"))

        candidate = copy.deepcopy(records)
        by_kind(candidate)["EndpointV1"]["capability_set_id"] = "caps_missing"
        cases.append((candidate, workspace, "graph_capability_set_missing"))

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        graph["EndpointV1"]["agent_id"] = "agent_missing"
        cases.append((candidate, workspace, "graph_agent_missing"))

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        for kind in ("DeliveryV1", "ReceiptV1"):
            graph[kind]["session_ref_id"] = "session_fake"
            graph[kind]["evidence"]["subject"]["session_ref_id"] = "session_fake"
            seal_state_evidence(graph[kind]["evidence"])
        cases.append(
            (candidate, workspace, "graph_evidence_reference:session_missing")
        )

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        graph["ReceiptV1"]["delivery_id"] = "delivery_fake"
        graph["ReceiptV1"]["evidence"]["subject"]["delivery_id"] = (
            "delivery_fake"
        )
        seal_state_evidence(graph["ReceiptV1"]["evidence"])
        cases.append(
            (candidate, workspace, "graph_evidence_reference:delivery_missing")
        )

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        native_delivery = next(
            capability
            for capability in graph["CapabilitySetV1"]["capabilities"]
            if capability["capability"] == "native_delivery"
        )
        native_delivery["quality"] = "best_effort"
        cases.append((candidate, workspace, "graph_delivery_capability"))

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        graph["CapabilitySetV1"]["capabilities"] = [
            capability
            for capability in graph["CapabilitySetV1"]["capabilities"]
            if capability["capability"] != "native_session_binding"
        ]
        cases.append((candidate, workspace, "graph_session_capability"))

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        graph["CapabilitySetV1"]["capabilities"].append(
            copy.deepcopy(graph["CapabilitySetV1"]["capabilities"][0])
        )
        graph["CapabilitySetV1"]["capabilities"][-1]["quality"] = "best_effort"
        cases.append(
            (
                candidate,
                workspace,
                "graph_capability_set:duplicate_capability_identity",
            )
        )

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        graph["ReceiptV1"]["evidence"]["authority"]["identity"] = (
            "attacker_adapter"
        )
        seal_state_evidence(graph["ReceiptV1"]["evidence"])
        cases.append((candidate, workspace, "graph_receipt_adapter_authority"))

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        graph["DeliveryV1"]["evidence"]["authority"][
            "capability_profile_revision"
        ] = "r2"
        seal_state_evidence(graph["DeliveryV1"]["evidence"])
        cases.append(
            (
                candidate,
                workspace,
                "graph_delivery_profile",
            )
        )

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        graph["EndpointV1"]["adapter_revision"] = "r2"
        graph["CapabilitySetV1"]["capabilities"][0]["evidence"][
            "source_revision"
        ] = "r2"
        for kind in ("SessionRefV1", "DeliveryV1", "ReceiptV1"):
            graph[kind]["evidence"]["authority"]["implementation_revision"] = (
                "r2"
            )
            seal_state_evidence(graph[kind]["evidence"])
        cases.append((candidate, workspace, "graph_endpoint_registration_mismatch"))

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        graph["EndpointV1"]["endpoint_id"] = "endpoint_fake"
        for kind in ("SessionRefV1", "DeliveryV1", "ReceiptV1"):
            graph[kind]["endpoint_id"] = "endpoint_fake"
            graph[kind]["evidence"]["subject"]["endpoint_id"] = "endpoint_fake"
            seal_state_evidence(graph[kind]["evidence"])
        cases.append((candidate, workspace, "graph_endpoint_unregistered"))

        candidate = copy.deepcopy(records)
        graph = by_kind(candidate)
        orphan = copy.deepcopy(graph["CapabilitySetV1"])
        orphan["capability_set_id"] = "caps_orphan"
        orphan["scope"] = {"kind": "workspace"}
        candidate.append(orphan)
        cases.append((candidate, workspace, "graph_workspace_capability_unbound"))

        for candidate, candidate_workspace, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                self.assertEqual(
                    reachable_graph_error(
                        candidate,
                        candidate_workspace,
                        TRUSTED_GRAPH_CATALOG,
                    ),
                    expected_error,
                )

        reference_cases = {
            "message_id": ("msg_renamed", "message_missing"),
            "delivery_id": ("delivery_renamed", "delivery_missing"),
            "attempt_id": ("attempt_renamed", "attempt_missing"),
            "endpoint_id": ("endpoint_renamed", "endpoint_missing"),
            "session_ref_id": ("session_renamed", "session_missing"),
        }
        for field, (value, expected_reference_error) in reference_cases.items():
            candidate = copy.deepcopy(records_with_resolved_standalone)
            standalone = candidate[-1]
            standalone["subject"][field] = value
            if field == "attempt_id":
                standalone["correlation_id"] = value
            seal_state_evidence(standalone)
            with self.subTest(standalone_reference=field):
                self.assertEqual(
                    reachable_graph_error(
                        candidate,
                        workspace,
                        TRUSTED_GRAPH_CATALOG,
                    ),
                    f"graph_evidence_reference:{expected_reference_error}",
                )

        candidate = copy.deepcopy(records_with_resolved_standalone)
        candidate[-1]["correlation_id"] = "attempt_other"
        seal_state_evidence(candidate[-1])
        self.assertEqual(
            reachable_graph_error(
                candidate,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_evidence_reference:attempt_correlation",
        )

        ambiguous = make_delivery_outcome("ambiguous")
        ambiguous["evidence"]["subject"]["session_ref_id"] = "session_missing"
        seal_state_evidence(ambiguous["evidence"])
        candidate = copy.deepcopy(records)
        delivery_index = next(
            index
            for index, record in enumerate(candidate)
            if graph_record_kind(record) == "DeliveryV1"
        )
        candidate[delivery_index] = ambiguous
        receipt = by_kind(candidate)["ReceiptV1"]
        receipt["state"] = "ambiguous"
        receipt["evidence"]["state"] = "ambiguous"
        receipt["evidence"]["quality"] = "best_effort"
        receipt["evidence"]["evidence_kind"] = "adapter_observation"
        receipt.pop("session_ref_id", None)
        receipt["evidence"]["subject"].pop("session_ref_id", None)
        seal_state_evidence(receipt["evidence"])
        self.assertEqual(
            reachable_graph_error(
                candidate,
                workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_evidence_reference:session_missing",
        )

        for project_id in ("amiga", "nuvyr"):
            project_workspace, project_records = make_reachable_graph(project_id)
            project_catalog = replace_project_identity(
                copy.deepcopy(TRUSTED_GRAPH_CATALOG),
                project_id,
            )
            project_evidence = next(
                record
                for record in project_records
                if graph_record_kind(record) == "StateEvidenceV1"
            )
            project_catalog["compatibility_policy"]["manifest_seal"] = (
                project_evidence["legacy_manifest"]["seal"]["value"]
            )
            with self.subTest(project_id=project_id):
                self.assertIsNone(
                    reachable_graph_error(
                        project_records,
                        project_workspace,
                        project_catalog,
                    )
                )

        mixed_workspace, mixed_records = make_reachable_graph("amiga")
        mixed_workspace["projects"].append({"project_id": "nuvyr"})
        mixed_workspace["repositories"].extend(
            [
                {
                    "project_id": "nuvyr",
                    "repo_id": "app",
                    "relative_path": "nuvyr/repos/app",
                },
                {
                    "project_id": "nuvyr",
                    "repo_id": "docs",
                    "relative_path": "nuvyr/repos/docs",
                },
            ]
        )
        receipt_index = next(
            index
            for index, record in enumerate(mixed_records)
            if graph_record_kind(record) == "ReceiptV1"
        )
        mixed_records[receipt_index] = replace_project_identity(
            mixed_records[receipt_index], "nuvyr", "amiga"
        )
        seal_state_evidence(mixed_records[receipt_index]["evidence"])
        self.assertEqual(
            reachable_graph_error(
                mixed_records,
                mixed_workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_project_identity_count",
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

        for required_adapter_field in ("adapter_name", "adapter_version"):
            missing = copy.deepcopy(envelope)
            missing.pop(required_adapter_field)
            self.assert_schema_rejects(
                "EventEnvelopeV1",
                missing,
                f"missing frozen {required_adapter_field}",
            )

        exact_aliases = {
            "adapter_name": "adapter_name",
            "adapter_revision": "adapter_revision",
            "adapter_version": "adapter_version",
            "handler_name": "handler_name",
            "handler_version": "handler_version",
            "handler_revision": "handler_revision",
            "capability_profile": "capability_profile",
            "capability_profile_id": "capability_profile_id",
            "capability_profile_revision": "capability_profile_revision",
            "profile_id": "profile_id",
            "profile_revision": "profile_revision",
            "identity": "identity",
            "exact_identity": "exact_identity",
            "subscription_id": "subscription_id",
            "subscription_revision": "subscription_revision",
            "revision": "revision",
            "registry_revision": "registry_revision",
            "received_at_utc": "received_at_utc",
            "receive_time": "receive_time",
            "receive_time_utc": "receive_time_utc",
            "content_hash": "content_hash",
            "envelope_hash": "envelope_hash",
            "command": "command",
            "path": "path",
            "routing": "routing",
            "retry_policy": "retry_policy",
            "reconciliation_policy": "reconciliation_policy",
            "retention_policy": "retention_policy",
            "feature_flag": "feature_flag",
            "delivery_state": "delivery_state",
            "filesystem_roots": "filesystem_roots",
            "native_session_id": "native_session_id",
        }
        for key, normalized in exact_aliases.items():
            candidate = copy.deepcopy(envelope)
            candidate["payload"] = {"nested": [{"safe": {key: "unsafe"}}]}
            with self.subTest(exact_alias=key):
                self.assertEqual(
                    envelope_error(candidate),
                    f"forbidden_payload_key:{normalized}",
                )
                self.assert_schema_rejects(
                    "EventEnvelopeV1",
                    candidate,
                    "forbidden authority key",
                )

        normalized_aliases = {
            "ＡＤＡＰＴＥＲ－ＮＡＭＥ": "adapter_name",
            "Handler.Version": "handler_version",
            "Capability Profile Revision": "capability_profile_revision",
            "PROFILE/ID": "profile_id",
            "ExactIdentity": "exact_identity",
            "Subscription--Revision": "subscription_revision",
            "Registry Revision": "registry_revision",
            "Receive.Time.UTC": "receive_time_utc",
            "CONTENT-HASH": "content_hash",
            "Envelope Hash": "envelope_hash",
            "Routing Policy": "routing_policy",
        }
        for key, normalized in normalized_aliases.items():
            candidate = copy.deepcopy(envelope)
            candidate["payload"] = {"nested": [{"deeper": {key: "unsafe"}}]}
            with self.subTest(normalized_alias=key):
                self.assertEqual(
                    envelope_error(candidate),
                    f"forbidden_payload_key:{normalized}",
                )

        acronym_aliases = {
            "ADAPTERName": "adapter_name",
            "ＡＤＡＰＴＥＲName": "adapter_name",
            "CAPABILITYProfileID": "capability_profile_id",
            "ＣＡＰＡＢＩＬＩＴＹProfileID": "capability_profile_id",
            "NATIVESessionID": "native_session_id",
            "ＮＡＴＩＶＥSessionID": "native_session_id",
            "REGISTRYRevision": "registry_revision",
            "ＲＥＧＩＳＴＲＹRevision": "registry_revision",
            "SUBSCRIPTIONRevision": "subscription_revision",
            "ＳＵＢＳＣＲＩＰＴＩＯＮRevision": "subscription_revision",
            "RECEIVETimeUTC": "receive_time_utc",
            "ＲＥＣＥＩＶＥTimeUTC": "receive_time_utc",
        }
        for key, normalized in acronym_aliases.items():
            with self.subTest(acronym_alias=key, case="normalization"):
                self.assertEqual(normalize_payload_key(key), normalized)
            candidate = copy.deepcopy(envelope)
            candidate["payload"] = {"nested": [{"safe": {key: "unsafe"}}]}
            with self.subTest(acronym_alias=key, case="nested_rejection"):
                self.assertEqual(
                    envelope_error(candidate),
                    f"forbidden_payload_key:{normalized}",
                )
                self.assert_schema_rejects(
                    "EventEnvelopeV1",
                    candidate,
                    "schema-expressible acronym authority alias",
                )

        def compact_aliases(canonical):
            compact = canonical.replace("_", "")
            uppercase = compact.upper()
            fullwidth_uppercase = "".join(
                chr(ord(character) + 0xFEE0)
                for character in uppercase
            )
            return tuple(dict.fromkeys((compact, uppercase, fullwidth_uppercase)))

        for canonical in sorted(FORBIDDEN_PAYLOAD_KEYS):
            for alias in compact_aliases(canonical):
                candidate = copy.deepcopy(envelope)
                candidate["payload"] = {alias: "unsafe"}
                with self.subTest(
                    compact_authority=canonical,
                    alias=alias,
                ):
                    self.assertEqual(
                        envelope_error(candidate),
                        f"forbidden_payload_key:{canonical}",
                    )
                    self.assert_schema_rejects(
                        "EventEnvelopeV1",
                        candidate,
                        "compact authority alias",
                    )

        recursive_compact_aliases = {
            "projectid": "project_id",
            "PROJECTID": "project_id",
            "PrOjEcTiD": "project_id",
            "NATIVESESSIONID": "native_session_id",
            "ＰＲＯＪＥＣＴＩＤ": "project_id",
            "ＰrＯjＥcＴiＤ": "project_id",
        }
        for depth in range(6):
            for key, canonical in recursive_compact_aliases.items():
                value = {key: "unsafe"}
                for level in range(depth):
                    value = {f"safe_level_{level}": value}
                candidate = copy.deepcopy(envelope)
                candidate["payload"] = value
                with self.subTest(recursive_compact=key, depth=depth):
                    self.assertEqual(
                        envelope_error(candidate),
                        f"forbidden_payload_key:{canonical}",
                    )
                    self.assert_schema_rejects(
                        "EventEnvelopeV1",
                        candidate,
                        "recursive compact authority alias",
                    )

        benign_normalizations = {
            "adapter_summary": "adapter_summary",
            "capability_profile_summary": "capability_profile_summary",
            "native_session_note": "native_session_note",
            "registry_revision_note": "registry_revision_note",
            "project_summary": "project_summary",
            "PROJECTSummary": "project_summary",
            "ＰＲＯＪＥＣＴSummary": "project_summary",
        }
        for key, normalized in benign_normalizations.items():
            with self.subTest(benign_normalization=key):
                self.assertEqual(normalize_payload_key(key), normalized)

        benign = copy.deepcopy(envelope)
        benign["payload"] = {
            "project_summary": "benign",
            "retry_countdown": 2,
            "implementation_notes": "benign",
            "tooling_note": "benign",
            "pathology": "benign",
            "identity_summary": "benign",
            "revision_note": "benign",
            "adapter_summary": "benign",
            "handler_version_note": "benign",
            "capability_profile_summary": "benign",
            "native_session_note": "benign",
            "registry_revision_note": "benign",
            "exact_identity_note": "benign",
            "subscription_revision_note": "benign",
            "receive_time_note": "benign",
            "content_hash_note": "benign",
            "routing_policy_summary": "benign",
            "ＡＤＡＰＴＥＲSummary": "benign",
            "PROJECTSummary": "benign",
            "ＰＲＯＪＥＣＴSummary": "benign",
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
        for payload, label, semantic_error in (
            ({"text": "\ud800"}, "surrogate value", "payload_invalid_unicode_or_control"),
            ({"text": "nul\x00value"}, "NUL value", "payload_invalid_unicode_or_control"),
            ({"nul\x00key": "value"}, "NUL key", "payload_property_name"),
            ({"text": "del\x7fvalue"}, "DEL value", "payload_invalid_unicode_or_control"),
            ({"text": "nel\x85value"}, "NEL value", "payload_invalid_unicode_or_control"),
            ({"text": "line\u2028value"}, "U+2028 value", "payload_invalid_unicode_or_control"),
            ({"text": "line\u2029value"}, "U+2029 value", "payload_invalid_unicode_or_control"),
            ({"nested": [{"revision": "r2"}]}, "nested revision", "forbidden_payload_key:revision"),
            ({"nested": [{"identity": "fake"}]}, "nested identity", "forbidden_payload_key:identity"),
            ({"\ud800": "value"}, "surrogate key", "payload_property_name"),
        ):
            candidate = copy.deepcopy(envelope)
            candidate["payload"] = payload
            with self.subTest(label=label):
                self.assert_schema_rejects("EventEnvelopeV1", candidate, label)
                self.assertEqual(envelope_error(candidate), semantic_error)
        for number in (float("nan"), float("inf"), 1.5):
            candidate = copy.deepcopy(envelope)
            candidate["payload"] = {"value": number}
            with self.subTest(number=repr(number)):
                self.assert_schema_rejects(
                    "EventEnvelopeV1", candidate, "non-canonical number"
                )
                self.assertEqual(
                    envelope_error(candidate), "payload_non_canonical_number"
                )
        huge_integer = copy.deepcopy(envelope)
        huge_integer["payload"] = {"value": 10**10000}
        self.assertEqual(
            envelope_error(huge_integer), "payload_integer_range"
        )

    def test_manifest_seal_bytes_scope_provenance_and_malformed_fail_closed(self):
        evidence = load(FIXTURE_DIR / "valid" / "state-evidence.json")
        workspace = load(FIXTURE_DIR / "valid" / "workspace.json")
        self.assertIsNone(
            manifest_error(evidence, workspace, TRUSTED_COMPATIBILITY_POLICY)
        )
        self.assertIsNone(state_evidence_error(evidence))

        for omitted_field in ("legacy_manifest", "legacy_import"):
            candidate = copy.deepcopy(evidence)
            candidate.pop(omitted_field)
            seal_state_evidence(candidate)
            with self.subTest(omitted_legacy_record=omitted_field):
                self.assert_schema_rejects(
                    "StateEvidenceV1",
                    candidate,
                    f"compatibility import without {omitted_field}",
                )
                self.assertEqual(
                    state_evidence_error(candidate),
                    "compatibility_legacy_fields",
                )

        missing_locator = copy.deepcopy(evidence)
        missing_locator["subject"].pop("legacy_locator")
        seal_state_evidence(missing_locator)
        self.assert_schema_rejects(
            "StateEvidenceV1",
            missing_locator,
            "compatibility import without legacy locator",
        )
        self.assertEqual(
            state_evidence_error(missing_locator),
            "compatibility_legacy_locator",
        )

        wrong_importer_authority = copy.deepcopy(evidence)
        wrong_importer_authority["authority"]["authority_kind"] = (
            "trusted_adapter"
        )
        seal_state_evidence(wrong_importer_authority)
        self.assert_schema_rejects(
            "StateEvidenceV1",
            wrong_importer_authority,
            "compatibility import without trusted importer authority",
        )
        self.assertEqual(
            state_evidence_error(wrong_importer_authority),
            "legacy_quality_escalation",
        )

        for evidence_kind in (
            "adapter_observation",
            "native_delivery_state",
            "exact_session_acknowledgment",
            "exact_session_binding",
        ):
            if evidence_kind == "exact_session_binding":
                non_compatibility = load(
                    FIXTURE_DIR / "valid" / "session-ref.json"
                )["evidence"]
            else:
                non_compatibility = load(
                    FIXTURE_DIR / "valid" / "delivery.json"
                )["evidence"]
                non_compatibility["state"] = "persisted"
                non_compatibility["evidence_kind"] = evidence_kind
                seal_state_evidence(non_compatibility)
            self.assertEqual(
                self.errors("StateEvidenceV1", non_compatibility),
                [],
            )
            for forbidden_field in ("legacy_manifest", "legacy_import"):
                candidate = copy.deepcopy(non_compatibility)
                candidate[forbidden_field] = copy.deepcopy(
                    evidence[forbidden_field]
                )
                seal_state_evidence(candidate)
                with self.subTest(
                    non_compatibility_kind=evidence_kind,
                    forbidden_legacy_field=forbidden_field,
                ):
                    self.assert_schema_rejects(
                        "StateEvidenceV1",
                        candidate,
                        "legacy authority on non-compatibility evidence",
                    )
                    self.assertEqual(
                        state_evidence_error(candidate),
                        "unexpected_legacy_fields",
                    )

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
        candidate["correlation_id"] = "source_tx1"
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
        cases.append((candidate, "manifest_entry_key"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_import"]["source_boundary"]["immutable"] = False
        recalculate_legacy_integrity(candidate)
        cases.append((candidate, "import_source_boundary_mismatch"))

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
                    manifest_error(
                        candidate,
                        workspace,
                        TRUSTED_COMPATIBILITY_POLICY,
                    ),
                    expected_error,
                )

        correlated_attacker_cases = []
        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["manifest_id"] = "manifest_attacker"
        candidate["legacy_import"]["manifest_id"] = "manifest_attacker"
        recalculate_legacy_integrity(candidate)
        correlated_attacker_cases.append(
            (candidate, "untrusted_manifest_policy")
        )

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["cutoff_policy_revision"] = "p2"
        candidate["legacy_manifest"]["publication"][
            "cutoff_policy_revision"
        ] = "p2"
        for entry in candidate["legacy_manifest"]["entries"]:
            entry["cutoff_policy_revision"] = "p2"
        candidate["legacy_import"]["cutoff_policy_revision"] = "p2"
        candidate["legacy_import"]["entry_key"][
            "cutoff_policy_revision"
        ] = "p2"
        candidate["authority"]["capability_profile_revision"] = "p2"
        recalculate_legacy_integrity(candidate)
        correlated_attacker_cases.append(
            (candidate, "untrusted_manifest_policy")
        )

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["publication"]["publisher"] = {
            "identity": "attacker_publisher",
            "revision": "r9",
        }
        recalculate_legacy_integrity(candidate)
        correlated_attacker_cases.append(
            (candidate, "untrusted_manifest_publisher")
        )

        candidate = copy.deepcopy(evidence)
        for entry in candidate["legacy_manifest"]["entries"]:
            entry["trusted_importer"] = {
                "identity": "attacker_importer",
                "revision": "r9",
            }
        candidate["legacy_import"]["importer"] = {
            "identity": "attacker_importer",
            "revision": "r9",
        }
        candidate["authority"]["identity"] = "attacker_importer"
        candidate["authority"]["implementation_revision"] = "r9"
        recalculate_legacy_integrity(candidate)
        correlated_attacker_cases.append(
            (candidate, "entry_untrusted_importer")
        )

        candidate = copy.deepcopy(evidence)
        attacker_boundary = {
            "kind": "source_snapshot",
            "identity": "attacker_snapshot",
            "immutable": True,
        }
        candidate["legacy_manifest"]["publication"][
            "source_boundary"
        ] = attacker_boundary
        for entry in candidate["legacy_manifest"]["entries"]:
            entry["source_boundary"] = copy.deepcopy(attacker_boundary)
        candidate["legacy_import"]["source_boundary"] = copy.deepcopy(
            attacker_boundary
        )
        recalculate_legacy_integrity(candidate)
        correlated_attacker_cases.append(
            (candidate, "publication_boundary_authority")
        )

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["entries"].pop()
        recalculate_legacy_integrity(candidate)
        correlated_attacker_cases.append((candidate, "manifest_entry_set"))

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["entries"][1][
            "transaction_id"
        ] = "attacker_non_selected_tx"
        candidate["legacy_manifest"]["entries"][1][
            "provenance_id"
        ] = "attacker_non_selected_prov"
        recalculate_legacy_integrity(candidate)
        correlated_attacker_cases.append(
            (candidate, "untrusted_manifest_seal")
        )

        candidate = copy.deepcopy(evidence)
        candidate["legacy_manifest"]["entries"][1][
            "source_registry_revision"
        ] = "attacker_registry"
        recalculate_legacy_integrity(candidate)
        correlated_attacker_cases.append(
            (candidate, "entry_source_scope_mismatch")
        )

        candidate = copy.deepcopy(evidence)
        candidate["correlation_id"] = "attacker_import_tx"
        seal_state_evidence(candidate)
        correlated_attacker_cases.append(
            (candidate, "import_transaction_correlation")
        )

        for candidate, expected_error in correlated_attacker_cases:
            with self.subTest(attacker_reseal=expected_error):
                self.assertEqual(
                    manifest_error(
                        candidate,
                        workspace,
                        TRUSTED_COMPATIBILITY_POLICY,
                    ),
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
                manifest_error(
                    candidate,
                    workspace,
                    TRUSTED_COMPATIBILITY_POLICY,
                ),
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
            manifest_error(
                escalated,
                workspace,
                TRUSTED_COMPATIBILITY_POLICY,
            ),
            "legacy_quality_escalation",
        )

    def test_delivery_outcomes_are_materially_valid_and_state_bound(self):
        delivery_outcomes = (
            "pending",
            "ambiguous",
            "deferred_busy",
            "rejected_before_acceptance",
            "pull_pending",
            "accepted",
            "completed",
        )
        for outcome in delivery_outcomes:
            for evidence_state in STATE_EVIDENCE_STATES:
                delivery = make_delivery_outcome(outcome, evidence_state)
                allowed = (
                    evidence_state in PENDING_DELIVERY_STATES
                    if outcome == "pending"
                    else evidence_state == outcome
                )
                with self.subTest(
                    outcome=outcome,
                    evidence_state=evidence_state,
                    surface="schema_and_semantic",
                ):
                    if allowed:
                        self.assertEqual(
                            self.errors("DeliveryV1", delivery),
                            [],
                        )
                        self.assertIsNone(outcome_error(delivery))
                    else:
                        self.assert_schema_rejects(
                            "DeliveryV1",
                            delivery,
                            "outcome/evidence state mismatch",
                        )
                        self.assertEqual(
                            outcome_error(delivery),
                            "outcome_evidence_state",
                        )

                graph_workspace, graph_records = make_reachable_graph()
                for index, record in enumerate(graph_records):
                    kind = graph_record_kind(record)
                    if kind == "DeliveryV1":
                        graph_records[index] = copy.deepcopy(delivery)
                    elif kind == "ReceiptV1":
                        graph_records[index] = make_receipt_state(
                            evidence_state
                        )
                with self.subTest(
                    outcome=outcome,
                    evidence_state=evidence_state,
                    surface="reachable_graph",
                ):
                    self.assertEqual(
                        reachable_graph_error(
                            graph_records,
                            graph_workspace,
                            TRUSTED_GRAPH_CATALOG,
                        ),
                        None if allowed else "graph_schema:DeliveryV1",
                    )

        invalid_pending_state = make_delivery_outcome(
            "pending",
            "not_a_state",
        )
        self.assert_schema_rejects(
            "DeliveryV1",
            invalid_pending_state,
            "pending with state outside the closed evidence vocabulary",
        )
        self.assertEqual(
            outcome_error(invalid_pending_state),
            "outcome_evidence_state",
        )
        graph_workspace, graph_records = make_reachable_graph()
        for index, record in enumerate(graph_records):
            if graph_record_kind(record) == "DeliveryV1":
                graph_records[index] = invalid_pending_state
        self.assertEqual(
            reachable_graph_error(
                graph_records,
                graph_workspace,
                TRUSTED_GRAPH_CATALOG,
            ),
            "graph_schema:DeliveryV1",
        )

    def test_receipt_outcomes_are_materially_valid_and_state_bound(self):
        for state in STATE_EVIDENCE_STATES:
            for evidence_state in STATE_EVIDENCE_STATES:
                receipt = make_receipt_state(state, evidence_state)
                allowed = evidence_state == state
                with self.subTest(
                    state=state,
                    evidence_state=evidence_state,
                    surface="schema_and_semantic",
                ):
                    if allowed:
                        self.assertEqual(
                            self.errors("ReceiptV1", receipt),
                            [],
                        )
                        self.assertIsNone(outcome_error(receipt))
                    else:
                        self.assert_schema_rejects(
                            "ReceiptV1",
                            receipt,
                            "receipt/evidence state mismatch",
                        )
                        self.assertEqual(
                            outcome_error(receipt),
                            "outcome_evidence_state",
                        )

                delivery = (
                    make_delivery_outcome("pending", evidence_state)
                    if evidence_state in PENDING_DELIVERY_STATES
                    else make_delivery_outcome(evidence_state)
                )
                graph_workspace, graph_records = make_reachable_graph()
                for index, record in enumerate(graph_records):
                    kind = graph_record_kind(record)
                    if kind == "DeliveryV1":
                        graph_records[index] = delivery
                    elif kind == "ReceiptV1":
                        graph_records[index] = copy.deepcopy(receipt)
                with self.subTest(
                    state=state,
                    evidence_state=evidence_state,
                    surface="reachable_graph",
                ):
                    self.assertEqual(
                        reachable_graph_error(
                            graph_records,
                            graph_workspace,
                            TRUSTED_GRAPH_CATALOG,
                        ),
                        None if allowed else "graph_schema:ReceiptV1",
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

        project_unbound = copy.deepcopy(session_ref)
        project_unbound.pop("repository_binding")
        project_unbound["evidence"]["subject"].pop("repository_binding")
        seal_state_evidence(project_unbound["evidence"])
        self.assertEqual(self.errors("SessionRefV1", project_unbound), [])
        self.assertIsNone(session_ref_error(project_unbound, workspace))

        workspace_unbound = copy.deepcopy(project_unbound)
        workspace_unbound["scope"] = {"kind": "workspace"}
        workspace_unbound["evidence"]["scope"] = {"kind": "workspace"}
        seal_state_evidence(workspace_unbound["evidence"])
        self.assertEqual(self.errors("SessionRefV1", workspace_unbound), [])
        self.assertIsNone(session_ref_error(workspace_unbound, workspace))

        graph_workspace, graph_records = make_reachable_graph()
        graph_session = next(
            record
            for record in graph_records
            if graph_record_kind(record) == "SessionRefV1"
        )
        graph_session.pop("repository_binding")
        graph_session["evidence"]["subject"].pop("repository_binding")
        seal_state_evidence(graph_session["evidence"])
        self.assertIsNone(
            reachable_graph_error(
                graph_records,
                graph_workspace,
                TRUSTED_GRAPH_CATALOG,
            )
        )

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
        candidate = copy.deepcopy(project_unbound)
        candidate["evidence"]["subject"]["repository_binding"] = copy.deepcopy(
            session_ref["repository_binding"]
        )
        seal_state_evidence(candidate["evidence"])
        self.assertEqual(
            session_ref_error(candidate, workspace),
            "session_subject_mismatch",
        )
        candidate = copy.deepcopy(session_ref)
        candidate["evidence"]["subject"].pop("repository_binding")
        seal_state_evidence(candidate["evidence"])
        self.assertEqual(
            session_ref_error(candidate, workspace),
            "session_subject_mismatch",
        )

        aliased_workspace = copy.deepcopy(workspace)
        aliased_workspace["projects"].append({"project_id": "other"})
        aliased_workspace["repositories"].append(
            {
                "project_id": "other",
                "repo_id": "app_alias",
                "relative_path": "repos/app",
            }
        )
        self.assertIsNone(workspace_registry_error(aliased_workspace))
        self.assertEqual(
            session_ref_error(session_ref, aliased_workspace),
            "repository_binding_cross_project_alias",
        )
        self.assertIsNone(
            session_ref_error(project_unbound, aliased_workspace)
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

        best_effort_without_constraints = copy.deepcopy(capability_set)
        best_effort_capability = next(
            capability
            for capability in best_effort_without_constraints["capabilities"]
            if capability["quality"] == "best_effort"
        )
        best_effort_capability.pop("constraints")
        self.assertEqual(
            self.errors("CapabilitySetV1", best_effort_without_constraints),
            [],
        )
        self.assertIsNone(capability_set_error(best_effort_without_constraints))

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
