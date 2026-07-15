"""Local HTTP server exposing the narrow AnkiBridge API.

The server runs on a background thread. Handlers catch every exception and
return JSON errors so a bad request can never crash Anki. Any operation that
touches the collection is delegated to ``anki_bridge`` (which hops to the main
thread).
"""

import json
import mimetypes
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from . import anki_bridge, const, discovery, pairing
from .anki_bridge import AnkiError
from .runtime import RUNTIME

_httpd = None
_thread = None


# -------------------------------------------------------------------- server
class AnkiBridgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't spam Anki's stderr on client disconnects.

    A phone closing the connection mid-request raises ConnectionResetError /
    BrokenPipeError deep inside socketserver. The default ``handle_error``
    prints a full traceback to stderr, which Anki turns into an error popup.
    Those disconnects are benign, so swallow them; route anything else through
    our own logger instead of stderr.
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
            return  # client went away; nothing to see here
        try:
            RUNTIME.logger.exception(
                const.Ev.SERVER_ERROR, message=f"request error from {client_address}"
            )
        except Exception:
            pass


# ------------------------------------------------------------------- handler
class AnkiBridgeHandler(BaseHTTPRequestHandler):
    server_version = "AnkiBridge/" + const.VERSION
    protocol_version = "HTTP/1.1"

    # Quiet the default noisy stderr logging; we do our own.
    def log_message(self, fmt, *args):
        return

    # -- helpers ----------------------------------------------------------
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers", "Authorization, Content-Type"
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise AnkiError("invalid_json", "Request body is not valid JSON")
        if not isinstance(data, dict):
            raise AnkiError("invalid_json", "Request body must be a JSON object")
        return data

    def _bearer_token(self):
        header = self.headers.get("Authorization", "") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return None

    def _require_auth(self):
        token = self._bearer_token()
        device = RUNTIME.pairing.validate(token)
        if device is None:
            RUNTIME.logger.log(
                const.Ev.AUTH_FAILURE, path=self.path, remote=self.client_address[0]
            )
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return None
        return device

    # -- verbs ------------------------------------------------------------
    def do_OPTIONS(self):
        # CORS preflight. 200 (not 204) because we always send a body/length.
        self._send_json(200, {"ok": True})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        RUNTIME.note_request(method, path, self.client_address[0])
        RUNTIME.logger.log(
            const.Ev.REQUEST_RECEIVED,
            method=method,
            path=path,
            remote=self.client_address[0],
        )
        try:
            route = _ROUTES.get((method, path))
            if route is not None:
                route(self)
                return
            # Variable path: GET /v1/pair-request/<id>
            if method == "GET" and path.startswith("/v1/pair-request/"):
                self.handle_pair_status(path[len("/v1/pair-request/"):])
                return
            # Variable path: GET /v1/card/<id>
            if method == "GET" and path.startswith("/v1/card/"):
                self.handle_card_state(path[len("/v1/card/"):])
                return
            # Variable path: GET /v1/media/<filename>
            if method == "GET" and path.startswith("/v1/media/"):
                self.handle_media(path[len("/v1/media/"):])
                return
            self._send_json(404, {"ok": False, "error": "not_found"})
        except AnkiError as exc:
            self._send_json(200, {"ok": False, "error": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 - never let a handler crash Anki
            RUNTIME.logger.exception(const.Ev.SERVER_ERROR, message=str(exc))
            RUNTIME.note_error(exc)
            self._send_json(
                500, {"ok": False, "error": "internal_error", "message": str(exc)}
            )

    # -- endpoints --------------------------------------------------------
    def handle_health(self):
        info = anki_bridge.health_info()
        self._send_json(
            200,
            {
                "ok": True,
                "app": const.ADDON_NAME,
                "version": const.VERSION,
                "requiresPairing": True,
                "computerName": info["computerName"],
                "ankiProfile": info["ankiProfile"],
            },
        )

    def handle_pair(self):
        data = self._read_json()
        code = data.get("pairingCode")
        device_name = data.get("deviceName", "Unknown device")
        token = RUNTIME.pairing.pair(code, device_name)
        if not token:
            RUNTIME.logger.log(
                const.Ev.PAIRING_FAILURE,
                device=device_name,
                remote=self.client_address[0],
            )
            self._send_json(
                200, {"ok": False, "error": "invalid_pairing_code"}
            )
            return
        info = anki_bridge.health_info()
        RUNTIME.logger.log(
            const.Ev.PAIRING_SUCCESS,
            device=device_name,
            remote=self.client_address[0],
        )
        self._send_json(
            200,
            {
                "ok": True,
                "token": token,
                "computerName": info["computerName"],
                "ankiProfile": info["ankiProfile"],
            },
        )

    def handle_pair_request(self):
        """Phone asks to connect; the user approves on the desktop.

        Returns immediately with a requestId. A dialog is raised on Anki's main
        thread; the phone polls ``GET /v1/pair-request/<id>`` for its token.
        """
        data = self._read_json()
        device_name = data.get("deviceName", "Unknown device")
        req = RUNTIME.pairing.create_request(device_name, self.client_address[0])
        RUNTIME.logger.log(
            const.Ev.PAIR_REQUEST_RECEIVED,
            device=device_name,
            remote=self.client_address[0],
            request=req.id,
        )
        # Prompt the user on the main (UI) thread without blocking this handler.
        try:
            from . import ui

            ui.prompt_pair_request(req.id)
        except Exception as exc:  # noqa: BLE001 - never crash on UI issues
            RUNTIME.logger.exception(const.Ev.SERVER_ERROR, message=str(exc))
        self._send_json(
            200,
            {
                "ok": True,
                "requestId": req.id,
                "status": "pending",
                "pollAfterMs": 1000,
                "expiresInMs": int(pairing.PAIR_REQUEST_TTL * 1000),
            },
        )

    def handle_pair_status(self, request_id):
        """Phone polls the outcome of its pairing request."""
        req = RUNTIME.pairing.get_request((request_id or "").strip())
        if req is None:
            raise AnkiError("pair_request_not_found", "Unknown pairing request")
        payload = {"ok": True, "requestId": req.id, "status": req.status}
        if req.status == "approved" and req.token:
            info = anki_bridge.health_info()
            payload["token"] = req.token
            payload["computerName"] = info["computerName"]
            payload["ankiProfile"] = info["ankiProfile"]
        return self._send_json(200, payload)

    def handle_decks(self):
        if self._require_auth() is None:
            return
        RUNTIME.logger.log(const.Ev.DECKS_REQUESTED)
        decks = anki_bridge.list_decks()
        self._send_json(200, {"ok": True, "decks": decks})

    def handle_review_batch(self):
        if self._require_auth() is None:
            return
        data = self._read_json()
        deck_name = data.get("deckName")
        if not deck_name:
            raise AnkiError("invalid_request", "deckName is required")
        limit = data.get("limit", 20)
        try:
            limit = max(1, min(int(limit), 200))
        except Exception:
            limit = 20
        include_new = bool(data.get("includeNew", True))
        include_media = bool(data.get("includeMedia", False))
        respect_daily_limits = bool(data.get("respectDailyLimits", False))

        RUNTIME.logger.log(
            const.Ev.REVIEW_BATCH_REQUESTED, deck=deck_name, limit=limit
        )
        cards = anki_bridge.review_batch(
            deck_name, limit,
            include_new=include_new,
            include_media=include_media,
            respect_daily_limits=respect_daily_limits,
        )

        session_id = str(uuid.uuid4())
        RUNTIME.sessions[session_id] = {
            "deckName": deck_name,
            "cardIds": [c["cardId"] for c in cards],
        }
        RUNTIME.logger.log(
            const.Ev.REVIEW_BATCH_RETURNED, deck=deck_name, count=len(cards)
        )

        response = {
            "ok": True,
            "sessionId": session_id,
            "deckName": deck_name,
            "cards": cards,
        }
        if not cards:
            response["message"] = "No due cards found"
        self._send_json(200, response)

    def handle_card_state(self, raw_id):
        if self._require_auth() is None:
            return
        try:
            card_id = int(raw_id.strip())
        except Exception:
            raise AnkiError("invalid_request", "card id must be an integer")
        state = anki_bridge.card_state(card_id)
        self._send_json(200, {"ok": True, "card": state})

    def handle_media(self, raw_name):
        """Stream a single file from Anki's media folder (bearer-auth).

        Only a bare filename is accepted; any path separators or parent refs are
        rejected and the resolved path is confirmed to sit inside the media dir,
        so a client can never read outside the collection's media.
        """
        if self._require_auth() is None:
            return
        name = os.path.basename(unquote(raw_name or "").strip())
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            self._send_json(400, {"ok": False, "error": "invalid_media_name"})
            return

        media_dir = anki_bridge.media_dir()
        path = os.path.realpath(os.path.join(media_dir, name))
        # Path-traversal guard: the resolved path must live inside the media dir.
        if os.path.commonpath([path, os.path.realpath(media_dir)]) != os.path.realpath(media_dir):
            self._send_json(400, {"ok": False, "error": "invalid_media_name"})
            return
        if not os.path.isfile(path):
            self._send_json(404, {"ok": False, "error": "media_not_found"})
            return

        with open(path, "rb") as fh:
            data = fh.read()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def handle_answer_card(self):
        if self._require_auth() is None:
            return
        data = self._read_json()
        card_id = data.get("cardId")
        rating = data.get("rating")
        if card_id is None:
            raise AnkiError("invalid_request", "cardId is required")
        if rating not in const.RATING_TO_EASE:
            raise AnkiError("invalid_rating", f"Unknown rating {rating!r}")

        RUNTIME.logger.log(
            const.Ev.ANSWER_CARD_REQUESTED, card=card_id, rating=rating
        )
        try:
            result = anki_bridge.answer_card(card_id, rating)
        except AnkiError as exc:
            RUNTIME.logger.log(
                const.Ev.ANSWER_CARD_FAILURE,
                card=card_id,
                rating=rating,
                error=exc.code,
            )
            if exc.code == "scheduler_error":
                RUNTIME.logger.exception(const.Ev.SCHEDULER_ERROR, message=exc.message)
            raise
        RUNTIME.logger.log(
            const.Ev.ANSWER_CARD_SUCCESS, card=card_id, rating=rating
        )
        self._send_json(
            200,
            {
                "ok": True,
                "cardId": result["cardId"],
                "rating": rating,
                "ankiEase": result["ankiEase"],
            },
        )


_ROUTES = {
    ("GET", "/v1/health"): AnkiBridgeHandler.handle_health,
    ("POST", "/v1/pair"): AnkiBridgeHandler.handle_pair,
    ("POST", "/v1/pair-request"): AnkiBridgeHandler.handle_pair_request,
    ("GET", "/v1/decks"): AnkiBridgeHandler.handle_decks,
    ("POST", "/v1/review-batch"): AnkiBridgeHandler.handle_review_batch,
    ("POST", "/v1/answer-card"): AnkiBridgeHandler.handle_answer_card,
}


# ------------------------------------------------------------ lifecycle
def start():
    """Start (or restart) the HTTP server. Safe to call repeatedly."""
    global _httpd, _thread
    stop()

    if not RUNTIME.pairing.code:
        RUNTIME.pairing.ensure_code()
        RUNTIME.logger.log(
            const.Ev.PAIRING_CODE_GENERATED, code=RUNTIME.pairing.code_display
        )

    port = int(RUNTIME.config.get("port", const.DEFAULT_PORT))
    bind = RUNTIME.config.get("bind_address", const.DEFAULT_BIND)

    try:
        httpd = AnkiBridgeServer((bind, port), AnkiBridgeHandler)
        httpd.daemon_threads = True
    except Exception as exc:
        RUNTIME.logger.exception(
            const.Ev.SERVER_ERROR, message=f"Could not bind {bind}:{port}: {exc}"
        )
        RUNTIME.note_error(exc)
        RUNTIME.set_running(False)
        return False

    _httpd = httpd
    _thread = threading.Thread(
        target=httpd.serve_forever, name="AnkiBridgeServer", daemon=True
    )
    _thread.start()
    RUNTIME.set_running(True, address=bind, port=port)
    RUNTIME.logger.log(const.Ev.SERVER_START, bind=bind, port=port)

    # Optional mDNS advertising (no-op unless enabled and zeroconf present).
    try:
        discovery.start(port)
    except Exception:
        pass
    return True


def stop():
    global _httpd, _thread
    try:
        discovery.stop()
    except Exception:
        pass
    if _httpd is not None:
        try:
            _httpd.shutdown()
            _httpd.server_close()
        except Exception:
            pass
        RUNTIME.logger.log(const.Ev.SERVER_STOP)
    _httpd = None
    _thread = None
    RUNTIME.set_running(False)


def is_running():
    return _httpd is not None
