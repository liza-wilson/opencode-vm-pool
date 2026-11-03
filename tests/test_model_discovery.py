"""Tests for Model Discovery and OpenCode Configuration Sync."""

import json
import tempfile
import threading
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from host.model_discovery import ModelDiscovery
from host.registry import VMRegistry


class MockVMHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/models":
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer valid-token":
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.end_headers()
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {
                "data": [
                    {"id": "anthropic/claude-3-5-sonnet", "name": "Claude 3.5 Sonnet", "provider": "anthropic"},
                    {"id": "openai/gpt-4o", "name": "GPT-4o", "provider": "openai"},
                ]
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()


class TestModelDiscovery(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.mock_server = ThreadingHTTPServer(("127.0.0.1", 4192), MockVMHandler)
        cls.mock_thread = threading.Thread(target=cls.mock_server.serve_forever, daemon=True)
        cls.mock_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mock_server.server_close()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "reg.db"
        self.oc_config = Path(self.temp_dir.name) / "opencode.json"
        self.registry = VMRegistry(db_path=self.db_path)
        self.discovery = ModelDiscovery(
            registry=self.registry,
            gateway_base_url="http://127.0.0.1:8080/v1",
            opencode_config_path=self.oc_config,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_fetch_and_sync_models_from_vm(self) -> None:
        vm = self.registry.register_or_update_vm(
            vm_id="vm-mock",
            vm_name="Mock-VM",
            host="127.0.0.1",
            port=4192,
            api_token="valid-token",
        )

        synced = self.discovery.discover_and_sync_vm("vm-mock")
        self.assertEqual(len(synced), 2)

        # Check opencode.json was created
        self.assertTrue(self.oc_config.exists())
        with open(self.oc_config, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        self.assertIn("provider", cfg)
        self.assertIn("vm-pool", cfg["provider"])
        pool_prov = cfg["provider"]["vm-pool"]
        self.assertEqual(pool_prov["package"], "@ai-sdk/openai-compatible")
        self.assertEqual(pool_prov["settings"]["baseURL"], "http://127.0.0.1:8080/v1")

        # Models in config
        models = pool_prov["models"]
        self.assertIn("Mock-VM__anthropic_claude-3-5-sonnet", models)
        self.assertIn("Mock-VM__openai_gpt-4o", models)

    def test_preserves_existing_user_providers_in_opencode_json(self) -> None:
        # Pre-seed opencode.json with user's personal provider
        pre_existing = {
            "$schema": "https://opencode.ai/config.json",
            "model": "personal/custom-model",
            "provider": {
                "personal-anthropic": {
                    "settings": {"apiKey": "user-secret-key"}
                }
            }
        }
        with open(self.oc_config, "w", encoding="utf-8") as f:
            json.dump(pre_existing, f, indent=2)

        # Sync a VM
        self.registry.register_or_update_vm(
            vm_id="vm-mock-2",
            vm_name="Mock-2",
            host="127.0.0.1",
            port=4192,
            api_token="valid-token",
        )
        self.discovery.discover_and_sync_vm("vm-mock-2")

        with open(self.oc_config, "r", encoding="utf-8") as f:
            updated = json.load(f)

        # Personal provider and model must still be intact!
        self.assertIn("personal-anthropic", updated["provider"])
        self.assertEqual(updated["model"], "personal/custom-model")
        # And vm-pool provider must be added alongside it
        self.assertIn("vm-pool", updated["provider"])


if __name__ == "__main__":
    unittest.main()

