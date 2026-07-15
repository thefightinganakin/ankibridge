"""All interaction with the Anki collection and scheduler.

Every public function here reads/writes the collection, which is NOT thread
safe, so each one is dispatched onto Anki's main thread via
``run_on_main_sync`` and blocks the calling (HTTP server) thread until Anki
returns a result.

Scheduler correctness is the priority: reviews are applied through Anki's own
scheduler so they are recorded exactly as if the user pressed a button in the
Reviewer. We never mutate due dates or scheduling fields by hand.
"""

import html
import re
import threading
import time

from aqt import mw

from . import const

# Anki API compatibility shims -------------------------------------------------
try:  # modern
    from anki.utils import strip_html as _anki_strip_html
except Exception:  # pragma: no cover - older Anki
    try:
        from anki.utils import stripHTML as _anki_strip_html
    except Exception:
        _anki_strip_html = None


class AnkiError(Exception):
    """Carries a machine-readable error code for the API layer."""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- main thread
def run_on_main_sync(fn, timeout=20.0):
    """Run ``fn`` on Anki's main thread and return its result synchronously."""
    result = {}
    done = threading.Event()

    def wrapper():
        try:
            result["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - propagated below
            result["error"] = exc
        finally:
            done.set()

    mw.taskman.run_on_main(wrapper)
    if not done.wait(timeout):
        raise AnkiError("timeout", "Anki did not respond in time")
    if "error" in result:
        raise result["error"]
    return result["value"]


def _col():
    col = getattr(mw, "col", None)
    if col is None:
        raise AnkiError("collection_unavailable", "No Anki collection is open")
    return col


# --------------------------------------------------------------------- health
def health_info():
    def work():
        return {
            "computerName": _computer_name(),
            "ankiProfile": _profile_name(),
        }

    return run_on_main_sync(work)


def media_dir():
    """Absolute path to the collection's media folder."""
    return run_on_main_sync(lambda: _col().media.dir())


def _profile_name():
    try:
        return mw.pm.name
    except Exception:
        return "Unknown"


def _computer_name():
    from . import netutil

    return netutil.computer_name()


# ---------------------------------------------------------------------- decks
def list_decks():
    def work():
        col = _col()
        counts = _refresh_due_tree_cache(col)

        decks = []
        for entry in col.decks.all_names_and_ids(
            skip_empty_default=False, include_filtered=False
        ):
            new_c, learn_c, review_c = counts.get(entry.id, (0, 0, 0))
            decks.append(
                {
                    "id": str(entry.id),
                    "name": entry.name,
                    "dueCount": int(review_c),
                    "newCount": int(new_c),
                    "learningCount": int(learn_c),
                }
            )
        decks.sort(key=lambda d: d["name"].lower())
        return decks

    return run_on_main_sync(work)


def _collect_counts(node, out):
    # The root node has deck_id 0; skip it but walk its children.
    deck_id = getattr(node, "deck_id", 0)
    if deck_id:
        out[deck_id] = (
            getattr(node, "new_count", 0),
            getattr(node, "learn_count", 0),
            getattr(node, "review_count", 0),
        )
    for child in getattr(node, "children", []):
        _collect_counts(child, out)


# `deck_due_tree()` walks the whole deck tree to compute scheduler-respected
# due counts. A single sync fans out one `list_decks` call followed by one
# `review_batch` call per deck — recomputing the tree for each would be
# wasteful, so cache the current tree and hand every caller the same one.
_due_tree_cache = {"counts": None, "expires_at": 0.0}
_DUE_TREE_CACHE_TTL = 5.0  # seconds — safety net for changes made outside this server


def _invalidate_due_tree_cache():
    _due_tree_cache["counts"] = None


def _compute_due_counts(col):
    """{deck_id: (new, learn, review)} straight from the scheduler, respecting
    daily limits. {} if the scheduler call fails."""
    counts = {}
    try:
        tree = col.sched.deck_due_tree()
        _collect_counts(tree, counts)
    except Exception:
        return {}
    return counts


def _refresh_due_tree_cache(col):
    """Unconditionally recompute the due tree and cache it. `list_decks` (the
    home screen's source of truth) always calls this, priming the cache for
    the `review_batch` calls that follow it in the same sync."""
    counts = _compute_due_counts(col)
    _due_tree_cache["counts"] = counts
    _due_tree_cache["expires_at"] = time.monotonic() + _DUE_TREE_CACHE_TTL
    return counts


def _current_due_counts(col):
    """Cached due-tree counts, recomputing only if missing/expired — used by
    `review_batch` so N decks in one sync don't each re-walk the full tree."""
    if _due_tree_cache["counts"] is not None and time.monotonic() < _due_tree_cache["expires_at"]:
        return _due_tree_cache["counts"]
    return _refresh_due_tree_cache(col)


# --------------------------------------------------------------- review batch
def review_batch(deck_name, limit, include_new=True, include_media=False, respect_daily_limits=False):
    """Fetch a batch of cards for review.

    Parameters
    ----------
    deck_name:     Deck to search.
    limit:         Maximum number of cards to return.
    include_new:   When True (default), new (unseen) cards are included
                   alongside due cards.
    include_media: When True, cards containing images, audio, or other rich
                   media are included and their raw HTML is returned in
                   ``frontHtml``/``backHtml`` fields alongside the plain-text
                   ``front``/``back`` fields.  When False (default), such
                   cards are still returned but are flagged with
                   ``unsupportedMedia: true`` so clients can choose to skip
                   them.
    respect_daily_limits: `find_cards` matches every due/new card in the deck
                   regardless of its daily new/review limits, so by default
                   (False) a batch can include more cards than `list_decks`
                   shows for the deck (e.g. for a "study beyond today's
                   limit" flow). When True, results are clamped to the same
                   scheduler-respected new/learn/review counts `list_decks`
                   reports, so a session never shows more cards than the
                   deck list already promised.
    """
    def work():
        col = _col()
        if not _deck_exists(col, deck_name):
            raise AnkiError("deck_not_found", f"No deck named {deck_name!r}")

        due_part = "(is:due OR is:new)" if include_new else "is:due"
        query = f'deck:"{_escape(deck_name)}" {due_part} -is:suspended -is:buried'
        try:
            card_ids = list(col.find_cards(query))
        except Exception as exc:  # malformed search etc.
            raise AnkiError("search_error", str(exc))

        did = col.decks.id_for_name(deck_name) if respect_daily_limits else None
        caps = _current_due_counts(col).get(did) if did else None
        if caps is not None:
            new_cap, learn_cap, review_cap = caps
            buckets = {"new": [], "learning": [], "review": []}
            for cid in card_ids:
                try:
                    kind = const.CARD_TYPE_TO_DUE_KIND.get(int(col.get_card(cid).type), "review")
                except Exception:
                    kind = "review"
                buckets["learning" if kind in ("learning", "relearning") else kind].append(cid)
            card_ids = buckets["new"][:new_cap] + buckets["learning"][:learn_cap] + buckets["review"][:review_cap]

        cards = []
        seen = set()
        for cid in card_ids:
            if len(cards) >= limit:
                break
            if cid in seen:
                continue
            seen.add(cid)
            try:
                cards.append(_card_payload(col, cid, include_media=include_media))
            except Exception:
                # Skip a card we cannot render rather than fail the batch.
                continue
        return cards

    return run_on_main_sync(work)


def _deck_exists(col, deck_name):
    try:
        return col.decks.id_for_name(deck_name) is not None
    except Exception:
        # Older API fallback.
        try:
            return col.decks.byName(deck_name) is not None
        except Exception:
            return False


def _is_cloze_note(note):
    """Return True if the note belongs to a cloze note type."""
    try:
        return int(note.note_type().get("type", 0)) == 1
    except Exception:
        pass
    try:
        # Older Anki API fallback.
        return int(note.model().get("type", 0)) == 1
    except Exception:
        return False


def _card_payload(col, cid, include_media=False):
    card = col.get_card(cid)
    note = card.note()

    front_html = ""
    back_html = ""
    try:
        out = card.render_output()
        front_html = out.question_text or ""
        answer_html = out.answer_text or ""
        back_html = _strip_front_from_answer(answer_html)
        # render_output() strips [sound:...] refs out of the text into
        # question_av_tags/answer_av_tags (Anki's reviewer plays them without
        # inlining markup) — put them back so clients that scan the HTML for
        # `[sound:file]` (this add-on's own contract, see README) find them.
        front_html += _sound_tags_markup(getattr(out, "question_av_tags", None))
        back_html += _sound_tags_markup(getattr(out, "answer_av_tags", None))
    except Exception:
        # Fall back to raw note fields (front = first, back = second).
        fields = list(note.values())
        front_html = fields[0] if fields else ""
        back_html = fields[1] if len(fields) > 1 else ""

    unsupported = _has_unsupported_media(front_html) or _has_unsupported_media(
        back_html
    )

    card_type = "cloze" if _is_cloze_note(note) else "basic"

    payload = {
        "cardId": int(card.id),
        "noteId": int(note.id),
        "deckName": col.decks.name(card.did),
        "cardType": card_type,
        "front": _to_plain_text(front_html),
        "back": _to_plain_text(back_html),
        "dueKind": const.CARD_TYPE_TO_DUE_KIND.get(int(card.type), "review"),
    }
    if unsupported:
        payload["unsupportedMedia"] = True
    if include_media and (front_html or back_html):
        payload["frontHtml"] = front_html
        payload["backHtml"] = back_html
    return payload


# ----------------------------------------------------------------- card state
# Human-readable labels for Anki's numeric queue/type fields (debug aid).
_QUEUE_LABELS = {
    -3: "user_buried",
    -2: "sched_buried",
    -1: "suspended",
    0: "new",
    1: "learning",
    2: "review",
    3: "day_learning",
    4: "preview",
}
_TYPE_LABELS = {0: "new", 1: "learning", 2: "review", 3: "relearning"}


def card_state(card_id):
    """Return raw scheduling state for one card (debugging / verification)."""

    def work():
        col = _col()
        try:
            card = col.get_card(int(card_id))
        except Exception:
            raise AnkiError("card_not_found", f"No card with id {card_id}")
        note = card.note()
        return {
            "cardId": int(card.id),
            "noteId": int(note.id),
            "deckName": col.decks.name(card.did),
            "reps": int(card.reps),
            "lapses": int(card.lapses),
            "queue": int(card.queue),
            "queueLabel": _QUEUE_LABELS.get(int(card.queue), "unknown"),
            "type": int(card.type),
            "typeLabel": _TYPE_LABELS.get(int(card.type), "unknown"),
            "due": int(card.due),
            "interval": int(card.ivl),
            "factor": int(card.factor),
            "dueKind": const.CARD_TYPE_TO_DUE_KIND.get(int(card.type), "review"),
        }

    return run_on_main_sync(work)


# --------------------------------------------------------------- answer card
def answer_card(card_id, rating):
    ease = const.RATING_TO_EASE.get(rating)
    if ease is None:
        raise AnkiError("invalid_rating", f"Unknown rating {rating!r}")

    def work():
        col = _col()
        try:
            card = col.get_card(int(card_id))
        except Exception:
            raise AnkiError("card_not_found", f"No card with id {card_id}")

        # Suspended/buried cards cannot be answered.
        if card.queue < 0:
            raise AnkiError(
                "card_not_answerable",
                "Card is suspended or buried and cannot be reviewed",
            )

        _apply_answer(col, card, ease)
        _invalidate_due_tree_cache()  # this review just changed the due counts

        # Refresh Anki's main window so due counts update, but never disrupt an
        # in-progress review on the desktop.
        try:
            if mw.state in ("deckBrowser", "overview"):
                mw.reset()
        except Exception:
            pass

        return {"cardId": int(card.id), "ankiEase": ease}

    return run_on_main_sync(work)


def _apply_answer(col, card, ease):
    """Record the review through Anki's scheduler (v3 preferred, v2 fallback)."""
    sched = col.sched

    # Anki's Reviewer starts the card's timer when it shows a card; the
    # scheduler later computes "time taken" as time.time() - card.timer_started.
    # We fetched the card directly, so timer_started is None and the math would
    # raise. Start it now (a near-zero time-taken is fine and Anki caps it).
    _ensure_timer_started(card)

    # --- Scheduler v3 path -----------------------------------------------
    if hasattr(sched, "build_answer") and hasattr(sched, "answer_card"):
        card_answer_cls = _card_answer_cls()
        if card_answer_cls is not None:
            rating_map = {
                1: card_answer_cls.AGAIN,
                2: card_answer_cls.HARD,
                3: card_answer_cls.GOOD,
                4: card_answer_cls.EASY,
            }
            try:
                states = col._backend.get_scheduling_states(card.id)
                answer = sched.build_answer(
                    card=card, states=states, rating=rating_map[ease]
                )
                sched.answer_card(answer)
                return
            except AnkiError:
                raise
            except Exception as exc:
                raise AnkiError("scheduler_error", str(exc))

    # --- Legacy v1/v2 path ------------------------------------------------
    if hasattr(sched, "answerCard"):
        try:
            sched.answerCard(card, ease)
            return
        except Exception as exc:
            raise AnkiError("scheduler_error", str(exc))

    raise AnkiError(
        "scheduler_error", "No compatible Anki scheduler API was found"
    )


def _ensure_timer_started(card):
    try:
        card.start_timer()
        return
    except Exception:
        pass
    try:
        card.startTimer()  # legacy name
        return
    except Exception:
        pass
    try:
        import time as _time

        card.timer_started = _time.time()
    except Exception:
        pass


def _card_answer_cls():
    try:
        from anki.scheduler.v3 import CardAnswer

        return CardAnswer
    except Exception:
        pass
    try:
        from anki.scheduler_pb2 import CardAnswer

        return CardAnswer
    except Exception:
        return None


# ------------------------------------------------------------ text rendering
_HR_ANSWER_RE = re.compile(r'(?i)<hr\s+id=?["\']?answer["\']?[^>]*>', re.I)
_MEDIA_RE = re.compile(r"(?i)\[sound:[^\]]+\]|<img\b|<audio\b|<video\b|<object\b")
_MATHJAX_RE = re.compile(r"\\\(|\\\[|\\begin\{|\$\$|\[\$\$")
_TAG_RE = re.compile(r"<[^>]+>")
_BREAKING_TAGS_RE = re.compile(r"(?i)<br\s*/?>|</div>|</p>|</li>|</tr>|<hr\b[^>]*>")


def _strip_front_from_answer(answer_html):
    """Anki answer = FrontSide + <hr id=answer> + Back. Keep only the Back."""
    parts = _HR_ANSWER_RE.split(answer_html, maxsplit=1)
    if len(parts) > 1:
        return parts[1]
    return answer_html


def _sound_tags_markup(av_tags):
    """`[sound:file]` markup for a card's AVTag list (duck-typed: real audio
    files have a ``filename``; TTS tags don't reference an on-disk file and
    are skipped since there's nothing for the client to download)."""
    names = [getattr(tag, "filename", None) for tag in (av_tags or [])]
    return "".join(f"[sound:{name}]" for name in names if name)


def _has_unsupported_media(raw_html):
    if not raw_html:
        return False
    return bool(_MEDIA_RE.search(raw_html) or _MATHJAX_RE.search(raw_html))


def _to_plain_text(raw_html):
    if not raw_html:
        return ""
    text = _BREAKING_TAGS_RE.sub(" ", raw_html)
    text = re.sub(r"(?i)\[sound:[^\]]+\]", " ", text)
    if _anki_strip_html is not None:
        try:
            text = _anki_strip_html(text)
        except Exception:
            text = _TAG_RE.sub("", text)
    else:
        text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _escape(deck_name):
    return deck_name.replace("\\", "\\\\").replace('"', '\\"')
