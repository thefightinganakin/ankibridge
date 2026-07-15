"""Constants shared across the AnkiBridge add-on."""

ADDON_NAME = "AnkiBridge"
VERSION = "0.1.0"

DEFAULT_PORT = 48731
DEFAULT_BIND = "0.0.0.0"

# mDNS / Bonjour service type (discovery is out of scope for MVP but the
# architecture stays compatible with it).
MDNS_SERVICE_TYPE = "_ankibridge._tcp.local."

# Map rating strings to Anki "ease" button numbers.
RATING_TO_EASE = {
    "again": 1,
    "hard": 2,
    "good": 3,
    "easy": 4,
}

# Map Anki card.type to the dueKind vocabulary.
CARD_TYPE_TO_DUE_KIND = {
    0: "new",
    1: "learning",
    2: "review",
    3: "relearning",
}


class Ev:
    """Structured log event names (see spec)."""

    SERVER_START = "SERVER_START"
    SERVER_STOP = "SERVER_STOP"
    SERVER_ERROR = "SERVER_ERROR"
    PAIRING_CODE_GENERATED = "PAIRING_CODE_GENERATED"
    PAIRING_SUCCESS = "PAIRING_SUCCESS"
    PAIRING_FAILURE = "PAIRING_FAILURE"
    PAIR_REQUEST_RECEIVED = "PAIR_REQUEST_RECEIVED"
    PAIR_REQUEST_APPROVED = "PAIR_REQUEST_APPROVED"
    PAIR_REQUEST_DENIED = "PAIR_REQUEST_DENIED"
    PAIR_REQUEST_EXPIRED = "PAIR_REQUEST_EXPIRED"
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    AUTH_FAILURE = "AUTH_FAILURE"
    DECKS_REQUESTED = "DECKS_REQUESTED"
    REVIEW_BATCH_REQUESTED = "REVIEW_BATCH_REQUESTED"
    REVIEW_BATCH_RETURNED = "REVIEW_BATCH_RETURNED"
    ANSWER_CARD_REQUESTED = "ANSWER_CARD_REQUESTED"
    ANSWER_CARD_SUCCESS = "ANSWER_CARD_SUCCESS"
    ANSWER_CARD_FAILURE = "ANSWER_CARD_FAILURE"
    SCHEDULER_ERROR = "SCHEDULER_ERROR"
