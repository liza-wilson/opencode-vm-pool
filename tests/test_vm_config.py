"""Tests for VM Config and Token Lifecycle."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from vm.config import RegistrationToken, VMConfig, detect_reachable_ip


class TestVMConfig(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "agent_config.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_config_creation_and_defaults(self) -> None:
        cfg = VMConfig(vm_id="vm-test-1", vm_name="Test-VM")
        self.assertEqual(cfg.vm_id, "vm-test-1")
        self.assertEqual(cfg.vm_name, "Test-VM")
        self.assertTrue(len(cfg.api_token) > 20)
        self.assertTrue(len(cfg.opencode_server_password) > 10)
        self.assertIsNotNone(cfg.reachable_ip)

    def test_config_save_load_permissions(self) -> None:
        cfg = VMConfig(vm_id="vm-perm-test", vm_name="Perm-VM", port=4097)
        saved_path = cfg.save(self.config_path)
        self.assertTrue(saved_path.exists())

        # Check permissions (0600)
        stat = os.stat(saved_path)
        mode = stat.st_mode & 0o777
        self.assertEqual(mode, 0o600)

        # Load back
        loaded = VMConfig.load_or_create(self.config_path)
        self.assertEqual(loaded.vm_id, "vm-perm-test")
        self.assertEqual(loaded.port, 4097)
        self.assertEqual(loaded.api_token, cfg.api_token)

    def test_token_expiration_and_single_use(self) -> None:
        cfg = VMConfig(vm_id="vm-tok-test", vm_name="Tok-VM")

        # 1. Valid token
        tok = cfg.create_registration_token(ttl_seconds=2)
        self.assertTrue(tok.is_valid())
        self.assertIn(tok.token, cfg.registration_tokens)

        # Validate without consume
        self.assertTrue(cfg.validate_registration_token(tok.token, consume=False))
        self.assertFalse(tok.used)

        # Validate with consume (single-use)
        self.assertTrue(cfg.validate_registration_token(tok.token, consume=True))
        self.assertTrue(tok.used)

        # Second attempt must fail
        self.assertFalse(cfg.validate_registration_token(tok.token, consume=True))

        # 2. Expired token
        tok_exp = cfg.create_registration_token(ttl_seconds=-1)
        self.assertFalse(tok_exp.is_valid())
        self.assertFalse(cfg.validate_registration_token(tok_exp.token, consume=True))

    def test_detect_reachable_ip(self) -> None:
        ip = detect_reachable_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(len(ip) >= 7)  # At least '0.0.0.0' or valid IP


if __name__ == "__main__":
    unittest.main()

