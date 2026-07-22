"""Inert canonical message APIs."""

from llm_collab.ledger.store import CanonicalConflictError

from .messages import (
    CanonicalIntegrityError,
    create_or_return_equivalent,
    project_message_v1,
    read_message,
)
from .delivery import (
    append_receipt,
    create_attempt,
    create_deliveries,
    project_delivery_v1,
    project_receipt_v1,
)
from .control import (
    CanonicalControlError,
    append_acknowledgment_receipt,
    append_dead_letter_receipt,
    inspect_delivery,
    inspect_legacy_manifest_provenance,
    inspect_receipt,
    require_canonical_write_gate,
)

__all__ = [
    "CanonicalControlError",
    "CanonicalConflictError",
    "CanonicalIntegrityError",
    "append_acknowledgment_receipt",
    "append_dead_letter_receipt",
    "append_receipt",
    "create_attempt",
    "create_deliveries",
    "create_or_return_equivalent",
    "inspect_delivery",
    "inspect_legacy_manifest_provenance",
    "inspect_receipt",
    "project_delivery_v1",
    "project_message_v1",
    "project_receipt_v1",
    "read_message",
    "require_canonical_write_gate",
]

# P2d compatibility projections intentionally remain under
# llm_collab.compatibility. Keeping them out of this canonical namespace avoids
# treating projected v2 shapes as canonical authority.
