import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _load_pairing_module():
    root = Path(__file__).resolve().parents[1]
    pkg_dir = root / "src" / "ankibridge"
    if "ankibridge" not in sys.modules:
        pkg = types.ModuleType("ankibridge")
        pkg.__path__ = [str(pkg_dir)]
        sys.modules["ankibridge"] = pkg
    spec = importlib.util.spec_from_file_location(
        "ankibridge.pairing", pkg_dir / "pairing.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ankibridge.pairing"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pairing = _load_pairing_module()


class PairingPersistenceTests(unittest.TestCase):
    def test_pairing_state_survives_manager_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "pairing_state.json"
            mgr = pairing.PairingManager(str(state_path))
            code = mgr.ensure_code()
            token = mgr.pair(code, "Victor's iPhone")

            self.assertIsNotNone(token)

            restarted = pairing.PairingManager(str(state_path))
            self.assertEqual(restarted.code, code)
            self.assertEqual(restarted.device_count(), 1)
            device = restarted.validate(token)
            self.assertIsNotNone(device)
            self.assertEqual(device.name, "Victor's iPhone")

    def test_regenerating_code_clears_persisted_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "pairing_state.json"
            mgr = pairing.PairingManager(str(state_path))
            code = mgr.ensure_code()
            token = mgr.pair(code, "iPad")

            self.assertIsNotNone(token)

            mgr.generate_code()

            restarted = pairing.PairingManager(str(state_path))
            self.assertEqual(restarted.device_count(), 0)
            self.assertIsNone(restarted.validate(token))

    def test_persistence_failures_do_not_break_in_memory_pairing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "pairing_state.json"
            mgr = pairing.PairingManager(str(state_path))

            with mock.patch("ankibridge.pairing.os.replace", side_effect=OSError("disk full")):
                code = mgr.ensure_code()
                token = mgr.pair(code, "Victor's iPhone")

            self.assertEqual(len(code), 6)
            self.assertIsNotNone(token)
            device = mgr.validate(token)
            self.assertIsNotNone(device)
            self.assertEqual(device.name, "Victor's iPhone")
            self.assertFalse((state_path.parent / "pairing_state.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
