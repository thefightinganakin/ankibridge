"""Best-effort pure-Python Bonjour/mDNS advertising.

Advertises one service instance and answers PTR/SRV/TXT/A queries over
224.0.0.251:5353 with no third-party dependency.
"""

import socket
import struct
import threading

from . import const, netutil
from .runtime import RUNTIME

_MCAST_GROUP = "224.0.0.251"
_MCAST_PORT = 5353
_TYPE_A = 1
_TYPE_PTR = 12
_TYPE_TXT = 16
_TYPE_SRV = 33
_TYPE_ANY = 255
_CLASS_IN = 1
_DEFAULT_TTL = 120

_sock = None
_thread = None
_stop_event = None
_service = None


def start(port):
    """Advertise the AnkiBridge service via mDNS. Safe to call repeatedly."""
    if not RUNTIME.config.get("enable_mdns"):
        return False

    stop()

    try:
        service = _build_service(int(port))
        bind_ip = _bind_ip()
        sock = _make_mdns_socket(bind_ip)
    except Exception as exc:  # noqa: BLE001 - discovery is never fatal
        RUNTIME.logger.exception("DISCOVERY_ERROR", message=str(exc))
        return False

    global _sock, _thread, _stop_event, _service
    _sock = sock
    _service = service
    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=_serve_queries, name="AnkiBridgeMDNS", daemon=True
    )
    _thread.start()

    try:
        _announce(ttl=_DEFAULT_TTL)
    except Exception:
        pass
    RUNTIME.logger.log("DISCOVERY_START", service=const.MDNS_SERVICE_TYPE, port=port)
    return True


def stop():
    global _sock, _thread, _stop_event, _service
    if _sock is not None and _service is not None:
        try:
            _announce(ttl=0)
        except Exception:
            pass
    if _stop_event is not None:
        _stop_event.set()
    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            pass
    if _thread is not None:
        _thread.join(timeout=0.2)
    if _sock is not None or _thread is not None:
        RUNTIME.logger.log("DISCOVERY_STOP")
    _sock = None
    _thread = None
    _stop_event = None
    _service = None


def _build_service(port):
    computer = netutil.computer_name()
    safe_name = computer.replace(".", "-").replace(" ", "-") or "Anki"
    hostname = f"{safe_name}.local."
    instance = f"{safe_name}.{const.MDNS_SERVICE_TYPE}"
    addresses = []
    for ip in netutil.all_ips():
        try:
            socket.inet_aton(ip)
            addresses.append(ip)
        except Exception:
            continue
    if not addresses:
        addresses = ["127.0.0.1"]
    return {
        "service_type": const.MDNS_SERVICE_TYPE,
        "instance": instance,
        "hostname": hostname,
        "port": int(port),
        "txt": {
            "version": const.VERSION,
            "computerName": safe_name,
            "ankiProfile": _profile_slug(),
        },
        "addresses": addresses,
    }


def _bind_ip():
    bind = str(RUNTIME.config.get("bind_address", "") or "").strip()
    if not bind or bind == "0.0.0.0":
        bind = netutil.primary_ip()
    if not bind or bind == "0.0.0.0":
        bind = "127.0.0.1"
    return bind


