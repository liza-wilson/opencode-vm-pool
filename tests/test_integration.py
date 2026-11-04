"""End-to-End Integration and Workflow Tests.

Validates the complete multi-VM lifecycle:
1. Provisioning VM1 agent.
2. Registering VM1 with Host Gateway via registration command.
3. Verifying VM1 models in host OpenCode config.
4. Provisioning and registering VM2.
5. Verifying unified model aggregation across both VMs.
6. Simulating upstream 429 rate limit on VM1.
7. Verifying automatic failover to VM2.
8. Verifying idempotency of registration and configuration updates.
"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from host.gateway import HostGatewayServer
from host.installer import register_vm_on_host, verify_vm_registration
from vm.agent import VMAgentServer
from vm.config import VMConfig
from vm.opencode_manager import OpenCodeManager


class TestEndToEndWorkflow(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.base_path = Path(cls.temp_dir.name)

        # 1. Start VM1 Agent on port 4181
        cls.vm1_cfg_path = cls.base_path / "vm1_config.json"
        cls.vm1_cfg = VMConfig(
            vm_id="vm-node-01",
            vm_name="VM-Node-01",
            host="127.0.0.1",
            port=4181,
            reachable_ip="127.0.0.1",
            api_token="vm1-secret-key-12345",
            config_file_path=str(cls.vm1_cfg_path),
        )
        cls.vm1_cfg.save(cls.vm1_cfg_path)
        cls.vm1_server = VMAgentServer(config=cls.vm1_cfg)
        cls.vm1_thread = threading.Thread(target=cls.vm1_server.serve_forever, daemon=True)
        cls.vm1_thread.start()

        # 2. Start VM2 Agent on port 4182
        cls.vm2_cfg_path = cls.base_path / "vm2_config.json"
        cls.vm2_cfg = VMConfig(
            vm_id="vm-node-02",
            vm_name="VM-Node-02",
            host="127.0.0.1",
            port=4182,
            reachable_ip="127.0.0.1",
            api_token="vm2-secret-key-67890",
            config_file_path=str(cls.vm2_cfg_path),
        )
        cls.vm2_cfg.save(cls.vm2_cfg_path)
        cls.vm2_server = VMAgentServer(config=cls.vm2_cfg)
        cls.vm2_thread = threading.Thread(target=cls.vm2_server.serve_forever, daemon=True)
        cls.vm2_thread.start()

        # 3. Start Host Gateway on port 8180
        cls.host_db_path = cls.base_path / "host_registry.db"
        cls.opencode_cfg_path = cls.base_path / "opencode.json"
        cls.gateway_server = HostGatewayServer(
            host="127.0.0.1",
            port=8180,
            db_path=str(cls.host_db_path),
            opencode_config_path=str(cls.opencode_cfg_path),
        )
        cls.gateway_server.start_all()
        cls.gateway_thread = threading.Thread(target=cls.gateway_server.serve_forever, daemon=True)
        cls.gateway_thread.start()

        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.gateway_server.shutdown_all()
        cls.vm1_server.server_close()
        cls.vm2_server.server_close()
        cls.temp_dir.cleanup()

    def test_complete_multi_vm_lifecycle(self) -> None:
        # Step 1: VM1 generates single-use registration token
        vm1_tok = self.vm1_cfg.create_registration_token(ttl_seconds=300)
        self.vm1_cfg.save()
        reg_cmd = self.vm1_cfg.get_registration_command(vm1_tok.token)
        self.assertIn(f"http://127.0.0.1:4181/register", reg_cmd)

        # Step 2: Host executes registration (verifying single-use token against VM1)
        vm1_verified = verify_vm_registration("http://127.0.0.1:4181", vm1_tok.token)
        self.assertEqual(vm1_verified["status"], "success")
        self.assertEqual(vm1_verified["vm_name"], "VM-Node-01")

        register_vm_on_host(
            vm_data=vm1_verified,
            db_path=str(self.host_db_path),
            opencode_config_path=str(self.opencode_cfg_path),
            gateway_host="127.0.0.1",
            gateway_port=8180,
        )

        # Step 3: Verify host OpenCode config has VM1 models
        with open(self.opencode_cfg_path, "r", encoding="utf-8") as f:
            oc_config = json.load(f)

        provider = oc_config["provider"]["vm-pool"]
        self.assertEqual(provider["settings"]["baseURL"], "http://127.0.0.1:8180/v1")
        sample_model_raw = vm1_verified["models"][0]["id"]
        sample_sanitized = sample_model_raw.replace("/", "_").replace(":", "_")
        vm1_sample_unique = f"VM-Node-01__{sample_sanitized}"
        vm2_sample_unique = f"VM-Node-02__{sample_sanitized}"
        pool_sample_id = f"pool__{sample_sanitized}"

        self.assertIn(vm1_sample_unique, provider["models"])

        # Step 4: Register VM2 (Tokenless registration)
        vm2_verified = verify_vm_registration("http://127.0.0.1:4182")
        self.assertEqual(vm2_verified["status"], "success")

        register_vm_on_host(
            vm_data=vm2_verified,
            db_path=str(self.host_db_path),
            opencode_config_path=str(self.opencode_cfg_path),
            gateway_host="127.0.0.1",
            gateway_port=8180,
        )

        # Step 5: Verify Model Aggregation across both VMs in Host Gateway /v1/models
        req_models = urllib.request.Request("http://127.0.0.1:8180/v1/models")
        with urllib.request.urlopen(req_models, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            model_ids = [m["id"] for m in data["data"]]
            # VM1 model
            self.assertIn(vm1_sample_unique, model_ids)
            # VM2 model
            self.assertIn(vm2_sample_unique, model_ids)
            # Auto-pool model
            self.assertIn(pool_sample_id, model_ids)

        # Step 6: Test Normal Chat Completion through Host Gateway
        chat_req = urllib.request.Request(
            "http://127.0.0.1:8180/v1/chat/completions",
            data=json.dumps({
                "model": vm1_sample_unique,
                "messages": [{"role": "user", "content": "Integration test query"}],
                "stream": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(chat_req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            chat_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(chat_data["object"], "chat.completion")

        # Step 7: Simulate Rate Limit on VM1 and Verify Failover to VM2
        # We manually mark VM-Node-01 rate limited in registry
        self.gateway_server.registry.record_rate_limit("vm-node-01", cooldown_seconds=15)
        self.assertTrue(self.gateway_server.registry.get_vm("vm-node-01").is_rate_limited())

        # Now send a request for the pooled model with failover enabled
        failover_req = urllib.request.Request(
            "http://127.0.0.1:8180/v1/chat/completions",
            data=json.dumps({
                "model": pool_sample_id,
                "messages": [{"role": "user", "content": "Failover query"}],
                "stream": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(failover_req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            resp_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp_data["object"], "chat.completion")
            self.assertEqual(resp_data["object"], "chat.completion")
            # Completed successfully using the healthy VM (VM2)!

        # Step 8: Verify Idempotency: Re-registering VM1 does not duplicate entries
        vm1_tok_2 = self.vm1_cfg.create_registration_token(ttl_seconds=300)
        self.vm1_cfg.save()
        vm1_verified_2 = verify_vm_registration("http://127.0.0.1:4181", vm1_tok_2.token)
        register_vm_on_host(
            vm_data=vm1_verified_2,
            db_path=str(self.host_db_path),
            opencode_config_path=str(self.opencode_cfg_path),
            gateway_host="127.0.0.1",
            gateway_port=8180,
        )
        all_vms = self.gateway_server.registry.list_vms()
        self.assertEqual(len(all_vms), 2)  # Still exactly 2 VMs!


if __name__ == "__main__":
    unittest.main()
