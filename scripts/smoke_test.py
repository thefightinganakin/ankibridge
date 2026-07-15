#!/usr/bin/env python3
"""Manual smoke test for the AnkiBridge API.

Run this while Anki is open with the add-on installed.

    python3 scripts/smoke_test.py --host 127.0.0.1 --port 48731 --code 482913

Steps: health -> pair -> decks -> review-batch -> answer-card (first card, good).
Stdlib only; no dependencies.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

_MEDIA_REF_RE = re.compile(r'(?i)<(?:img|source)\b[^>]*\bsrc="([^"]+)"|\[sound:([^\]]+)\]')


def fetch_bytes(base, path, token=None):
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None, exc.read()


def call(base, method, path, token=None, body=None):
    url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"raw": str(exc)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=48731)
    ap.add_argument("--code", required=True, help="6-digit pairing code (dashes ok)")
    ap.add_argument("--deck", default=None, help="deck to test; default = first with due")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--answer", action="store_true", help="actually answer the first card 'good'")
    args = ap.parse_args()

    base = f"http://{args.host}:{args.port}"

    print("== health ==")
    status, data = call(base, "GET", "/v1/health")
    print(status, json.dumps(data, indent=2))
    if not data.get("ok"):
        sys.exit("health check failed")

    print("\n== unauthorized decks (should be 401) ==")
    status, data = call(base, "GET", "/v1/decks")
    print(status, data)

    print("\n== pair ==")
    status, data = call(base, "POST", "/v1/pair", body={
        "pairingCode": args.code, "deviceName": "Smoke Test",
    })
    print(status, json.dumps(data, indent=2))
    token = data.get("token")
    if not token:
        sys.exit("pairing failed")

    print("\n== decks ==")
    status, data = call(base, "GET", "/v1/decks", token=token)
    decks = data.get("decks", [])
    for d in decks:
        print(f"  {d['name']}: due={d['dueCount']} new={d['newCount']} learn={d['learningCount']}")

    deck = args.deck
    if deck is None:
        due = [d for d in decks if d["dueCount"] > 0]
        deck = (due[0] if due else decks[0])["name"] if decks else None
    if not deck:
        sys.exit("no decks found")
    print(f"\n== review-batch ({deck}) ==")
    status, data = call(base, "POST", "/v1/review-batch", token=token, body={
        "deckName": deck, "limit": args.limit, "includeMedia": True,
    })
    cards = data.get("cards", [])
    print(f"got {len(cards)} cards (session {data.get('sessionId')})")
    for c in cards[:5]:
        print(f"  [{c['dueKind']}/{c.get('cardType')}] {c['front'][:60]!r} -> {c['back'][:60]!r}")

    print("\n== media ==")
    media_name = None
    for c in cards:
        for html in (c.get("frontHtml", ""), c.get("backHtml", "")):
            m = _MEDIA_REF_RE.search(html or "")
            if m:
                media_name = m.group(1) or m.group(2)
                break
        if media_name:
            break
    if not media_name:
        print("  (no media references found in this batch)")
    else:
        path = "/v1/media/" + urllib.parse.quote(media_name)
        st, ctype, blob = fetch_bytes(base, path, token=token)
        print(f"  {media_name!r} -> {st} {ctype} ({len(blob)} bytes)")
        # Path-traversal guard: encoded traversal collapses to a basename inside
        # the media dir, so it can never escape (400 for '..', else 404).
        st_bad, _, _ = fetch_bytes(base, "/v1/media/..%2f..%2fsecret", token=token)
        print(f"  traversal guard (../../secret) -> {st_bad} (expect 404, never 200)")

    if args.answer and cards:
        first = cards[0]
        print(f"\n== answer-card (cardId={first['cardId']}, good) ==")
        status, data = call(base, "POST", "/v1/answer-card", token=token, body={
            "sessionId": data.get("sessionId"),
            "cardId": first["cardId"],
            "rating": "good",
        })
        print(status, json.dumps(data, indent=2))

    print("\nDone.")


if __name__ == "__main__":
    main()
