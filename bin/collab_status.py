#!/usr/bin/env python3
"""Render a bounded, read-only status view from the canonical ledger."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

from _helpers import config_get, project_state_root
from _session_autobridge import parse_iso8601
from llm_collab.ledger import LedgerPaths, LedgerStore
from llm_collab.ledger.paths import validate_project_id


MAX_ROWS = 5_000
MAX_TOTAL_ROWS = 15_000
MAX_EVENTS = 50
STALE_AFTER_SECONDS = 60 * 60


class StatusError(RuntimeError):
    """Raised when the status view cannot prove a complete bounded result."""


class RowBudget:
    def __init__(self) -> None:
        self.used = 0

    def take(self, rows: list[tuple[Any, ...]], label: str) -> list[tuple[Any, ...]]:
        self.used += len(rows)
        if self.used > MAX_TOTAL_ROWS:
            raise StatusError(f"status ledger scan exceeds {MAX_TOTAL_ROWS} rows ({label})")
        return rows


def _rows(store: LedgerStore, sql: str, parameters: tuple[object, ...], label: str, budget: RowBudget) -> list[tuple[Any, ...]]:
    cursor = store._connection.execute(sql, (*parameters, MAX_ROWS + 1))
    rows = cursor.fetchmany(MAX_ROWS + 1)
    budget.take(rows, label)
    if len(rows) > MAX_ROWS:
        raise StatusError(f"status ledger scan exceeds {MAX_ROWS} rows ({label})")
    return rows


def _timestamp(value: object, label: str) -> datetime:
    parsed = parse_iso8601(str(value))
    if parsed is None or parsed.tzinfo is None:
        raise StatusError(f"canonical ledger contains an invalid {label} timestamp")
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: str, now: datetime) -> int | None:
    parsed = parse_iso8601(value)
    if parsed is None or parsed.tzinfo is None:
        return None
    return max(0, int((now - parsed.astimezone(timezone.utc)).total_seconds()))


def _validate_project(store: LedgerStore, project_id: str) -> str:
    project_id = validate_project_id(project_id)
    workspace_id = store.paths.workspace_id
    revision = store.current_registry_revision(workspace_id=workspace_id)
    if project_id not in store.registered_project_ids(
        workspace_id=workspace_id, registry_revision=revision
    ):
        raise StatusError(f"project is absent from the current registry snapshot: {project_id}")
    return project_id


def _active_bindings(
    store: LedgerStore, project_id: str, now: datetime, budget: RowBudget
) -> list[dict[str, object]]:
    workspace_id = store.paths.workspace_id
    rows = _rows(
        store,
        """
        WITH freeze_activity AS (
            SELECT binding_id, MAX(created_at_utc) AS last_activity_utc
            FROM canonical_delivery_attempt_binding_freezes
            WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
            GROUP BY binding_id
        ), receipt_activity AS (
            SELECT f.binding_id, MAX(r.created_at_utc) AS last_activity_utc
            FROM canonical_delivery_attempt_binding_freezes AS f
            JOIN canonical_delivery_receipts AS r
              ON r.workspace_id = f.workspace_id
             AND r.scope_kind = f.scope_kind
             AND r.scope_identity = f.scope_identity
             AND r.message_id = f.message_id
             AND r.delivery_id = f.delivery_id
             AND r.attempt_id = f.attempt_id
            WHERE f.workspace_id = ? AND f.scope_kind = 'project' AND f.scope_identity = ?
            GROUP BY f.binding_id
        ), transition_activity AS (
            SELECT binding_id, MAX(created_at_utc) AS last_activity_utc
            FROM (
                SELECT predecessor_binding_id AS binding_id, created_at_utc
                FROM conversation_binding_transition_audit
                WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
                UNION ALL
                SELECT successor_binding_id AS binding_id, created_at_utc
                FROM conversation_binding_transition_audit
                WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
            )
            GROUP BY binding_id
        )
        SELECT p.agent_id, b.conversation_id, b.native_session_id, b.binding_id,
               b.generation, b.state, b.provider_id, b.endpoint_id,
               b.runtime_instance_id, b.registered_at_utc,
               MAX(
                   b.registered_at_utc,
                   COALESCE(f.last_activity_utc, b.registered_at_utc),
                   COALESCE(r.last_activity_utc, b.registered_at_utc),
                   COALESCE(t.last_activity_utc, b.registered_at_utc)
               ) AS last_activity_utc
        FROM conversation_bindings AS b
        JOIN conversation_participants AS p
          ON p.workspace_id = b.workspace_id
         AND p.scope_kind = b.scope_kind
         AND p.scope_identity = b.scope_identity
         AND p.conversation_id = b.conversation_id
         AND p.participant_id = b.participant_id
        LEFT JOIN freeze_activity AS f ON f.binding_id = b.binding_id
        LEFT JOIN receipt_activity AS r ON r.binding_id = b.binding_id
        LEFT JOIN transition_activity AS t ON t.binding_id = b.binding_id
        WHERE b.workspace_id = ? AND b.scope_kind = 'project' AND b.scope_identity = ?
          AND b.mutation_capable = 1 AND b.state IN ('active', 'draining')
        ORDER BY b.conversation_id, p.agent_id, b.generation DESC, b.binding_id
        LIMIT ?
        """,
        (
            workspace_id,
            project_id,
            workspace_id,
            project_id,
            workspace_id,
            project_id,
            workspace_id,
            project_id,
            workspace_id,
            project_id,
        ),
        "active bindings",
        budget,
    )
    result = []
    for row in rows:
        last_activity = str(row[10])
        _timestamp(last_activity, "binding activity")
        age = _age_seconds(last_activity, now)
        result.append(
            {
                "agent": str(row[0]).removeprefix("agent_"),
                "chat": row[1],
                "native_session": row[2],
                "binding": row[3],
                "generation": row[4],
                "status": row[5],
                "provider": row[6],
                "endpoint": row[7],
                "runtime_instance": row[8],
                "registered_at_utc": row[9],
                "last_activity_utc": last_activity,
                "staleness_seconds": age,
                "stale": age is not None and age > STALE_AFTER_SECONDS,
            }
        )
    return result


def _pending_attempts(
    store: LedgerStore, project_id: str, budget: RowBudget
) -> dict[str, int]:
    workspace_id = store.paths.workspace_id
    deliveries = _rows(
        store,
        """
        SELECT message_id, delivery_id
        FROM canonical_deliveries
        WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
        ORDER BY created_at_utc, delivery_id
        LIMIT ?
        """,
        (workspace_id, project_id),
        "deliveries",
        budget,
    )
    counts: Counter[str] = Counter()
    for message_id, delivery_id in deliveries:
        delivery = store.read_canonical_delivery(
            workspace_id=workspace_id,
            scope_kind="project",
            scope_identity=project_id,
            message_id=str(message_id),
            delivery_id=str(delivery_id),
        )
        if delivery is None or delivery.get("outcome") not in {
            "pending",
            "pull_pending",
            "deferred_busy",
        }:
            continue
        attempts = _rows(
            store,
            """
            SELECT a.attempt_id, f.binding_id
            FROM canonical_delivery_attempts AS a
            LEFT JOIN canonical_delivery_attempt_binding_freezes AS f
              ON f.workspace_id = a.workspace_id
             AND f.scope_kind = a.scope_kind
             AND f.scope_identity = a.scope_identity
             AND f.message_id = a.message_id
             AND f.delivery_id = a.delivery_id
             AND f.attempt_id = a.attempt_id
            WHERE a.workspace_id = ? AND a.scope_kind = 'project' AND a.scope_identity = ?
              AND a.message_id = ? AND a.delivery_id = ?
            ORDER BY a.attempt_index, a.attempt_id
            LIMIT ?
            """,
            (workspace_id, project_id, str(message_id), str(delivery_id)),
            "pending attempts",
            budget,
        )
        for _attempt_id, binding_id in attempts:
            counts[str(binding_id or "unbound")] += 1
    return dict(sorted(counts.items()))


def _recent_events(
    store: LedgerStore, project_id: str, budget: RowBudget
) -> list[dict[str, object]]:
    workspace_id = store.paths.workspace_id
    events: list[dict[str, object]] = []
    receipts = _rows(
        store,
        """
        SELECT message_id, delivery_id, attempt_id, receipt_id, state, created_at_utc
        FROM canonical_delivery_receipts
        WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
          AND state IN ('ambiguous', 'rejected_before_acceptance', 'deferred_busy', 'pull_pending')
        ORDER BY created_at_utc DESC, receipt_id DESC
        LIMIT ?
        """,
        (workspace_id, project_id),
        "dead-letter receipts",
        budget,
    )
    for message_id, delivery_id, attempt_id, receipt_id, state, created_at in receipts:
        store.read_canonical_receipt(
            workspace_id=workspace_id,
            scope_kind="project",
            scope_identity=project_id,
            message_id=str(message_id),
            delivery_id=str(delivery_id),
            attempt_id=str(attempt_id),
            receipt_id=str(receipt_id),
        )
        events.append(
            {
                "kind": "dead_letter",
                "state": state,
                "occurred_at_utc": created_at,
                "message_id": message_id,
                "delivery_id": delivery_id,
                "attempt_id": attempt_id,
            }
        )

    bindings = _rows(
        store,
        """
        SELECT p.agent_id, b.conversation_id, b.binding_id, b.generation,
               b.state, b.registered_at_utc
        FROM conversation_bindings AS b
        JOIN conversation_participants AS p
          ON p.workspace_id = b.workspace_id
         AND p.scope_kind = b.scope_kind
         AND p.scope_identity = b.scope_identity
         AND p.conversation_id = b.conversation_id
         AND p.participant_id = b.participant_id
        WHERE b.workspace_id = ? AND b.scope_kind = 'project' AND b.scope_identity = ?
          AND b.state IN ('quarantined', 'unverified')
        ORDER BY b.registered_at_utc DESC, b.binding_id DESC
        LIMIT ?
        """,
        (workspace_id, project_id),
        "quarantine bindings",
        budget,
    )
    for agent, chat, binding_id, generation, state, occurred_at in bindings:
        events.append(
            {
                "kind": "quarantine" if state == "quarantined" else "health",
                "state": state,
                "occurred_at_utc": occurred_at,
                "agent": str(agent).removeprefix("agent_"),
                "chat": chat,
                "binding": binding_id,
                "generation": generation,
            }
        )

    audits = _rows(
        store,
        """
        SELECT action, result, occurred_at_utc, audit_id
        FROM observation_audit
        WHERE workspace_id = ? AND project_id = ?
        ORDER BY occurred_at_utc DESC, audit_id DESC
        LIMIT ?
        """,
        (workspace_id, project_id),
        "health audit events",
        budget,
    )
    for action, result, occurred_at, audit_id in audits:
        events.append(
            {
                "kind": "health",
                "action": action,
                "result": result,
                "occurred_at_utc": occurred_at,
                "audit_id": audit_id,
            }
        )

    for event in events:
        _timestamp(event["occurred_at_utc"], "event")
    events.sort(key=lambda item: str(item["occurred_at_utc"]), reverse=True)
    return events[:MAX_EVENTS]


def render_status(
    store: LedgerStore,
    project_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Render one complete project view using a query-only LedgerStore reader."""
    project_id = _validate_project(store, project_id)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    budget = RowBudget()
    return {
        "schema_version": 1,
        "project": project_id,
        "workspace_id": store.paths.workspace_id,
        "generated_at_utc": now.isoformat(),
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "active_bindings": _active_bindings(store, project_id, now, budget),
        "pending_delivery_attempts": _pending_attempts(store, project_id, budget),
        "recent_events": _recent_events(store, project_id, budget),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show canonical llm-collab status")
    parser.add_argument("--project", required=True, help="exact registered project id")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser


def _text_report(status: dict[str, object]) -> str:
    lines = [
        f"Project: {status['project']} (workspace {status['workspace_id']})",
        f"Generated: {status['generated_at_utc']}",
        "",
        "Active bindings:",
    ]
    bindings = status["active_bindings"]
    if bindings:
        for binding in bindings:  # type: ignore[union-attr]
            lines.append(
                "  {agent} / {chat} / {native_session} — {status}, generation {generation}, "
                "last activity {last_activity_utc}, stale={stale}".format(**binding)
            )
    else:
        lines.append("  none")
    lines.append("")
    lines.append("Pending delivery attempts by binding:")
    pending = status["pending_delivery_attempts"]
    if pending:
        lines.extend(f"  {binding}: {count}" for binding, count in pending.items())  # type: ignore[union-attr]
    else:
        lines.append("  none")
    lines.append("")
    lines.append("Recent health/dead-letter/quarantine events:")
    events = status["recent_events"]
    if events:
        lines.extend(
            f"  {event['occurred_at_utc']} — {event['kind']} {event.get('state', event.get('action', ''))}"  # type: ignore[union-attr]
            for event in events
        )
    else:
        lines.append("  none")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace_id = config_get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise StatusError("collab.config.json has no workspace_id")
    paths = LedgerPaths.derive(project_state_root(), workspace_id)
    with LedgerStore.open_reader(paths) as store:
        status = render_status(store, args.project)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(_text_report(status))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (StatusError, FileNotFoundError, ValueError) as error:
        print(f"[error] {error}", file=sys.stderr)
        raise SystemExit(1)
