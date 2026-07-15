"""AnkiBridge — Anki Desktop add-on entry point.

Starts a local HTTP server that lets the AnkiBridge mobile app connect over the
local network, fetch decks and due cards, and submit review ratings that are
applied through Anki's own scheduler.
"""

from aqt import gui_hooks, mw

from . import const, server, ui
from .runtime import RUNTIME


def _load_config():
    try:
        cfg = mw.addonManager.getConfig(__name__)
    except Exception:
        cfg = None
    RUNTIME.configure(cfg or {})


def _on_profile_open():
    # A profile (and its collection) is now open, so we have a profile name and
    # can safely start serving requests that touch the collection.
    _load_config()
    server.start()


def _on_profile_close():
    server.stop()


# Register the Tools menu item immediately; start/stop the server with the
# collection lifecycle.
ui.setup_menu()
gui_hooks.profile_did_open.append(_on_profile_open)
gui_hooks.profile_will_close.append(_on_profile_close)
