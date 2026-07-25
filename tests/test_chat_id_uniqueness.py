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

    def test_every_real_chat_directory_yields_an_id(self) -> None:
        """Guards the naming assumption this rule now depends on."""
        real = [p for p in (ROOT / "Chats").iterdir()
                if p.is_dir() and not p.name.startswith(".")]
        self.assertTrue(real, "the live workspace should have chats to check")
        missing = [p.name for p in real if _helpers.chat_id_of(p) is None]
        self.assertEqual([], missing, "every chat directory must expose an id after '__'")

    def test_lowercase_legacy_ids_are_covered_by_the_same_rule(self) -> None:
        with self._chats("2026-04-01_a__CHAT-e71d34ff", "2026-04-02_b__CHAT-e71d34ff"):
            with self.assertRaises(ValueError):
                _helpers.find_chat_by_partial("CHAT-e71d34ff")


if __name__ == "__main__":
    unittest.main()
