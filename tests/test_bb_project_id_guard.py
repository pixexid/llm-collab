"""GH-731 guard: one author for ``bb.project_id`` validation.

The production Python trees are parsed with ``ast``. Any literal
``project.get("bb")``/``bb.get("project_id")`` or subscript equivalent is a
direct registry read and fails unless it is the shared validator. One existing
``watch_inbox._bb_start_inputs`` read is enumerated narrowly: it consumes the
raw identifier but does not implement the padding rule. A second read at either
named site fails by count, and ``.strip()`` on that grandfathered raw value also
fails, so it cannot quietly become another padding authority.

This does not perform general dataflow analysis. A key assembled dynamically or
passed through another function is outside the guard; the defect class being
closed is a consumer directly re-authoring the literal registry check.
"""

from __future__ import annotations

import ast
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_DIRS = ("bin", "llm_collab", "scripts")

# The sole validation authority. Exactly one direct read is expected.
AUTHORITY = ("llm_collab/bb_client.py", "bb_project_id_from_project")

# Existing bootstrap input consumption: it forwards the raw native id and has
# no padding check. This frozen GH-731 lane names three validation authorities;
# the guard permits this one exact pre-existing read, but not a second read or a
# ``strip`` check on its result.
RAW_CONSUMER = ("bin/watch_inbox.py", "_bb_start_inputs")

EXPECTED_READS = {AUTHORITY: 1, RAW_CONSUMER: 1}


def _literal_key(node: ast.AST, key: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == key


def _get_key(node: ast.AST, key: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and bool(node.args)
        and _literal_key(node.args[0], key)
    )


def _subscript_key(node: ast.AST, key: str) -> bool:
    return isinstance(node, ast.Subscript) and _literal_key(node.slice, key)


def _bb_block_read(node: ast.AST) -> bool:
    return _get_key(node, "bb") or _subscript_key(node, "bb")


def _bb_project_id_read(node: ast.AST, aliases: set[str]) -> bool:
    if not (_get_key(node, "project_id") or _subscript_key(node, "project_id")):
        return False
    receiver = node.func.value if isinstance(node, ast.Call) else node.value
    return (
        isinstance(receiver, ast.Name) and receiver.id in aliases
    ) or _bb_block_read(receiver)


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for item in node.elts for name in _target_names(item)}
    return set()


class RegistryReadVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scopes = ["<module>"]
        self.aliases = [{"bb"}]
        self.tainted = [set()]
        self.reads: list[tuple[tuple[str, str], int]] = []
        self.recorded_read_ids: set[int] = set()
        self.hits: list[str] = []

    def _record_read(self, node: ast.AST) -> None:
        self.reads.append(((self.relative_path, self.scopes[-1]), node.lineno))
        self.recorded_read_ids.add(id(node))

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.aliases.append({"bb"})
        self.tainted.append(set())
        for statement in node.body:
            self.visit(statement)
        self.tainted.pop()
        self.aliases.pop()
        self.scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Assign(self, node: ast.Assign) -> None:
        targets = {name for target in node.targets for name in _target_names(target)}
        if _bb_block_read(node.value):
            self.aliases[-1].update(targets)
        if _bb_project_id_read(node.value, self.aliases[-1]):
            self._record_read(node.value)
            self.tainted[-1].update(targets)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _bb_project_id_read(node, self.aliases[-1]) and id(node) not in self.recorded_read_ids:
            self._record_read(node)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "strip"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.tainted[-1]
            and (self.relative_path, self.scopes[-1]) != AUTHORITY
        ):
            self.hits.append(
                f"{self.relative_path}:{node.lineno}: independent bb.project_id padding check"
            )
        self.generic_visit(node)


def independent_bb_project_id_validations() -> list[str]:
    visitors: list[RegistryReadVisitor] = []
    raw_consumer_uses: list[str] = []
    for directory in PY_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            visitor = RegistryReadVisitor(relative)
            visitor.visit(tree)
            visitors.append(visitor)
            if relative == RAW_CONSUMER[0]:
                parents = {
                    id(child): parent
                    for parent in ast.walk(tree)
                    for child in ast.iter_child_nodes(parent)
                }
                function = next(
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == RAW_CONSUMER[1]
                )
                for node in ast.walk(function):
                    if not (
                        isinstance(node, ast.Name)
                        and isinstance(node.ctx, ast.Load)
                        and node.id == "native_project_id"
                    ):
                        continue
                    parent = parents[id(node)]
                    allowed_type_check = (
                        isinstance(parent, ast.Call)
                        and isinstance(parent.func, ast.Name)
                        and parent.func.id == "isinstance"
                        and parent.args[0] is node
                    )
                    allowed_empty_check = (
                        isinstance(parent, ast.UnaryOp)
                        and isinstance(parent.op, ast.Not)
                    )
                    allowed_forward = isinstance(parent, ast.Dict) and any(
                        value is node
                        and isinstance(key, ast.Constant)
                        and key.value == "native_project_id"
                        for key, value in zip(parent.keys, parent.values)
                    )
                    if not (allowed_type_check or allowed_empty_check or allowed_forward):
                        raw_consumer_uses.append(
                            f"{relative}:{node.lineno}: unexpected use of raw bb.project_id"
                        )

    reads = [read for visitor in visitors for read in visitor.reads]
    counts = Counter(site for site, _lineno in reads)
    hits = [hit for visitor in visitors for hit in visitor.hits] + raw_consumer_uses
    for site, lineno in reads:
        if site not in EXPECTED_READS:
            hits.append(f"{site[0]}:{lineno}: direct bb.project_id validation outside the seam")
    for site, expected in EXPECTED_READS.items():
        if counts[site] != expected:
            hits.append(
                f"{site[0]}:{site[1]}: expected {expected} direct bb.project_id read; "
                f"found {counts[site]}"
            )
    return sorted(hits)


class BbProjectIdGuardTest(unittest.TestCase):
    def test_one_bb_project_id_validator(self) -> None:
        hits = independent_bb_project_id_validations()
        self.assertEqual(
            [],
            hits,
            "independent bb.project_id validation(s) outside "
            "bb_project_id_from_project():\n" + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
