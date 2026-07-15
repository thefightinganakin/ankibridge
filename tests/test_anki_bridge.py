"""Tests for anki_bridge helper functions.

These tests exercise pure-Python helpers that do not require a live Anki
process.  The aqt / anki packages are mocked out before the module is loaded.
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


def _load_anki_bridge_module():
    root = Path(__file__).resolve().parents[1]
    pkg_dir = root / "src" / "ankibridge"

    # Stub out aqt and anki so the import at the top of anki_bridge.py works
    # without a real Anki installation.
    if "aqt" not in sys.modules:
        aqt = types.ModuleType("aqt")
        aqt.mw = mock.MagicMock()
        sys.modules["aqt"] = aqt

    if "ankibridge" not in sys.modules:
        pkg = types.ModuleType("ankibridge")
        pkg.__path__ = [str(pkg_dir)]
        sys.modules["ankibridge"] = pkg

    # const is imported by anki_bridge
    const_spec = importlib.util.spec_from_file_location(
        "ankibridge.const", pkg_dir / "const.py"
    )
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules["ankibridge.const"] = const_module
    const_spec.loader.exec_module(const_module)

    spec = importlib.util.spec_from_file_location(
        "ankibridge.anki_bridge", pkg_dir / "anki_bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ankibridge.anki_bridge"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bridge = _load_anki_bridge_module()


# ---------------------------------------------------------------------------
# _is_cloze_note
# ---------------------------------------------------------------------------

class IsClozeNoteTests(unittest.TestCase):
    def _make_note(self, type_val, use_note_type=True):
        note = mock.MagicMock()
        if use_note_type:
            note.note_type.return_value = {"type": type_val}
        else:
            note.note_type.side_effect = AttributeError
            note.model.return_value = {"type": type_val}
        return note

    def test_basic_note_is_not_cloze(self):
        note = self._make_note(0)
        self.assertFalse(bridge._is_cloze_note(note))

    def test_cloze_note_is_detected(self):
        note = self._make_note(1)
        self.assertTrue(bridge._is_cloze_note(note))

    def test_fallback_to_model_api(self):
        note = self._make_note(1, use_note_type=False)
        self.assertTrue(bridge._is_cloze_note(note))

    def test_exception_returns_false(self):
        note = mock.MagicMock()
        note.note_type.side_effect = Exception("boom")
        note.model.side_effect = Exception("boom")
        self.assertFalse(bridge._is_cloze_note(note))


# ---------------------------------------------------------------------------
# _card_payload
# ---------------------------------------------------------------------------

def _make_card(card_id=1001, note_id=2001, did=1, card_type=2,
               front="Front text", back="Back text", cloze=False,
               question_av_tags=None, answer_av_tags=None):
    """Build a minimal mock card for _card_payload."""
    note = mock.MagicMock()
    note.id = note_id
    note.note_type.return_value = {"type": 1 if cloze else 0}
    note.values.return_value = [front, back]

    render_out = mock.MagicMock()
    render_out.question_text = front
    render_out.answer_text = (
        f'<hr id=answer>{back}'
    )
    render_out.question_av_tags = question_av_tags or []
    render_out.answer_av_tags = answer_av_tags or []

    card = mock.MagicMock()
    card.id = card_id
    card.did = did
    card.type = card_type
    card.note.return_value = note
    card.render_output.return_value = render_out

    col = mock.MagicMock()
    col.get_card.return_value = card
    col.decks.name.return_value = "TestDeck"

    return col, card, note


class CardPayloadTests(unittest.TestCase):
    def test_basic_card_type_field(self):
        col, card, note = _make_card(cloze=False)
        payload = bridge._card_payload(col, card.id)
        self.assertEqual(payload["cardType"], "basic")

    def test_cloze_card_type_field(self):
        col, card, note = _make_card(cloze=True)
        payload = bridge._card_payload(col, card.id)
        self.assertEqual(payload["cardType"], "cloze")

    def test_no_html_fields_by_default(self):
        col, card, note = _make_card()
        payload = bridge._card_payload(col, card.id)
        self.assertNotIn("frontHtml", payload)
        self.assertNotIn("backHtml", payload)

    def test_html_fields_present_when_include_media(self):
        col, card, note = _make_card(front="Hello", back="World")
        payload = bridge._card_payload(col, card.id, include_media=True)
        self.assertIn("frontHtml", payload)
        self.assertIn("backHtml", payload)
        self.assertEqual(payload["frontHtml"], "Hello")

    def test_unsupported_media_flagged(self):
        col, card, note = _make_card(front='<img src="x.jpg"> word', back="Answer")
        payload = bridge._card_payload(col, card.id)
        self.assertTrue(payload.get("unsupportedMedia"))

    def test_unsupported_media_not_flagged_for_plain_text(self):
        col, card, note = _make_card(front="Simple front", back="Simple back")
        payload = bridge._card_payload(col, card.id)
        self.assertNotIn("unsupportedMedia", payload)

    def test_audio_av_tags_are_reinserted_as_sound_markup(self):
        # Anki's render_output() strips [sound:...] refs out of question_text/
        # answer_text and returns them separately as av_tags; _card_payload
        # must put them back so clients scanning the HTML find the audio.
        sound_tag = mock.MagicMock(filename="clip.mp3")
        col, card, note = _make_card(
            front="Listen", back="Answer",
            question_av_tags=[sound_tag], answer_av_tags=[sound_tag],
        )
        payload = bridge._card_payload(col, card.id, include_media=True)
        self.assertIn("[sound:clip.mp3]", payload["frontHtml"])
        self.assertIn("[sound:clip.mp3]", payload["backHtml"])

    def test_tts_av_tags_without_filename_are_skipped(self):
        tts_tag = mock.MagicMock(spec=["field_text"])  # no `filename` attr
        col, card, note = _make_card(front="Say hi", question_av_tags=[tts_tag])
        payload = bridge._card_payload(col, card.id, include_media=True)
        self.assertNotIn("[sound:", payload["frontHtml"])

    def test_audio_only_card_flagged_unsupported_without_media(self):
        sound_tag = mock.MagicMock(filename="clip.mp3")
        col, card, note = _make_card(front="Listen", question_av_tags=[sound_tag])
        payload = bridge._card_payload(col, card.id)  # include_media defaults False
        self.assertTrue(payload.get("unsupportedMedia"))

    def test_due_kind_mapping(self):
        col, card, note = _make_card(card_type=0)  # type 0 = new
        payload = bridge._card_payload(col, card.id)
        self.assertEqual(payload["dueKind"], "new")

    def test_required_fields_present(self):
        col, card, note = _make_card()
        payload = bridge._card_payload(col, card.id)
        for field in ("cardId", "noteId", "deckName", "cardType", "front", "back", "dueKind"):
            self.assertIn(field, payload, f"Missing field: {field}")


# ---------------------------------------------------------------------------
# review_batch — must not return more cards than the scheduler (and thus the
# home screen's deck counts) actually allows for today.
# ---------------------------------------------------------------------------

def _make_review_batch_col(card_types, caps):
    """card_types: Anki card.type per matched card (0=new,1=learning,2=review,
    3=relearning). caps: (new_cap, learn_cap, review_cap) from deck_due_tree,
    same source `list_decks` reports to the home screen."""
    col = mock.MagicMock()
    col.decks.id_for_name.return_value = 42
    col.find_cards.return_value = list(range(len(card_types)))

    cards_by_id = {}
    for cid, ctype in enumerate(card_types):
        note = mock.MagicMock()
        note.id = 9000 + cid
        note.note_type.return_value = {"type": 0}
        card = mock.MagicMock()
        card.id = cid
        card.did = 42
        card.type = ctype
        card.note.return_value = note
        card.render_output.return_value = mock.MagicMock(
            question_text="F", answer_text="<hr id=answer>B",
            question_av_tags=[], answer_av_tags=[],
        )
        cards_by_id[cid] = card

    col.get_card.side_effect = lambda cid: cards_by_id[cid]
    col.decks.name.return_value = "TestDeck"
    col.sched.deck_due_tree.return_value = mock.MagicMock(
        deck_id=42, new_count=caps[0], learn_count=caps[1], review_count=caps[2], children=[],
    )
    return col


class ReviewBatchCapsTests(unittest.TestCase):
    def setUp(self):
        # run_on_main_sync normally hops to Anki's main thread via
        # mw.taskman.run_on_main; run the work synchronously in-thread instead.
        bridge.mw.taskman.run_on_main.side_effect = lambda fn: fn()
        # The due-tree cache is a module-level global — clear it so one
        # test's counts can't leak into the next.
        bridge._invalidate_due_tree_cache()

    def tearDown(self):
        bridge.mw.taskman.run_on_main.side_effect = None
        bridge._invalidate_due_tree_cache()

    def test_uncapped_by_default(self):
        # respect_daily_limits defaults to False: the scheduler's caps exist
        # but must not be applied unless a caller explicitly asks for them.
        col = _make_review_batch_col(card_types=[0] * 10, caps=(3, 0, 0))
        with mock.patch.object(bridge, "_col", return_value=col), \
             mock.patch.object(bridge, "_deck_exists", return_value=True):
            result = bridge.review_batch("TestDeck", limit=100)
        self.assertEqual(len(result), 10)
        col.sched.deck_due_tree.assert_not_called()

    def test_caps_new_cards_to_scheduler_count(self):
        # Search matches 10 new cards, but the scheduler only allows 3 today.
        col = _make_review_batch_col(card_types=[0] * 10, caps=(3, 0, 0))
        with mock.patch.object(bridge, "_col", return_value=col), \
             mock.patch.object(bridge, "_deck_exists", return_value=True):
            result = bridge.review_batch("TestDeck", limit=100, respect_daily_limits=True)
        self.assertEqual(len(result), 3)

    def test_does_not_exceed_home_screen_total(self):
        # Search finds 100 cards, but the home screen shows 7 new + 33
        # learning + 4 review = 44 — the session must match, not show 100.
        # (Each bucket has more matches than its cap, so the cap binds.)
        col = _make_review_batch_col(
            card_types=[0] * 50 + [1] * 40 + [2] * 10, caps=(7, 33, 4),
        )
        with mock.patch.object(bridge, "_col", return_value=col), \
             mock.patch.object(bridge, "_deck_exists", return_value=True):
            result = bridge.review_batch("TestDeck", limit=100, respect_daily_limits=True)
        self.assertEqual(len(result), 44)

    def test_uncapped_when_scheduler_unavailable(self):
        # deck_due_tree() can fail on older Anki APIs — fall back to the raw
        # search result rather than returning zero cards, even when capping
        # was requested.
        col = _make_review_batch_col(card_types=[0] * 5, caps=(0, 0, 0))
        col.sched.deck_due_tree.side_effect = Exception("no v3 scheduler")
        with mock.patch.object(bridge, "_col", return_value=col), \
             mock.patch.object(bridge, "_deck_exists", return_value=True):
            result = bridge.review_batch("TestDeck", limit=100, respect_daily_limits=True)
        self.assertEqual(len(result), 5)

    def test_reuses_cached_due_tree_across_decks_in_one_sync(self):
        # A sync fans out one review_batch call per deck; the due tree should
        # only be walked once, not once per deck.
        col = _make_review_batch_col(card_types=[0] * 10, caps=(3, 0, 0))
        with mock.patch.object(bridge, "_col", return_value=col), \
             mock.patch.object(bridge, "_deck_exists", return_value=True):
            bridge.review_batch("TestDeck", limit=100, respect_daily_limits=True)
            bridge.review_batch("TestDeck", limit=100, respect_daily_limits=True)
        self.assertEqual(col.sched.deck_due_tree.call_count, 1)

    def test_list_decks_always_refreshes_the_cache(self):
        # list_decks is the home screen's source of truth — it must never
        # serve a stale cached tree, even if one is already cached.
        col = mock.MagicMock()
        col.decks.all_names_and_ids.return_value = []
        col.sched.deck_due_tree.return_value = mock.MagicMock(deck_id=0, children=[])
        with mock.patch.object(bridge, "_col", return_value=col):
            bridge.list_decks()
            bridge.list_decks()
        self.assertEqual(col.sched.deck_due_tree.call_count, 2)

    def test_answer_card_invalidates_the_cache(self):
        col = _make_review_batch_col(card_types=[0] * 10, caps=(3, 0, 0))
        col.get_card.side_effect = None
        col.get_card.return_value = mock.MagicMock(id=1, queue=0)
        with mock.patch.object(bridge, "_col", return_value=col), \
             mock.patch.object(bridge, "_deck_exists", return_value=True):
            bridge.review_batch("TestDeck", limit=100, respect_daily_limits=True)  # primes the cache
            with mock.patch.object(bridge, "_apply_answer"):
                bridge.answer_card(1, "good")
            bridge.review_batch("TestDeck", limit=100, respect_daily_limits=True)  # must recompute
        self.assertEqual(col.sched.deck_due_tree.call_count, 2)


# ---------------------------------------------------------------------------
# _has_unsupported_media / _to_plain_text
# ---------------------------------------------------------------------------

class MediaDetectionTests(unittest.TestCase):
    def test_plain_text_is_supported(self):
        self.assertFalse(bridge._has_unsupported_media("Hello world"))

    def test_image_tag_is_unsupported(self):
        self.assertTrue(bridge._has_unsupported_media('<img src="pic.jpg">'))

    def test_sound_tag_is_unsupported(self):
        self.assertTrue(bridge._has_unsupported_media("[sound:audio.mp3]"))

    def test_mathjax_is_unsupported(self):
        self.assertTrue(bridge._has_unsupported_media(r"\(x^2\)"))

    def test_audio_element_is_unsupported(self):
        self.assertTrue(bridge._has_unsupported_media('<audio src="x.mp3">'))


class PlainTextTests(unittest.TestCase):
    def test_strips_html_tags(self):
        self.assertEqual(bridge._to_plain_text("<b>Hello</b>"), "Hello")

    def test_converts_break_to_space(self):
        result = bridge._to_plain_text("A<br/>B")
        self.assertIn("A", result)
        self.assertIn("B", result)

    def test_removes_sound_tags(self):
        result = bridge._to_plain_text("[sound:x.mp3] word")
        self.assertNotIn("[sound:", result)
        self.assertIn("word", result)

    def test_empty_input(self):
        self.assertEqual(bridge._to_plain_text(""), "")

    def test_html_entities_unescaped(self):
        self.assertEqual(bridge._to_plain_text("&amp;"), "&")


if __name__ == "__main__":
    unittest.main()
