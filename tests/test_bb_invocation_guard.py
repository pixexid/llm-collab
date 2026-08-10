"""GH-728 guard: no bare BB invocation outside the resolver seam.

Every call site that invokes BB obtains its command from
``llm_collab.bb_client.bb_executable_from_project()``; nothing else constructs
a BB invocation. This test scans the production source tree and fails on any
bare BB invocation, so the seventh instance of the bare-``bb`` class arrives
as a red test in the authoring worker's own suite, seconds after it is
written, instead of as a review finding hours later.

Detection approach — deliberately independent of where a line break falls
(PR #735 review, P2: a line-by-line scan never presents both tokens of the
ordinary multiline ``subprocess.run([\\n    "bb", ...])`` form to any
pattern, so the guard stayed green with a bare invocation present):

- Python files are parsed with ``ast`` and walked. Two node shapes flag: a
  list/tuple literal whose first element is the constant string ``bb`` (an
  argv being constructed, whether assigned or passed inline, on one line or
  many), and a call whose first positional argument is the constant string
  ``bb`` (``spawn("bb", ...)``, ``which("bb")`` — the PATH-fallback probe).
  AST matching needs no subscript lookbehind (``entry["bb"]`` is a Subscript,
  never a List) and cannot be fooled by comments, docstrings, or formatting.
  An unparseable production file fails the guard rather than being skipped.
- TS/JS files have no parser here, so they are scanned as WHOLE-FILE text
  with regexes whose ``\\s`` spans newlines; a multiline array literal is as
  ordinary there as in Python. The subscript lookbehind is retained for them.
- Extensionless executable scripts in ``bin/`` (``axsend-ensure``,
  ``llm-collab``) are detected by SHEBANG, not the mode bit: ``#!`` is direct
  evidence the file is interpreted as commands, while +x only says it may be
  executed. They take the same whole-file regex path as TS/JS, plus a shell
  command-word pattern (``bb`` at a line start or after a shell separator),
  since a shell invocation is a bare word, not a call.

What this guard does NOT catch, deliberately: a bb literal reaching a spawn
through a variable or other indirection (``cmd = "bb"; subprocess.run([cmd,
...])``), where the first element is a Name rather than a constant. The class
this guard exists to prevent is the accidentally written literal — six
instances in one day, every one a literal — and nobody assigns ``"bb"`` to a
variable by accident, so dataflow analysis would be disproportionate to a
threat that has never occurred. The class is hard to reintroduce
*accidentally*; it is not closed against a determined author, and no reader
should take this guard as proof that it is.

Scope and exemptions — deliberately narrow:

- Scanned: ``bin/``, ``llm_collab/``, ``scripts/`` (Python), ``bb-plugins/``
  and ``pi-extensions/`` (TS/JS), and extensionless shebang scripts in
  ``bin/`` (shell). Those are the trees whose code can spawn a
  process; a BB invocation outside them cannot execute.
- Not scanned: ``tests/`` and ``docs/``. Test fixtures carry ``["bb"]`` as
  registry *data* — the configured value the seam validates — never as an
  invocation; docs describe BB's native CLI surface in prose and are governed
  by review, not by this guard. No production file is exempt: after GH-728 the
  resolver itself holds no bb argv literal, so a clean tree needs zero
  exemptions and any hit is a real violation.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PY_DIRS = ("bin", "llm_collab", "scripts")
TS_DIRS = ("bb-plugins", "pi-extensions")

TS_PATTERNS = (
    # argv array literal starting with the bb binary, across line breaks.
    # The lookbehind excludes subscripts (entry["bb"]), which are config
    # reads, not invocations.
    re.compile(r"(?<![\w\)\]\"'`])\[\s*[\"']bb[\"'\s,\]]"),
    # bb literal as the command argument of a process-spawning call.
    re.compile(
        r"(?:Popen|run|exec|spawn|check_output|check_call|call)\(\s*[\"']bb[\"'\s,\)]"
    ),
    # A PATH lookup for bb: how a silent PATH fallback gets constructed.
    re.compile(r"which\(\s*[\"']bb[\"']"),
)

# Shell files invoke a command as a bare word, so they take the shared text
# patterns plus their own.
SHELL_PATTERNS = TS_PATTERNS + (
    # bb as a command word: at a line start or after a shell separator,
    # including $(...) substitution. A mention inside a comment or string
    # is not preceded by one of these and does not match.
    re.compile(r"(?m)(?:^|[|;&`(])\s*bb(?:[ \t]|\n|$)"),
    # bb as the first element of a shell array: ("bb" ...)
    re.compile(r"\(\s*[\"']bb[\"'\s,\)]"),
)


# Function names whose first argument is a command: process spawns and PATH
# lookups. ``get``/subscript-style config reads (``project.get("bb")``) are
# deliberately NOT in this set — they read the registry, they do not invoke.
SPAWN_NAMES = frozenset({
    "Popen", "run", "exec", "execv", "execve", "execl", "execlp", "execvp",
    "spawn", "spawnl", "spawnlp", "system", "popen", "check_output",
    "check_call", "call", "which",
})


def _is_bb_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "bb"


def _is_bb_command_text(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == "bb" or node.value.startswith(("bb ", "bb\t")))
    )


def _python_hits(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts and _is_bb_constant(node.elts[0]):
            hits.append(f"{node.lineno}: argv literal starting with the bb binary")
        elif isinstance(node, ast.Call) and node.args and _is_bb_command_text(node.args[0]):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name in SPAWN_NAMES:
                hits.append(f"{node.lineno}: bb literal as the command argument of a call")
    return hits


def _text_hits(path: Path, patterns) -> list[str]:
    text = path.read_text()
    hits: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            hits.append(f"{lineno}: {match.group(0)!r}")
    return hits


def _shell_scripts() -> list[Path]:
    """Extensionless scripts in bin/ that a kernel can execute directly.

    Detection is by shebang rather than the executable bit: a ``#!`` line is
    direct evidence the file is interpreted as commands, while the mode bit
    only says it may be executed.
    """
    scripts: list[Path] = []
    for path in sorted((ROOT / "bin").iterdir()):
        if not path.is_file() or path.suffix:
            continue
        with path.open("rb") as handle:
            if handle.readline(256).startswith(b"#!"):
                scripts.append(path)
    return scripts


def bare_bb_invocations() -> list[str]:
    hits: list[str] = []
    for directory in PY_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            for hit in _python_hits(path):
                hits.append(f"{path.relative_to(ROOT)}:{hit}")
    for directory in TS_DIRS:
        for suffix in ("*.ts", "*.js"):
            for path in sorted((ROOT / directory).rglob(suffix)):
                for hit in _text_hits(path, TS_PATTERNS):
                    hits.append(f"{path.relative_to(ROOT)}:{hit}")
    for path in _shell_scripts():
        for hit in _text_hits(path, SHELL_PATTERNS):
            hits.append(f"{path.relative_to(ROOT)}:{hit}")
    return hits


class BbInvocationGuardTest(unittest.TestCase):
    def test_no_bare_bb_invocation_outside_the_resolver_seam(self) -> None:
        hits = bare_bb_invocations()
        self.assertEqual(
            [],
            hits,
            "bare BB invocation(s) outside bb_executable_from_project() "
            "(GH-728); resolve the command through the seam instead:\n"
            + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