def _make_mdns_socket(bind_ip):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except Exception:
        pass
    # Bind to INADDR_ANY so the socket receives multicast packets delivered to
    # 224.0.0.251:5353 on any interface (binding to a unicast address prevents
    # receiving multicast traffic on most OSes).
    sock.bind(("", _MCAST_PORT))
    membership = socket.inet_aton(_MCAST_GROUP) + socket.inet_aton(bind_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.settimeout(0.5)
    return sock


def _want_unicast_response(packet):
    """Return True if any question in packet has the QU bit set (RFC 6762 §5.4)."""
    if len(packet) < 12:
        return False
    try:
        _, _, qdcount, _, _, _ = struct.unpack("!6H", packet[:12])
        offset = 12
        for _ in range(qdcount):
            _, offset = _decode_name(packet, offset)
            if offset + 4 > len(packet):
                return False
            _, qclass = struct.unpack("!HH", packet[offset: offset + 4])
            offset += 4
            if qclass & 0x8000:
                return True
    except Exception:
        pass
    return False


def _serve_queries():
    while _stop_event is not None and not _stop_event.is_set():
        try:
            data, addr = _sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        except Exception:
            continue
        try:
            response = _response_for_query(data, _service)
            if response is None:
                continue
            # RFC 6762 §6: send unicast only when the QU bit is set in QCLASS;
            # all other queries get a multicast response to 224.0.0.251:5353.
            if _want_unicast_response(data):
                _sock.sendto(response, addr)
            else:
                _sock.sendto(response, (_MCAST_GROUP, _MCAST_PORT))
        except Exception:
            continue


def _response_for_query(packet, service):
    if len(packet) < 12:
        return None
    msg_id, _, qdcount, _, _, _ = struct.unpack("!6H", packet[:12])
    if qdcount <= 0:
        return None

    questions, offset = _parse_questions(packet, 12, qdcount)
    wants_ptr = False
    wants_srv = False
    wants_txt = False
    wants_a = False

    for qname, qtype, _ in questions:
        qname = qname.lower()
        if qname == service["service_type"].lower() and qtype in (_TYPE_PTR, _TYPE_ANY):
            wants_ptr = True
        elif qname == service["instance"].lower() and qtype in (_TYPE_SRV, _TYPE_TXT, _TYPE_ANY):
            if qtype in (_TYPE_SRV, _TYPE_ANY):
                wants_srv = True
            if qtype in (_TYPE_TXT, _TYPE_ANY):
                wants_txt = True
        elif qname == service["hostname"].lower() and qtype in (_TYPE_A, _TYPE_ANY):
            wants_a = True

    if not any((wants_ptr, wants_srv, wants_txt, wants_a)):
        return None

    answers = []
    if wants_ptr:
        answers.append(_rr_ptr(service["service_type"], service["instance"], _DEFAULT_TTL))
    if wants_srv:
        answers.append(_rr_srv(service["instance"], service["hostname"], service["port"], _DEFAULT_TTL))
    if wants_txt:
        answers.append(_rr_txt(service["instance"], service["txt"], _DEFAULT_TTL))
    if wants_a:
        for ip in service["addresses"]:
            answers.append(_rr_a(service["hostname"], ip, _DEFAULT_TTL))

    if wants_ptr and not wants_srv:
        answers.append(_rr_srv(service["instance"], service["hostname"], service["port"], _DEFAULT_TTL))
    if wants_ptr and not wants_txt:
        answers.append(_rr_txt(service["instance"], service["txt"], _DEFAULT_TTL))
    if wants_ptr and not wants_a:
        for ip in service["addresses"]:
            answers.append(_rr_a(service["hostname"], ip, _DEFAULT_TTL))

    header = struct.pack("!6H", msg_id, 0x8400, 0, len(answers), 0, 0)
    return header + b"".join(answers)


def _parse_questions(data, offset, qdcount):
    questions = []
    for _ in range(qdcount):
        name, offset = _decode_name(data, offset)
        if offset + 4 > len(data):
            raise ValueError("truncated question")
        qtype, qclass = struct.unpack("!HH", data[offset: offset + 4])
        offset += 4
        questions.append((name, qtype, qclass & 0x7FFF))
    return questions, offset


def _decode_name(data, offset):
    labels = []
    jumped = False
    next_offset = offset
    max_jumps = len(data)
    jump_count = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated name")
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                next_offset = offset
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("bad compression pointer")
            jump_count += 1
            if jump_count > max_jumps:
                raise ValueError("compression pointer loop detected")
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                next_offset = offset + 2
            offset = ptr
            jumped = True
            continue
        offset += 1
        end = offset + length
        if end > len(data):
            raise ValueError("truncated label")
        labels.append(data[offset:end].decode("utf-8", "replace"))
        offset = end
        if not jumped:
            next_offset = offset
    return ".".join(labels) + ".", next_offset


def _encode_name(name):
    clean = (name or "").rstrip(".")
    if not clean:
        return b"\x00"
    out = bytearray()
    for label in clean.split("."):
        raw = label.encode("utf-8")[:63]  # DNS label limit: 63 bytes per RFC 1035
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def _rr(name, rtype, rdata, ttl):
    return (
        _encode_name(name)
        + struct.pack("!HHIH", rtype, _CLASS_IN, int(ttl), len(rdata))
        + rdata
    )


def _rr_ptr(name, target, ttl):
    return _rr(name, _TYPE_PTR, _encode_name(target), ttl)


def _rr_srv(name, host, port, ttl):
    rdata = struct.pack("!HHH", 0, 0, int(port)) + _encode_name(host)
    return _rr(name, _TYPE_SRV, rdata, ttl)


def _rr_txt(name, txt, ttl):
    parts = []
    for key, value in txt.items():
        entry = f"{key}={value}".encode("utf-8")[:255]  # DNS TXT string limit: 255 bytes
        parts.append(bytes([len(entry)]) + entry)
    return _rr(name, _TYPE_TXT, b"".join(parts), ttl)


def _rr_a(name, ip, ttl):
    return _rr(name, _TYPE_A, socket.inet_aton(ip), ttl)


def _announce(ttl):
    if _sock is None or _service is None:
        return
    records = [
        _rr_ptr(_service["service_type"], _service["instance"], ttl),
        _rr_srv(_service["instance"], _service["hostname"], _service["port"], ttl),
        _rr_txt(_service["instance"], _service["txt"], ttl),
    ]
    for ip in _service["addresses"]:
        records.append(_rr_a(_service["hostname"], ip, ttl))
    header = struct.pack("!6H", 0, 0x8400, 0, len(records), 0, 0)
    packet = header + b"".join(records)
    _sock.sendto(packet, (_MCAST_GROUP, _MCAST_PORT))


def _profile_slug():
    try:
        from aqt import mw

        return (mw.pm.name or "User").replace(" ", "-")
    except Exception:
        return "User"
