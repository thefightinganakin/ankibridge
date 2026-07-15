"""Best-effort helpers for computer name and LAN IP detection."""

import socket
import subprocess
import sys


def computer_name():
    """A human-friendly name for this computer."""
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["scutil", "--get", "ComputerName"], timeout=1.0
            )
            name = out.decode("utf-8", "replace").strip()
            if name:
                return name
        except Exception:
            pass
    name = socket.gethostname()
    if name.endswith(".local"):
        name = name[: -len(".local")]
    return name or "This computer"


def primary_ip():
    """The LAN IP most likely reachable from other devices on the network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are actually sent; this just picks the outbound interface.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


def all_ips():
    """All plausible IPv4 LAN addresses, primary first."""
    primary = primary_ip()
    ips = []
    if not primary.startswith("127."):
        ips.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips or [primary]
