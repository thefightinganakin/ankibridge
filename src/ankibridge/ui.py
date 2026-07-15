"""Anki UI: Tools menu item and the AnkiBridge status window."""

import time

from aqt import mw
from aqt.qt import (
    QAction,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    Qt,
    QTimer,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import tooltip

from . import const, netutil, server
from .runtime import RUNTIME

_dialog = None


def prompt_pair_request(request_id):
    """Raise an Allow/Deny dialog for a phone's pairing request.

    Called from the HTTP server thread; the actual dialog is shown on Anki's
    main (UI) thread. The phone polls for the outcome, so this never blocks the
    caller and its result is delivered purely through the PairingManager.
    """

    def show():
        req = RUNTIME.pairing.get_request(request_id)
        if req is None or req.status != "pending":
            return

        box = QMessageBox(mw)
        box.setWindowTitle(const.ADDON_NAME)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"“{req.name}” wants to connect to Anki")
        box.setInformativeText(
            f"Allow this device (from {req.remote}) to review your cards "
            "through AnkiBridge?"
        )
        allow_btn = box.addButton("Allow", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Deny", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(allow_btn)
        box.exec()

        if box.clickedButton() is allow_btn:
            token = RUNTIME.pairing.approve_request(request_id)
            if token:
                RUNTIME.logger.log(
                    const.Ev.PAIR_REQUEST_APPROVED,
                    device=req.name,
                    remote=req.remote,
                )
                tooltip(f"{req.name} connected to AnkiBridge.")
            else:
                RUNTIME.logger.log(
                    const.Ev.PAIR_REQUEST_EXPIRED, device=req.name
                )
                tooltip("That pairing request already expired.")
        else:
            RUNTIME.pairing.deny_request(request_id)
            RUNTIME.logger.log(
                const.Ev.PAIR_REQUEST_DENIED, device=req.name, remote=req.remote
            )

        if _dialog is not None:
            _dialog.refresh()

    try:
        mw.taskman.run_on_main(show)
    except Exception:
        pass


def setup_menu():
    action = QAction(const.ADDON_NAME, mw)
    qconnect(action.triggered, show_status_window)
    mw.form.menuTools.addAction(action)


def show_status_window():
    global _dialog
    if _dialog is None:
        _dialog = StatusDialog(mw)
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()
    _dialog.refresh()


def _fmt_time(ts):
    if not ts:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(ts))


class StatusDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(const.ADDON_NAME)
        self.setMinimumWidth(460)
        self._build()

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        qconnect(self._timer.timeout, self.refresh)
        self._timer.start()

    def _build(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Connect AnkiBridge to Anki\n\n"
            "Keep Anki open on this computer. In the AnkiBridge app on your phone, "
            "tap this computer and approve the request that pops up here. "
            "(You can also type the pairing code below.) Your phone and computer "
            "must be on the same Wi-Fi."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Connection group
        conn = QGroupBox("Connection")
        conn_l = QVBoxLayout(conn)
        self.status_label = QLabel()
        self.computer_label = QLabel()
        self.address_label = QLabel()
        self.address_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label = QLabel()
        self.code_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        for w in (
            self.status_label,
            self.computer_label,
            self.address_label,
            self.code_label,
        ):
            conn_l.addWidget(w)
        layout.addWidget(conn)

        # Activity group
        activity = QGroupBox("Activity")
        act_l = QVBoxLayout(activity)
        self.pending_label = QLabel()
        self.pending_label.setWordWrap(True)
        self.devices_label = QLabel()
        self.last_request_label = QLabel()
        self.last_error_label = QLabel()
        self.last_error_label.setWordWrap(True)
        for w in (
            self.pending_label,
            self.devices_label,
            self.last_request_label,
            self.last_error_label,
        ):
            act_l.addWidget(w)
        layout.addWidget(activity)

        # Logs
        logs = QGroupBox("Recent logs")
        logs_l = QVBoxLayout(logs)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(160)
        logs_l.addWidget(self.log_view)
        layout.addWidget(logs)

        # Buttons
        btns = QHBoxLayout()
        self.regen_btn = QPushButton("Regenerate pairing code")
        self.restart_btn = QPushButton("Restart server")
        self.close_btn = QPushButton("Close")
        qconnect(self.regen_btn.clicked, self._regenerate_code)
        qconnect(self.restart_btn.clicked, self._restart_server)
        qconnect(self.close_btn.clicked, self.close)
        btns.addWidget(self.regen_btn)
        btns.addWidget(self.restart_btn)
        btns.addStretch()
        btns.addWidget(self.close_btn)
        layout.addLayout(btns)

    # ------------------------------------------------------------- actions
    def _regenerate_code(self):
        RUNTIME.pairing.generate_code()
        RUNTIME.logger.log(
            const.Ev.PAIRING_CODE_GENERATED, code=RUNTIME.pairing.code_display
        )
        tooltip("New pairing code generated. Existing devices must pair again.")
        self.refresh()

    def _restart_server(self):
        server.start()
        tooltip("AnkiBridge server restarted.")
        self.refresh()

    # ------------------------------------------------------------- refresh
    def refresh(self):
        snap = RUNTIME.snapshot()
        running = snap["running"]

        self.status_label.setText(
            "● Server running" if running else "○ Server stopped"
        )
        self.status_label.setStyleSheet(
            "color: #2ecc71;" if running else "color: #e74c3c;"
        )

        try:
            profile = mw.pm.name
        except Exception:
            profile = "Unknown"
        self.computer_label.setText(
            f"Computer: {netutil.computer_name()}   (Anki profile: {profile})"
        )

        if running:
            ips = netutil.all_ips()
            port = snap["bound_port"]
            addrs = "   ".join(f"http://{ip}:{port}" for ip in ips)
            self.address_label.setText(f"Address: {addrs}")
        else:
            self.address_label.setText("Address: —")

        self.code_label.setText(f"Pairing code:  {RUNTIME.pairing.code_display}")

        # Pending approval requests
        pending = RUNTIME.pairing.pending_requests()
        if pending:
            names = ", ".join(f"{p.name} ({p.remote})" for p in pending)
            self.pending_label.setText(f"⏳ Waiting for approval: {names}")
            self.pending_label.setStyleSheet("color: #f39c12;")
        else:
            self.pending_label.setText("")
            self.pending_label.setStyleSheet("")

        # Devices
        devices = RUNTIME.pairing.devices()
        if devices:
            names = ", ".join(
                f"{d.name} (seen {_fmt_time(d.last_seen)})" for d in devices
            )
            self.devices_label.setText(f"Connected devices ({len(devices)}): {names}")
        else:
            self.devices_label.setText("Connected devices: none")

        lr = snap["last_request"]
        if lr:
            self.last_request_label.setText(
                f"Last request: {lr['method']} {lr['path']} "
                f"from {lr['remote']} at {_fmt_time(lr['at'])}"
            )
        else:
            self.last_request_label.setText("Last request: —")

        le = snap["last_error"]
        if le:
            self.last_error_label.setText(
                f"Last error: {le['message']} at {_fmt_time(le['at'])}"
            )
        else:
            self.last_error_label.setText("Last error: none")

        self.log_view.setPlainText("\n".join(RUNTIME.logger.recent(200)))
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)
        global _dialog
        _dialog = None
