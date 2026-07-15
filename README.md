# AnkiBridge — Anki Desktop Add-on

Lets other devices connect to Anki Desktop over the local
Wi-Fi network to fetch real decks and due cards, and to send review ratings back
so AnkiBridge sessions count toward the user's actual Anki schedule.

**Anki remains the source of truth.** This add-on records every rating through Anki's own scheduler, exactly as if the
user pressed Again / Hard / Good / Easy in the Reviewer. Due dates are never
edited by hand.

---

## What it does

When Anki opens, the add-on starts a small local HTTP server (default port
`48731`, bound to `0.0.0.0` so phones on the same Wi-Fi can reach it) that
exposes a narrow, versioned, pairing-protected API:

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET  /v1/health` | none | Identify the server, computer, Anki profile |
| `POST /v1/pair-request` | none | Ask to connect; user approves on the desktop |
| `GET  /v1/pair-request/<id>` | none | Poll a pairing request for its token |
| `POST /v1/pair` | none | Fallback: exchange a 6-digit code for a token |
| `GET  /v1/decks` | bearer | List decks with due / new / learning counts |
| `POST /v1/review-batch` | bearer | Fetch up to N real due cards from a deck |
| `POST /v1/answer-card` | bearer | Apply a rating through Anki's scheduler |
| `GET  /v1/media/<filename>` | bearer | Stream one file from the collection's media folder |
| `GET  /v1/card/<id>` | bearer | Debug: raw scheduling state of one card |

`GET /v1/card/<id>` is a verification aid — it returns the card's
`reps`, `lapses`, `queue`/`queueLabel`, `type`/`typeLabel`, `due`, `interval`,
and `factor` so you can confirm a rating changed the schedule without opening the
database. Example after answering:

```bash
curl -s http://127.0.0.1:48731/v1/card/1339252520772 \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# -> reps rises, queue/interval advance to the next step
```

This is **not** an AnkiConnect-style general API — only the AnkiBridge
endpoints above are exposed.

---

## Install (development)

```bash
# macOS: symlink into Anki's addons folder, then fully quit & reopen Anki
./scripts/dev_link.sh
```

Or copy `src/ankibridge/` into your Anki `addons21/ankibridge/` folder manually.

Then in Anki: **Tools → AnkiBridge** opens the status window showing the
address(es), pairing code, connected devices, recent activity, and logs.

## Build a distributable package

```bash
./scripts/build_ankiaddon.sh   # produces dist/ankibridge.ankiaddon
```

## Smoke test the API (stdlib only)

With Anki open and the add-on running, grab the pairing code from the status
window and:

```bash
python3 scripts/smoke_test.py --host 127.0.0.1 --port 48731 --code 482-913
# add --answer to actually rate the first returned card "good"
```

From another device on the same Wi-Fi, use the computer's LAN IP shown in the
status window (e.g. `http://192.168.1.42:48731`).

---

## Architecture

```
AnkiBridge mobile app
        │  HTTP over local Wi-Fi
        ▼
AnkiBridge add-on  (this repo)
  ├─ server.py       background-thread HTTP server + routing
  ├─ pairing.py      6-digit code + in-memory bearer tokens
  ├─ anki_bridge.py  collection/scheduler ops (hopped to Anki's main thread)
  ├─ runtime.py      shared config + live stats + logger + pairing
  ├─ ui.py           Tools menu + status window
  ├─ discovery.py    optional mDNS/Bonjour advertising (off by default)
  ├─ logger.py       structured in-memory + file logging
  └─ netutil.py      LAN IP / computer-name detection
        │  Python / Anki APIs
        ▼
Anki collection + scheduler   (source of truth)
```

### Threading safety

The HTTP server runs on a background thread. The Anki collection is **not**
thread-safe, so every collection read/write is dispatched to Anki's main thread
via `mw.taskman.run_on_main` and the server thread blocks on a result
(`anki_bridge.run_on_main_sync`). Nothing mutates the collection off-thread.

### Scheduler correctness

