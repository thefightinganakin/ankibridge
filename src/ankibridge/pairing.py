"""Pairing code generation and bearer-token management."""

import json
import os
import secrets
import threading
import time

# How long a pending "please approve me" request stays valid before it expires.
PAIR_REQUEST_TTL = 120.0
# How long a decided/expired request lingers (so the phone can read the outcome)
# before it is pruned.
PAIR_REQUEST_GRACE = 300.0


class Device:
    def __init__(self, token, name):
        self.token = token
        self.name = name
        self.paired_at = time.time()
        self.last_seen = self.paired_at

    def touch(self):
        self.last_seen = time.time()


class PairRequest:
    """A phone asking to connect, awaiting Allow/Deny on the desktop."""

    def __init__(self, request_id, name, remote):
        self.id = request_id
        self.name = name
        self.remote = remote
        self.created_at = time.time()
        self.decided_at = None
        self.status = "pending"  # pending | approved | denied | expired
        self.token = None

    def is_expired(self, now=None):
        now = now or time.time()
        return self.status == "pending" and (now - self.created_at) > PAIR_REQUEST_TTL


class PairingManager:
    def __init__(self, state_path=None):
        self._lock = threading.Lock()
        self._state_path = state_path
        self._code = None
        self._tokens = {}  # token -> Device
        self._requests = {}  # requestId -> PairRequest
        self._load_state()

    # ------------------------------------------------------------------ codes
    def _generate_code_locked(self):
        self._code = "".join(secrets.choice("0123456789") for _ in range(6))
        self._tokens.clear()
        self._requests.clear()
        self._persist_locked()
        return self._code

    def generate_code(self):
        """Generate a fresh 6-digit pairing code and invalidate old tokens."""
        with self._lock:
            return self._generate_code_locked()

    def ensure_code(self):
        with self._lock:
            if not self._code:
                return self._generate_code_locked()
            return self._code

    @property
    def code(self):
        with self._lock:
            return self._code

    @property
    def code_display(self):
        with self._lock:
            if not self._code:
                return "------"
            return f"{self._code[:3]}-{self._code[3:]}"

    # ----------------------------------------------------------------- tokens
    def pair(self, pairing_code, device_name):
        """Validate a pairing code and, if valid, mint a bearer token.

        Returns the token string on success, or None on failure.
        """
        normalized = (pairing_code or "").replace("-", "").replace(" ", "").strip()
        with self._lock:
            if not self._code or normalized != self._code:
                return None
            token = secrets.token_urlsafe(32)
            self._tokens[token] = Device(token, (device_name or "Unknown device").strip())
            self._persist_locked()
            return token

    # ---------------------------------------------------- approve-on-desktop
    def create_request(self, device_name, remote=""):
        """Register a phone's request to connect and return the PairRequest."""
        with self._lock:
            self._prune_locked()
            request_id = secrets.token_urlsafe(16)
            req = PairRequest(
                request_id, (device_name or "Unknown device").strip(), remote
            )
            self._requests[request_id] = req
            return req

    def get_request(self, request_id):
        """Return the PairRequest (marking it expired if stale), else None."""
        with self._lock:
            req = self._requests.get(request_id)
            if req is not None and req.is_expired():
                req.status = "expired"
                req.decided_at = req.decided_at or time.time()
            return req

    def pending_requests(self):
        with self._lock:
            self._prune_locked()
            return [r for r in self._requests.values() if r.status == "pending"]

    def approve_request(self, request_id):
        """Approve a pending request, minting a token. Returns token or None."""
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                return None
            if req.is_expired():
                req.status = "expired"
                req.decided_at = req.decided_at or time.time()
                return None
            if req.status != "pending":
                return None
            token = secrets.token_urlsafe(32)
            self._tokens[token] = Device(token, req.name)
            req.status = "approved"
            req.token = token
            req.decided_at = time.time()
            self._persist_locked()
            return token

    def deny_request(self, request_id):
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status != "pending":
                return False
            req.status = "denied"
            req.decided_at = time.time()
            return True

    def _prune_locked(self):
        now = time.time()
        stale = []
        for rid, req in self._requests.items():
            if req.is_expired(now):
                req.status = "expired"
                req.decided_at = req.decided_at or now
            anchor = req.decided_at or req.created_at
            if req.status != "pending" and (now - anchor) > PAIR_REQUEST_GRACE:
                stale.append(rid)
        for rid in stale:
            self._requests.pop(rid, None)

    def validate(self, token):
        """Return the Device for a valid token (and mark it seen), else None."""
        if not token:
            return None
        with self._lock:
            device = self._tokens.get(token)
            if device:
                device.touch()
            return device

    def devices(self):
        with self._lock:
            return list(self._tokens.values())

    def device_count(self):
        with self._lock:
            return len(self._tokens)

    def _load_state(self):
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        code = data.get("code")
        tokens = data.get("tokens")
        if isinstance(code, str) and len(code) == 6 and code.isdigit():
            self._code = code
        if not isinstance(tokens, dict):
            return
        for token, device_data in tokens.items():
            if not isinstance(token, str) or not isinstance(device_data, dict):
                continue
            name = (device_data.get("name") or "Unknown device").strip()
            device = Device(token, name)
            paired_at = device_data.get("paired_at")
            last_seen = device_data.get("last_seen")
            if isinstance(paired_at, (int, float)):
                device.paired_at = float(paired_at)
            if isinstance(last_seen, (int, float)):
                device.last_seen = float(last_seen)
            self._tokens[token] = device

    def _persist_locked(self):
        if not self._state_path:
            return
        tmp_path = self._state_path + ".tmp"
        try:
            state_dir = os.path.dirname(self._state_path)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
                try:
                    os.chmod(state_dir, 0o700)
                except Exception:
                    pass
            payload = {
                "code": self._code,
                "tokens": {
                    token: {
                        "name": device.name,
                        "paired_at": device.paired_at,
                        "last_seen": device.last_seen,
                    }
                    for token, device in self._tokens.items()
                },
            }
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass
            os.replace(tmp_path, self._state_path)
            try:
                os.chmod(self._state_path, 0o600)
            except Exception:
                pass
        except Exception:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
