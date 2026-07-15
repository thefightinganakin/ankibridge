"""Process-wide runtime state: config, logger, pairing, live stats.

A single RUNTIME instance is shared by the HTTP server thread and the Qt UI
thread, so all mutable stat fields are guarded by a lock.
"""

import os
import threading
import time

from . import const
from .logger import Logger
from .pairing import PairingManager


class Runtime:
    def __init__(self):
        addon_dir = os.path.dirname(__file__)
        user_files_dir = os.path.join(addon_dir, "user_files")
        log_path = os.path.join(user_files_dir, "ankibridge.log")
        pairing_state_path = os.path.join(user_files_dir, "pairing_state.json")
        self.logger = Logger(log_path, to_file=True)
        self.pairing = PairingManager(pairing_state_path)

        self.config = {
            "port": const.DEFAULT_PORT,
            "bind_address": const.DEFAULT_BIND,
            "log_to_file": True,
            "enable_mdns": False,
        }

        self._lock = threading.Lock()
        self.running = False
        self.bound_port = None
        self.bound_address = None
        self.last_request = None
        self.last_error = None
        self.sessions = {}  # sessionId -> dict

    def configure(self, cfg):
        if not isinstance(cfg, dict):
            cfg = {}
        merged = dict(self.config)
        for key in ("port", "bind_address", "log_to_file", "enable_mdns"):
            if key in cfg and cfg[key] is not None:
                merged[key] = cfg[key]
        self.config = merged
        self.logger.to_file = bool(merged.get("log_to_file", True))
        return merged

    # ------------------------------------------------------------- live stats
    def set_running(self, running, address=None, port=None):
        with self._lock:
            self.running = running
            self.bound_address = address
            self.bound_port = port

    def note_request(self, method, path, remote):
        with self._lock:
            self.last_request = {
                "method": method,
                "path": path,
                "remote": remote,
                "at": time.time(),
            }

    def note_error(self, message):
        with self._lock:
            self.last_error = {"message": str(message), "at": time.time()}

    def snapshot(self):
        with self._lock:
            return {
                "running": self.running,
                "bound_address": self.bound_address,
                "bound_port": self.bound_port,
                "last_request": self.last_request,
                "last_error": self.last_error,
            }


RUNTIME = Runtime()