`anki_bridge._apply_answer` prefers the **v3 scheduler**:
`get_scheduling_states(card.id)` → `build_answer(...)` → `answer_card(...)`,
mapping ratings to `CardAnswer.AGAIN/HARD/GOOD/EASY`. It falls back to the
legacy `sched.answerCard(card, ease)` on older Anki. Scheduler exceptions are
returned as `{"ok": false, "error": "scheduler_error"}` and logged with a full
traceback — never silently faked.

Rating → ease mapping: `again→1, hard→2, good→3, easy→4`.

### Due-card selection

`review-batch` searches `deck:"<deck>" (is:due OR is:new) -is:suspended -is:buried`
by default, returning real due review + learning/relearning cards **and** new
(unseen) cards, while excluding suspended and buried cards.  Pass
`"includeNew": false` in the request body to suppress new cards.

That search matches every due/new card in the deck regardless of its daily
new/review limits, so by default a batch can include more cards than
`decks` shows for it (useful for a "study beyond today's limit" flow). Pass
`"respectDailyLimits": true` to clamp the batch to the same
scheduler-respected new/learning/review counts `decks` reports, so a session
never shows more cards than the deck list already promised.

Cards are rendered to plain text (HTML stripped, front side removed from the
answer).  Cards containing images, audio, video, or MathJax are still returned
as a plain-text approximation with `"unsupportedMedia": true`.  Pass
`"includeMedia": true` to additionally receive the raw rendered HTML in
`"frontHtml"` / `"backHtml"` fields so the client can render rich content
itself (audio, images, cloze highlights, etc.).  The HTML references media by
bare filename (`<img src="plant.jpg">`, `[sound:cell.mp3]`); fetch each file
from `GET /v1/media/<filename>` (bearer auth), which streams it straight from
the collection's media folder.  Only bare filenames are served — path
separators and parent refs are rejected — so clients cannot read outside the
media directory.

Every card payload now includes a `"cardType"` field: `"basic"` for standard
front/back cards, `"cloze"` for cloze-deletion note types.

---

## Pairing (approve-on-desktop)

The primary flow needs nothing typed on the phone — it works like Spotify
Connect or "approve this sign-in":

1. The phone discovers Anki on the LAN via mDNS (`_ankibridge._tcp.local.`)
   and shows it as e.g. *"Anki — Victor's MacBook."*
2. The user taps it. The app sends `POST /v1/pair-request` with its
   `deviceName`; the server returns a `requestId` immediately and raises an
   **Allow / Deny** dialog on the desktop.
3. The user clicks **Allow**. A bearer token is minted.
4. The phone polls `GET /v1/pair-request/<requestId>` and receives
   `{"status":"approved","token":"…"}` once approved (or `denied` / `expired`).

Pending requests expire after 120 s. The desktop dialog is the source of
authority — the phone can never mint its own token. The 6-digit code
(`POST /v1/pair`, shown in the status window) remains as a manual fallback.

## Security model

Local-network only, but never unauthenticated. Tokens are issued **only** by a
human clicking Allow on the desktop (or by entering the current 6-digit code).
Tokens are random and stored locally in AnkiBridge's `user_files` state so
approved devices stay connected across app restarts; all data endpoints require
`Authorization: Bearer <token>` and return `401 {"error":"unauthorized"}`
otherwise. The pairing code stays stable across normal restarts. Using
**Regenerate pairing code** from the status window rotates the code,
invalidates all previously issued tokens, and clears any pending pairing
requests.

If your OS firewall blocks inbound connections on the port, allow Anki (or the
port) for phones to connect.

---

## mDNS

mDNS auto-discovery (`_ankibridge._tcp.local.`) is implemented in
`discovery.py` with a dependency-free pure-Python advertiser (PTR/SRV/TXT/A
responses plus unsolicited announcements) and is **on by default**
(`enable_mdns: true`). It has been verified discoverable/resolvable via
`dns-sd`. Manual IP entry is still supported as a fallback.

## API shapes

See the product spec. Payloads match the mobile app's `AnkiDeck` / `AnkiCard` /
`ReviewBatch` TypeScript interfaces (`cardId`/`noteId` are numbers, deck `id` is
a string).
