"""GH-731 guard: one author for BB project-id validation.

The production Python trees are parsed with ``ast``. Any literal read of
``bb.project_id`` or ``bb.project_ids`` fails unless it is the shared validator.

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

# The sole validation authority. It reads the mapping and legacy fallback once.
AUTHORITY = ("llm_collab/bb_client.py", "bb_project_id_from_project")

EXPECTED_READS = {AUTHORITY: 2}


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
    if not any(
        _get_key(node, key) or _subscript_key(node, key)
        for key in ("project_id", "project_ids")
    ):
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
        self.reads: list[tuple[tuple[str, str], int]] = []
        self.recorded_read_ids: set[int] = set()

    def _record_read(self, node: ast.AST) -> None:
        self.reads.append(((self.relative_path, self.scopes[-1]), node.lineno))
        self.recorded_read_ids.add(id(node))

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.aliases.append({"bb"})
        for statement in node.body:
            self.visit(statement)
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
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _bb_project_id_read(node, self.aliases[-1]) and id(node) not in self.recorded_read_ids:
            self._record_read(node)
        self.generic_visit(node)


def _unexpected_read_hits(
    reads: list[tuple[tuple[str, str], int]],
) -> list[str]:
    return [
        f"{site[0]}:{lineno}: direct BB project-id validation outside the seam"
        for site, lineno in reads
        if site not in EXPECTED_READS
    ]


def independent_bb_project_id_validations() -> list[str]:
    visitors: list[RegistryReadVisitor] = []
    for directory in PY_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            visitor = RegistryReadVisitor(relative)
            visitor.visit(tree)
            visitors.append(visitor)

    reads = [read for visitor in visitors for read in visitor.reads]
    counts = Counter(site for site, _lineno in reads)
    hits = _unexpected_read_hits(reads)
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
            "independent BB project-id validation(s) outside "
            "bb_project_id_from_project():\n" + "\n".join(hits),
        )

    def test_reintroduced_bootstrap_raw_read_is_rejected(self) -> None:
        tree = ast.parse(
            "def _bb_start_inputs(project):\n"
            "    bb = project.get('bb')\n"
            "    return bb.get('project_id')\n"
        )
        visitor = RegistryReadVisitor("bin/watch_inbox.py")
        visitor.visit(tree)
        self.assertEqual(
            ["bin/watch_inbox.py:3: direct BB project-id validation outside the seam"],
            _unexpected_read_hits(visitor.reads),
        )


if __name__ == "__main__":
    unittest.main()
