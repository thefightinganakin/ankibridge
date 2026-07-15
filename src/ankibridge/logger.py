"""Structured, in-memory + file logging for the add-on.

Never raises: logging failures must not take down Anki or the HTTP server.
"""

import collections
import os
import threading
import time
import traceback


class Logger:
    def __init__(self, log_path, to_file=True, max_lines=500):
        self.log_path = log_path
        self.to_file = to_file
        self._buffer = collections.deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def _format(self, event, message, fields):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = [ts, event]
        if message:
            parts.append(str(message))
        if fields:
            kv = " ".join(f"{k}={v}" for k, v in fields.items())
            parts.append(kv)
        return " | ".join(parts)

    def log(self, event, message="", **fields):
        line = self._format(event, message, fields)
        with self._lock:
            self._buffer.append(line)
        # Console (Anki captures stdout in the debug console / terminal).
        try:
            print(f"[AnkiBridge] {line}")
        except Exception:
            pass
        if self.to_file:
            self._write_file(line)
        return line

    def exception(self, event, message="", **fields):
        tb = traceback.format_exc()
        line = self.log(event, message, **fields)
        with self._lock:
            self._buffer.append(tb.rstrip())
        if self.to_file:
            self._write_file(tb.rstrip())
        return line

    def _write_file(self, line):
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            # Deliberately swallow: file logging is best-effort.
            pass

    def recent(self, n=200):
        with self._lock:
            items = list(self._buffer)
        return items[-n:]
