"""A chat id that matches two directories must refuse to resolve.

Two directories carried CHAT-8976EECB and one had no meta.json. find_chat_by_partial
returned matches[-1], so an exact-id send resolved to whichever name sorted last --
the malformed one -- and deliver.py refused with an error naming a directory the
sender never chose. The same silent pick would have routed mail into the wrong chat
had both been well formed, which is the wrong-receiver failure the workspace exists
to prevent. Loose partials keep newest-wins; exact ids must be unique or fail.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import _helpers  # noqa: E402


class ChatIdUniquenessTest(unittest.TestCase):
    def _chats(self, *names: str):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in names:
            (root / name).mkdir(parents=True)
        return mock.patch.object(_helpers, "CHATS_DIR", root)

    def test_duplicate_exact_chat_id_refuses_instead_of_guessing(self) -> None:
        with self._chats("2026-07-20_a__CHAT-8976EECB", "2026-07-21_b__CHAT-8976EECB"):
            with self.assertRaises(ValueError) as caught:
                _helpers.find_chat_by_partial("CHAT-8976EECB")
        message = str(caught.exception)
        self.assertIn("2026-07-20_a__CHAT-8976EECB", message)
        self.assertIn("2026-07-21_b__CHAT-8976EECB", message,
                      "both candidates must be named so the operator can merge them")

    def test_unique_exact_chat_id_still_resolves(self) -> None:
        with self._chats("2026-07-20_a__CHAT-8976EECB", "2026-07-21_b__CHAT-0FA738E3"):
            found = _helpers.find_chat_by_partial("CHAT-8976EECB")
        self.assertEqual("2026-07-20_a__CHAT-8976EECB", found.name)

    def _chats_with_meta(self, spec: dict):
        """spec: directory name -> project_id, or None to omit meta.json entirely."""
        import json as _json
        import tempfile as _tempfile
        tmp = _tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, project in spec.items():
            (root / name).mkdir(parents=True)
            if project is not None:
                (root / name / "meta.json").write_text(
                    _json.dumps({"chat_id": name.rpartition("__")[2], "project_id": project}),
                    encoding="utf-8")
        return mock.patch.object(_helpers, "CHATS_DIR", root)

    def test_same_id_in_two_projects_does_not_block_the_named_project(self) -> None:
        """Chat ids are NOT globally unique here, so a collision only matters in-project.

        Counting across projects turned a silent wrong-delivery bug into a refusal of
        legitimate traffic: `--project amiga --chat CHAT-X` was blocked by an unrelated
        CHAT-X owned by another project.
        """
        with self._chats_with_meta({"2026-07-20_a__CHAT-X": "amiga",
                                    "2026-07-21_b__CHAT-X": "nuvyr"}):
            found = _helpers.find_chat_by_partial("CHAT-X", project="amiga")
            self.assertEqual("2026-07-20_a__CHAT-X", found.name)
            other = _helpers.find_chat_by_partial("CHAT-X", project="nuvyr")
            self.assertEqual("2026-07-21_b__CHAT-X", other.name)

    def test_a_duplicate_within_one_project_still_refuses(self) -> None:
        with self._chats_with_meta({"2026-07-20_a__CHAT-X": "amiga",
                                    "2026-07-21_b__CHAT-X": "amiga"}):
            with self.assertRaises(ValueError) as caught:
                _helpers.find_chat_by_partial("CHAT-X", project="amiga")
        self.assertIn("within project amiga", str(caught.exception))

    def test_a_chat_missing_its_project_id_is_not_claimed_by_a_project(self) -> None:
        # cannot be shown to belong here, so it must not satisfy a scoped request
        with self._chats_with_meta({"2026-07-20_a__CHAT-X": None}):
            self.assertIsNone(_helpers.find_chat_by_partial("CHAT-X", project="amiga"))

    def test_an_id_present_only_in_another_project_resolves_to_nothing(self) -> None:
        """Never fall through to a loose match: that delivers where the caller did not ask."""
        with self._chats_with_meta({"2026-07-20_other__CHAT-X": "nuvyr",
                                    "2026-07-19_unrelated__CHAT-Y": "amiga"}):
            self.assertIsNone(_helpers.find_chat_by_partial("CHAT-X", project="amiga"))

    def test_scoped_last_cannot_pick_another_projects_chat(self) -> None:
        """`last` is a selection too, so it must select within scope.

        Filtering after exact-id selection left this path unscoped: the newest chat overall
        won even when it belonged to a different project, and the mistake only surfaced later
        as a delivery failure.
        """
        with self._chats_with_meta({"2026-07-20_mine__CHAT-A": "amiga",
                                    "2026-07-25_theirs__CHAT-B": "nuvyr"}):
            found = _helpers.find_chat_by_partial("last", project="amiga")
        self.assertEqual("2026-07-20_mine__CHAT-A", found.name,
                         "the newest chat IN THIS PROJECT, not the newest overall")

    def test_a_scoped_loose_partial_cannot_leave_the_project(self) -> None:
        with self._chats_with_meta({"2026-07-20_gh-90-mine__CHAT-A": "amiga",
                                    "2026-07-25_gh-90-theirs__CHAT-B": "nuvyr"}):
            found = _helpers.find_chat_by_partial("gh-90", project="amiga")
        self.assertEqual("2026-07-20_gh-90-mine__CHAT-A", found.name)

    def test_a_scoped_lookup_with_no_chat_in_that_project_resolves_nothing(self) -> None:
        with self._chats_with_meta({"2026-07-25_theirs__CHAT-B": "nuvyr"}):
            self.assertIsNone(_helpers.find_chat_by_partial("last", project="amiga"))
            self.assertIsNone(_helpers.find_chat_by_partial("gh-90", project="amiga"))

    def test_malformed_metadata_elsewhere_cannot_block_a_valid_chat(self) -> None:
        """An unrelated same-id directory with broken metadata must not raise.

        It also must not match: metadata that cannot be read cannot establish a project.
        """
        import json as _json
        import tempfile as _tempfile
        tmp = _tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        good = root / "2026-07-20_good__CHAT-X"
        good.mkdir()
        (good / "meta.json").write_text(
            _json.dumps({"chat_id": "CHAT-X", "project_id": "amiga"}), encoding="utf-8")
        broken = root / "2026-07-21_broken__CHAT-X"
        broken.mkdir()
        (broken / "meta.json").write_text("{not json", encoding="utf-8")

        with mock.patch.object(_helpers, "CHATS_DIR", root):
            found = _helpers.find_chat_by_partial("CHAT-X", project="amiga")
        self.assertEqual("2026-07-20_good__CHAT-X", found.name,
                         "a broken sibling must neither raise nor win")

    def test_metadata_that_is_not_an_object_is_a_non_match(self) -> None:
        import tempfile as _tempfile
        tmp = _tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        odd = root / "2026-07-20_odd__CHAT-X"
        odd.mkdir()
        (odd / "meta.json").write_text('["amiga"]', encoding="utf-8")
        with mock.patch.object(_helpers, "CHATS_DIR", root):
            self.assertIsNone(_helpers.find_chat_by_partial("CHAT-X", project="amiga"))

    def test_an_out_of_scope_bearer_beats_an_in_scope_title_mention(self) -> None:
        """The exact case the pre-filter broke.

        CHAT-X is borne only in nuvyr, while an amiga directory merely MENTIONS it in its
        title. Pre-filtering to amiga left that mention as the only candidate and the loose
        fallback returned CHAT-Y -- handing the caller a different chat because the one they
        named lives elsewhere. Who bears an id is a global fact and must survive scoping.
        """
        with self._chats_with_meta({"2026-07-20_followup-CHAT-X__CHAT-Y": "amiga",
                                    "2026-07-21_real__CHAT-X": "nuvyr"}):
            self.assertIsNone(_helpers.find_chat_by_partial("CHAT-X", project="amiga"))
            # and the bearer is still reachable in its own project
            found = _helpers.find_chat_by_partial("CHAT-X", project="nuvyr")
        self.assertEqual("2026-07-21_real__CHAT-X", found.name)

    def test_an_in_scope_bearer_wins_over_an_in_scope_mention(self) -> None:
        with self._chats_with_meta({"2026-07-20_followup-CHAT-X__CHAT-Y": "amiga",
                                    "2026-07-21_real__CHAT-X": "amiga"}):
            found = _helpers.find_chat_by_partial("CHAT-X", project="amiga")
        self.assertEqual("2026-07-21_real__CHAT-X", found.name)

    def test_a_loose_partial_with_no_bearer_anywhere_still_falls_back(self) -> None:
        # the fallback is only forbidden when the named id exists somewhere
        with self._chats_with_meta({"2026-07-20_gh-90-one__CHAT-A": "amiga",
                                    "2026-07-21_gh-90-two__CHAT-B": "amiga"}):
            found = _helpers.find_chat_by_partial("gh-90", project="amiga")
        self.assertEqual("2026-07-21_gh-90-two__CHAT-B", found.name)

    def test_a_programming_defect_in_metadata_reading_is_not_swallowed(self) -> None:
        """A bare except made every scoped chat vanish instead of raising.

        Silent AND confusing: the caller sees "no chat in this project" for a bug in our own
        code.
        """
        with self._chats_with_meta({"2026-07-20_a__CHAT-X": "amiga"}):
            with mock.patch.object(_helpers, "load_chat_meta",
                                   side_effect=TypeError("bug in our own code")):
                with self.assertRaises(TypeError):
                    _helpers.find_chat_by_partial("CHAT-X", project="amiga")

    def test_unscoped_callers_keep_the_global_uniqueness_rule(self) -> None:
        with self._chats_with_meta({"2026-07-20_a__CHAT-X": "amiga",
                                    "2026-07-21_b__CHAT-X": "nuvyr"}):
            with self.assertRaises(ValueError):
                _helpers.find_chat_by_partial("CHAT-X")

    def test_loose_partial_matching_many_keeps_newest_wins(self) -> None:
        # human lookup, not delivery addressing -- must not become an error
        with self._chats("2026-07-20_gh-90_x__CHAT-AAAA1111", "2026-07-21_gh-90_y__CHAT-BBBB2222"):
            found = _helpers.find_chat_by_partial("gh-90")
        self.assertEqual("2026-07-21_gh-90_y__CHAT-BBBB2222", found.name)

    def test_lowercase_selector_for_an_uppercase_duplicate_still_refuses(self) -> None:
        """The decision must come from the directory's id, not the selector's shape.

        Testing the selector against an uppercase pattern let `chat-8976eecb` bypass the
        refusal completely and deliver into whichever duplicate sorted last.
        """
        with self._chats("2026-07-20_a__CHAT-8976EECB", "2026-07-21_b__CHAT-8976EECB"):
            with self.assertRaises(ValueError):
                _helpers.find_chat_by_partial("chat-8976eecb")

    def test_id_shaped_prefix_is_an_ordinary_loose_match_not_a_collision(self) -> None:
        # `CHAT-89` looks like an id but is a prefix; refusing it would break human lookup
        with self._chats("2026-07-20_a__CHAT-8976EECB", "2026-07-21_b__CHAT-8912ABCD"):
            found = _helpers.find_chat_by_partial("CHAT-89")
        self.assertEqual("2026-07-21_b__CHAT-8912ABCD", found.name)

    def test_a_title_mentioning_another_id_does_not_count_as_a_collision(self) -> None:
        # only the TRAILING token is the directory's id
        with self._chats("2026-07-20_re-CHAT-8976EECB-followup__CHAT-0000AAAA",
                         "2026-07-21_b__CHAT-8976EECB"):
            found = _helpers.find_chat_by_partial("CHAT-8976EECB")
        self.assertEqual("2026-07-21_b__CHAT-8976EECB", found.name,
                         "the real bearer of the id must win over an incidental mention")

    def test_hyphenated_ids_are_not_a_gap_in_the_guard(self) -> None:
        """The workspace produces CHAT-CLAUDE-CODEX, CHAT-BIND-SAFE, CHAT-READY-DRIFT.

        Re-deriving the id grammar as CHAT-[0-9A-Za-z]+ matched none of them, so both
        duplicates parsed as None, neither was an exact match, and newest-wins applied --
        the guard was absent for a whole class of ids while appearing to be present.
        """
        for chat_id in ("CHAT-READY-DRIFT", "CHAT-CLAUDE-CODEX", "CHAT-BIND-SAFE"):
            with self.subTest(chat_id=chat_id):
                with self._chats(f"2026-07-20_a__{chat_id}", f"2026-07-21_b__{chat_id}"):
                    with self.assertRaises(ValueError):
                        _helpers.find_chat_by_partial(chat_id)

    def test_a_unique_hyphenated_id_resolves_to_its_bearer(self) -> None:
        with self._chats("2026-07-20_a__CHAT-READY-DRIFT", "2026-07-21_b__CHAT-0000AAAA"):
            found = _helpers.find_chat_by_partial("CHAT-READY-DRIFT")
        self.assertEqual("2026-07-20_a__CHAT-READY-DRIFT", found.name)

    def test_the_naming_convention_this_rule_depends_on(self) -> None:
        """Fixture-only, deliberately.

        An earlier version walked the live ROOT/Chats tree, which is gitignored mutable
        runtime state: absent in a clean checkout, so the suite raised FileNotFoundError
        there. A unit test may not depend on another lane's working directory. The
        346-directory live survey that motivated this parser stays where it belongs, as
        review evidence in the PR body.
        """
        shapes = {
            "2026-07-20_slug__CHAT-8976EECB": "CHAT-8976EECB",
            "2026-04-23_readiness-drift__CHAT-READY-DRIFT": "CHAT-READY-DRIFT",
            "2026-03-30_workstream__gh-127-ui__CHAT-b13bc468": "CHAT-b13bc468",
            "2026-07-20_re-CHAT-0000AAAA-followup__CHAT-8976EECB": "CHAT-8976EECB",
        }
        with self._chats(*shapes):
            for name, expected in shapes.items():
                with self.subTest(name=name):
                    self.assertEqual(expected, _helpers.chat_id_of(Path(name)))

    def test_a_directory_without_the_separator_exposes_no_id(self) -> None:
        # such a directory can never be an exact match, so it can only be a loose one
        self.assertIsNone(_helpers.chat_id_of(Path("CHAT-8976EECB")))
        self.assertIsNone(_helpers.chat_id_of(Path("2026-07-20_slug-CHAT-8976EECB")))

    def test_lowercase_legacy_ids_are_covered_by_the_same_rule(self) -> None:
        with self._chats("2026-04-01_a__CHAT-e71d34ff", "2026-04-02_b__CHAT-e71d34ff"):
            with self.assertRaises(ValueError):
                _helpers.find_chat_by_partial("CHAT-e71d34ff")


if __name__ == "__main__":
    unittest.main()
