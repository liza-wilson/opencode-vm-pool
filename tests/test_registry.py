"""Tests for Host SQLite Registry."""

import tempfile
import time
import unittest
from pathlib import Path

from host.registry import ModelRecord, VMRecord, VMRegistry


class TestVMRegistry(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_registry.db"
        self.registry = VMRegistry(db_path=self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_register_and_get_vm(self) -> None:
        vm = self.registry.register_or_update_vm(
            vm_id="vm-01",
            vm_name="VM-Alpha",
            host="192.168.1.10",
            port=4096,
            api_token="alpha-token-secret",
            metadata={"env": "prod"},
        )
        self.assertEqual(vm.vm_id, "vm-01")
        self.assertEqual(vm.vm_name, "VM-Alpha")
        self.assertEqual(vm.base_url, "http://192.168.1.10:4096")
        self.assertTrue(vm.is_healthy())

        fetched = self.registry.get_vm("vm-01")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.vm_name, "VM-Alpha")
        self.assertEqual(fetched.metadata.get("env"), "prod")

    def test_duplicate_vm_registration_is_idempotent(self) -> None:
        # Initial registration
        self.registry.register_or_update_vm(
            vm_id="vm-dup",
            vm_name="Dup-VM",
            host="192.168.1.20",
            port=4096,
            api_token="token-v1",
        )
        # Duplicate registration with updated IP/port/token
        updated = self.registry.register_or_update_vm(
            vm_id="vm-dup",
            vm_name="Dup-VM-Updated",
            host="192.168.1.25",
            port=4097,
            api_token="token-v2",
        )
        self.assertEqual(updated.vm_name, "Dup-VM-Updated")
        self.assertEqual(updated.port, 4097)
        self.assertEqual(updated.api_token, "token-v2")

        vms = self.registry.list_vms()
        self.assertEqual(len(vms), 1)

    def test_model_sync_and_unique_id_generation(self) -> None:
        self.registry.register_or_update_vm(
            vm_id="vm-models",
            vm_name="Model-VM",
            host="192.168.1.30",
            port=4096,
            api_token="tok",
        )
        models_data = [
            {
                "id": "anthropic/claude-3-5-sonnet",
                "name": "Claude 3.5 Sonnet",
                "provider": "anthropic",
                "context_length": 200000,
            },
            {
                "id": "openai/gpt-4o",
                "name": "GPT-4o",
                "provider": "openai",
                "context_length": 128000,
            },
        ]
        synced = self.registry.sync_models_for_vm("vm-models", models_data)
        self.assertEqual(len(synced), 2)

        # Unique ID check: must start with vm_name
        self.assertEqual(synced[0].unique_id, "Model-VM__anthropic_claude-3-5-sonnet")
        self.assertEqual(synced[1].unique_id, "Model-VM__openai_gpt-4o")

        # Query all models
        all_models = self.registry.list_models()
        self.assertEqual(len(all_models), 2)

    def test_model_id_conflicts_handling(self) -> None:
        # Register two different VMs that offer the SAME model (e.g. claude-3-5-sonnet)
        self.registry.register_or_update_vm("vm-1", "VM-One", "192.168.1.101", 4096, "tok1")
        self.registry.register_or_update_vm("vm-2", "VM-Two", "192.168.1.102", 4096, "tok2")

        m = [{"id": "anthropic/claude-3-5-sonnet", "name": "Claude 3.5", "provider": "anthropic"}]
        self.registry.sync_models_for_vm("vm-1", m)
        self.registry.sync_models_for_vm("vm-2", m)

        # Both models must coexist in the registry with unique IDs
        m1 = self.registry.get_model("VM-One__anthropic_claude-3-5-sonnet")
        m2 = self.registry.get_model("VM-Two__anthropic_claude-3-5-sonnet")
        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)
        self.assertNotEqual(m1.unique_id, m2.unique_id)
        self.assertEqual(m1.original_model_id, m2.original_model_id)

        # Finding healthy VMs for the model should return BOTH
        healthy_candidates = self.registry.find_healthy_vms_for_model("anthropic/claude-3-5-sonnet")
        self.assertEqual(len(healthy_candidates), 2)

    def test_rate_limit_and_cooldown(self) -> None:
        vm = self.registry.register_or_update_vm("vm-rl", "RL-VM", "192.168.1.50", 4096, "tok")
        self.assertFalse(vm.is_rate_limited())

        # Put on 5-second cooldown
        self.registry.record_rate_limit("vm-rl", cooldown_seconds=5)
        updated = self.registry.get_vm("vm-rl")
        self.assertTrue(updated.is_rate_limited())
        self.assertFalse(updated.is_healthy())

        # Check that find_healthy_vms_for_model excludes it during cooldown
        m = [{"id": "claude", "name": "Claude", "provider": "anthropic"}]
        self.registry.sync_models_for_vm("vm-rl", m)
        candidates = self.registry.find_healthy_vms_for_model("claude")
        self.assertEqual(len(candidates), 0)

    def test_unregister_vm_cascades(self) -> None:
        self.registry.register_or_update_vm("vm-del", "Del-VM", "192.168.1.60", 4096, "tok")
        self.registry.sync_models_for_vm("vm-del", [{"id": "m1", "name": "M1", "provider": "p"}])
        self.assertEqual(len(self.registry.list_models()), 1)

        # Unregister VM
        self.assertTrue(self.registry.unregister_vm("vm-del"))
        self.assertIsNone(self.registry.get_vm("vm-del"))
        self.assertEqual(len(self.registry.list_models()), 0)


if __name__ == "__main__":
    unittest.main()

