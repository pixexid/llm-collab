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
  many), and ANY call whose first argument is a bb literal — bare ``"bb"``
  or a shell-string command starting with ``"bb "`` — regardless of the
  callee's name. The call check is name-agnostic by design (PR #735 review):
  an enumerated list of spawn APIs fails open on every API nobody listed —
  ``asyncio.create_subprocess_exec`` passed under exactly such a list — so
  unknown names fail CLOSED instead: a spawn helper nobody anticipated is
  caught by default, and a genuinely benign new shape fails loud until
  someone exempts it deliberately, in a diff, with a comment. The exemptions
  are named literals below: config-key reads (``get``) and prose constructors
  whose first argument is a message, not a command.
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

# bb literal as the first argument of ANY call, across line breaks — the
# same fail-closed inversion as the Python check: the callee name is captured
# so the config-key read (get) can be exempted deliberately, and every other
# name, anticipated or not, is flagged.
TS_CALL_PATTERN = re.compile(r"(?P<callee>[\w$]+(?:\.[\w$]+)*)\(\s*[\"']bb[\"'\s,\)]")

TS_PATTERNS = (
    # argv array literal starting with the bb binary, across line breaks.
    # The lookbehind excludes subscripts (entry["bb"]), which are config
    # reads, not invocations.
    re.compile(r"(?<![\w\)\]\"'`])\[\s*[\"']bb[\"'\s,\]]"),
    TS_CALL_PATTERN,
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


# Exemptions to the name-agnostic call check — each a named literal with its
# reason. Anything taking a bb literal as its first argument under any other
# name is flagged.

# ``get`` reads a registry KEY named "bb" (``project.get("bb")``); it never
# invokes a command. (8 such reads across bin/, llm_collab/, scripts/ when
# this inversion landed.)
BENIGN_KEY_READ_NAMES = frozenset({"get"})

# Message/label constructors whose first argument is PROSE that happens to
# start with "bb " ("bb session is not active", "bb thread row ...", the
# hook's "bb version" label) — not a shell-string command. The string-command
# form (``os.system("bb thread list")``) stays caught under every other name,
# known or not. (27 such call sites when this inversion landed; every one
# verified prose.)
PROSE_FIRST_ARG_NAMES = frozenset({
    "ValueError",
    "RuntimeError",
    "ProbeError",
    "BbContinuationRefused",
    "CanonicalIntegrityError",
    "SessionLifecycleError",
    "_line",
})


def _is_bb_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "bb"


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _python_hits(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts and _is_bb_constant(node.elts[0]):
            hits.append(f"{node.lineno}: argv literal starting with the bb binary")
        elif isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                # A List/Tuple first argument is flagged at the List node
                # itself; anything else (a Name, a call) is the documented
                # indirection limit, not a literal.
                continue
            name = _call_name(node)
            if first.value == "bb":
                if name not in BENIGN_KEY_READ_NAMES:
                    hits.append(f"{node.lineno}: bb literal as the first argument of a call")
            elif first.value.startswith(("bb ", "bb\t")):
                if name not in PROSE_FIRST_ARG_NAMES and name not in BENIGN_KEY_READ_NAMES:
                    hits.append(f"{node.lineno}: bb command string as the first argument of a call")
    return hits


def _text_hits(path: Path, patterns) -> list[str]:
    text = path.read_text()
    hits: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            # The fail-closed call pattern captures its callee so the one
            # benign shape — a config-KEY read, get("bb") — can be exempted
            # deliberately; every other callee name flags.
            callee = match.groupdict().get("callee")
            if callee is not None and callee.rsplit(".", 1)[-1] == "get":
                continue
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
