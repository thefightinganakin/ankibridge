import importlib.util
import struct
import sys
import types
import unittest
from pathlib import Path


def _load_discovery_module():
    root = Path(__file__).resolve().parents[1]
    pkg_dir = root / "src" / "ankibridge"
    if "ankibridge" not in sys.modules:
        pkg = types.ModuleType("ankibridge")
        pkg.__path__ = [str(pkg_dir)]
        sys.modules["ankibridge"] = pkg
    spec = importlib.util.spec_from_file_location(
        "ankibridge.discovery", pkg_dir / "discovery.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ankibridge.discovery"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


discovery = _load_discovery_module()


def _build_query(msg_id, qname, qtype):
    header = struct.pack("!6H", msg_id, 0, 1, 0, 0, 0)
    question = (
        discovery._encode_name(qname) + struct.pack("!HH", qtype, discovery._CLASS_IN)
    )
    return header + question


def _parse_answer_types(packet):
    _, _, _, ancount, _, _ = struct.unpack("!6H", packet[:12])
    offset = 12
    types = []
    for _ in range(ancount):
        _, offset = discovery._decode_name(packet, offset)
        rtype, _, _, rdlen = struct.unpack("!HHIH", packet[offset: offset + 10])
        offset += 10 + rdlen
        types.append(rtype)
    return types


class DiscoveryPacketTests(unittest.TestCase):
    def setUp(self):
        self.service = {
            "service_type": "_ankibridge._tcp.local.",
            "instance": "Desk._ankibridge._tcp.local.",
            "hostname": "Desk.local.",
            "port": 48731,
            "txt": {
                "version": "0.1.0",
                "computerName": "Desk",
                "ankiProfile": "User",
            },
            "addresses": ["192.168.1.10"],
        }

    def test_encode_name_trailing_dot(self):
        encoded = discovery._encode_name("Desk.local.")
        self.assertEqual(encoded, b"\x04Desk\x05local\x00")

    def test_ptr_query_generates_full_advertisement_records(self):
        query = _build_query(0x1234, self.service["service_type"], discovery._TYPE_PTR)
        response = discovery._response_for_query(query, self.service)
        self.assertIsNotNone(response)
        msg_id, flags, _, ancount, _, _ = struct.unpack("!6H", response[:12])
        self.assertEqual(msg_id, 0x1234)
        self.assertEqual(flags, 0x8400)
        self.assertGreaterEqual(ancount, 4)
        answer_types = _parse_answer_types(response)
        self.assertIn(discovery._TYPE_PTR, answer_types)
        self.assertIn(discovery._TYPE_SRV, answer_types)
        self.assertIn(discovery._TYPE_TXT, answer_types)
        self.assertIn(discovery._TYPE_A, answer_types)

    def test_constants_use_ankibridge_name_and_mdns_service(self):
        self.assertEqual(discovery.const.ADDON_NAME, "AnkiBridge")
        self.assertEqual(discovery.const.MDNS_SERVICE_TYPE, "_ankibridge._tcp.local.")

    def test_unrelated_query_returns_none(self):
        query = _build_query(1, "_http._tcp.local.", discovery._TYPE_PTR)
        self.assertIsNone(discovery._response_for_query(query, self.service))

    def test_decode_name_compression_pointer(self):
        # Build a buffer: 12 zero bytes (simulated header) + encoded name + 2-byte pointer
        # The pointer at the end points back to offset 12 where the name starts.
        name_bytes = discovery._encode_name("example.local.")
        header = b"\x00" * 12
        # 0xC0 0x0C = pointer to offset 12 (0x0C)
        pointer = bytes([0xC0, 0x0C])
        packet = header + name_bytes + pointer
        ptr_offset = 12 + len(name_bytes)
        decoded, next_off = discovery._decode_name(packet, ptr_offset)
        self.assertEqual(decoded, "example.local.")
        # next_offset should advance past the 2-byte pointer only
        self.assertEqual(next_off, ptr_offset + 2)

    def test_decode_name_pointer_loop_raises(self):
        # A self-referential pointer: offset 0 → 0xC0 0x00 → points back to offset 0
        packet = bytes([0xC0, 0x00, 0x00, 0x00])
        with self.assertRaises(ValueError):
            discovery._decode_name(packet, 0)


if __name__ == "__main__":
    unittest.main()
