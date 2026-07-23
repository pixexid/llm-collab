"""Fail-closed Runtime Adapter V1 conformance claim publication."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Mapping

from llm_collab.runtime_adapter_conformance import ClauseOccurrence, extract_clause_occurrences
from llm_collab.runtime_adapter_fixtures import (
    FIXTURES,
    POLARITY_CONFORMING,
    POLARITY_VIOLATING,
    ExpectedRefusal,
    ExpectedResult,
    RuntimeAdapterFixture,
    _thaw,
    validate_fixtures,
)
from llm_collab.runtime_adapter_reference import ReferenceAdapter


UNREFERENCED = "unreferenced"
REFERENCED_NOT_EXERCISED = "referenced_not_exercised"
EXERCISED_CONFORMING = "exercised_conforming"
_GAP_STATES = frozenset((UNREFERENCED, REFERENCED_NOT_EXERCISED))


@dataclass(frozen=True)
class ClaimFailure:
    gaps: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class ClaimSuccess:
    artifact: Mapping[str, object]


def build_claim(
    protocol_text: str,
    *,
    fixtures: Iterable[RuntimeAdapterFixture] = FIXTURES,
) -> ClaimSuccess | ClaimFailure:
    checked = validate_fixtures(protocol_text, fixtures)
    clauses = tuple(extract_clause_occurrences(protocol_text))
    exercised = _replayed_fixture_ids(checked)
    return _claim_from_checked(clauses, checked, exercised)


def _claim_from_checked(
    clauses: Iterable[ClauseOccurrence],
    checked_fixtures: Iterable[RuntimeAdapterFixture],
    exercised_fixture_ids: set[str],
) -> ClaimSuccess | ClaimFailure:
    states = _coverage_states(clauses, checked_fixtures, exercised_fixture_ids)
    gaps = tuple(
        {"clause_key": key, "state": state}
        for key, state in sorted(states.items())
        if state in _GAP_STATES
    )
    if gaps:
        return ClaimFailure(gaps)
    return ClaimSuccess(
        {
            "schema_version": 1,
            "protocol": "runtime-adapter-jsonrpc-v1",
            "claim": "exercised_conforming",
            "clauses": [{"clause_key": key, "state": states[key]} for key in sorted(states)],
            "gaps": [],
        }
    )


def publish_claim(protocol_text: str, output_path: str | Path, *, repo_root: str | Path) -> ClaimSuccess | ClaimFailure:
    path = Path(output_path).resolve()
    root = Path(repo_root).resolve()
    if path == root or root not in path.parents:
        raise ValueError("claim output path must be inside the repository")
    result = build_claim(protocol_text)
    if isinstance(result, ClaimFailure):
        return result
    path.write_text(json.dumps(result.artifact, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def _coverage_states(
    clauses: Iterable[ClauseOccurrence],
    fixtures: Iterable[RuntimeAdapterFixture],
    exercised_fixture_ids: set[str],
) -> dict[str, str]:
    states = {clause.clause_key: UNREFERENCED for clause in clauses}
    keywords = {clause.clause_key: clause.keyword for clause in clauses}
    for fixture in fixtures:
        for ref in fixture.clause_refs:
            exercised = fixture.fixture_id in exercised_fixture_ids
            keyword = keywords[ref.clause_key]
            relevant = (
                (keyword == "MUST NOT" and ref.polarity == "violating")
                or (keyword == "MUST NOT" and ref.non_classifying)
                or keyword != "MUST NOT"
            )
            if exercised and relevant:
                states[ref.clause_key] = EXERCISED_CONFORMING
            elif relevant and states[ref.clause_key] == UNREFERENCED:
                states[ref.clause_key] = REFERENCED_NOT_EXERCISED
    return states


def _replayed_fixture_ids(fixtures: Iterable[RuntimeAdapterFixture]) -> set[str]:
    return {fixture.fixture_id for fixture in fixtures if _fixture_replays(fixture)}


def _fixture_replays(fixture: RuntimeAdapterFixture) -> bool:
    adapter = ReferenceAdapter()
    saw_expected = False
    host_traces = [trace for trace in fixture.trace if trace.sender == "host" and trace.receiver == "adapter"]
    for index, trace in enumerate(host_traces):
        frame = _thaw(trace.frame)
        response = adapter.handle_text(json.dumps(frame, sort_keys=True, separators=(",", ":")))
        final_frame = index == len(host_traces) - 1
        if fixture.polarity == POLARITY_VIOLATING and final_frame and _matches_refusal(fixture.expectation, response):
            return True
        if response is None:
            return False
        payload = json.loads(response)
        if "error" in payload:
            return False
        if fixture.polarity == POLARITY_CONFORMING and _matches_result(fixture.expectation, frame, payload):
            saw_expected = True
    return saw_expected


def _matches_result(expectation: ExpectedResult | ExpectedRefusal, frame: Mapping[str, object], payload: object) -> bool:
    return (
        isinstance(expectation, ExpectedResult)
        and isinstance(payload, Mapping)
        and frame.get("method") == expectation.method
        and payload.get("result") == expectation.result
    )


def _matches_refusal(expectation: ExpectedResult | ExpectedRefusal, response: str | bytes | None) -> bool:
    if not isinstance(expectation, ExpectedRefusal):
        return False
    if response is None:
        return not expectation.response_emitted and expectation.closes_connection
    payload = json.loads(response)
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return False
    return error.get("code") == expectation.error_code and error.get("message") == expectation.error_name
