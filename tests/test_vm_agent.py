"""Tests for VM Agent HTTP Server and Endpoints."""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from vm.agent import VMAgentServer
from vm.config import VMConfig
from vm.opencode_manager import OpenCodeManager


class TestVMAgentServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.config_path = Path(cls.temp_dir.name) / "agent_config.json"
        cls.config = VMConfig(
            vm_id="vm-agent-test",
            vm_name="Agent-Test-VM",
            host="127.0.0.1",
            port=4191,  # dedicated test port
            reachable_ip="127.0.0.1",
            api_token="test-secret-token-12345",
            config_file_path=str(cls.config_path),
        )
        cls.config.save(cls.config_path)
        cls.manager = OpenCodeManager()
        cls.server = VMAgentServer(config=cls.config, opencode_manager=cls.manager)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.2)
        cls.base_url = f"http://127.0.0.1:{cls.config.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.server_close()
        cls.temp_dir.cleanup()

    def test_health_endpoint(self) -> None:
        req = urllib.request.Request(f"{self.base_url}/health")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["vm_id"], "vm-agent-test")
            self.assertIn("models_count", data)

    def test_models_unauthorized_and_authorized(self) -> None:
        # 1. Unauthorized
        req_no_auth = urllib.request.Request(f"{self.base_url}/models")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_no_auth, timeout=2.0)
        self.assertEqual(ctx.exception.code, 401)

        # 2. Authorized
        req_auth = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.config.api_token}"},
        )
        with urllib.request.urlopen(req_auth, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["object"], "list")
            self.assertTrue(len(data["data"]) > 0)

    def test_registration_flow_and_single_use_token(self) -> None:
        # Create token
        tok = self.config.create_registration_token(ttl_seconds=10)

        # 1. Download registration script via GET /register/<token>
        req_script = urllib.request.Request(f"{self.base_url}/register/{tok.token}")
        with urllib.request.urlopen(req_script, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
            content = resp.read().decode("utf-8")
            self.assertIn("OpenCode VM Pool - Host Registration", content)

        # 2. Verify token via POST /api/register/verify
        req_verify = urllib.request.Request(
            f"{self.base_url}/api/register/verify",
            data=json.dumps({"token": tok.token}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_verify, timeout=2.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["vm_id"], self.config.vm_id)
            self.assertEqual(data["api_token"], self.config.api_token)

        # 3. Second verification attempt must be rejected (single-use token!)
        req_verify2 = urllib.request.Request(
            f"{self.base_url}/api/register/verify",
            data=json.dumps({"token": tok.token}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_verify2, timeout=2.0)
        self.assertEqual(ctx.exception.code, 403)

    def test_chat_completions_non_streaming(self) -> None:
        payload = {
            "model": "opencode/mock-coder",
            "messages": [{"role": "user", "content": "Write a python fibonacci function"}],
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["object"], "chat.completion")
            self.assertTrue(len(data["choices"]) > 0)
            self.assertIn("OpenCode VM Pool response", data["choices"][0]["message"]["content"])

    def test_chat_completions_streaming(self) -> None:
        payload = {
            "model": "opencode/mock-coder",
            "messages": [{"role": "user", "content": "Stream test"}],
            "stream": True,
        }
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "text/event-stream")
            body = resp.read().decode("utf-8")
            self.assertIn("data:", body)
            self.assertIn("[DONE]", body)

    def test_rate_limit_detection(self) -> None:
        payload = {
            "model": "opencode/simulated_rate_limit",
            "messages": [{"role": "user", "content": "Test rate limit"}],
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_token}",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=3.0)
        self.assertEqual(ctx.exception.code, 429)
        self.assertIn("Retry-After", ctx.exception.headers)


if __name__ == "__main__":
    unittest.main()
