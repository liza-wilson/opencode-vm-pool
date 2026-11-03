"""Tests for Model Routing, Rate-Limit Handling, and Failover."""

import json
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List

from host.model_router import ModelRouter, RoutingError
from host.registry import VMRegistry


class MockRouterVMHandler(BaseHTTPRequestHandler):
    """Mock VM server simulating rate limits (429) or healthy completions."""

    fail_with_429: bool = False
    fail_with_503: bool = False
    requests_received: List[Dict[str, str]] = []

    def log_message(self, format: str, *args: any) -> None:
        pass

    def do_POST(self) -> None:
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
            self.requests_received.append(payload)

            if self.fail_with_429:
                self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "10")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Rate limit exceeded"}).encode("utf-8"))
                return

            if self.fail_with_503:
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Service unavailable"}).encode("utf-8"))
                return

            # Success
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "id": "cmpl-success",
                "object": "chat.completion",
                "model": payload.get("model"),
                "choices": [{"message": {"role": "assistant", "content": f"Success from {self.server.server_port}"}}],
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()


class TestModelRouter(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        # Start two mock VM servers on ports 4193 and 4194
        cls.handler1_class = type("H1", (MockRouterVMHandler,), {"fail_with_429": False, "fail_with_503": False, "requests_received": []})
        cls.handler2_class = type("H2", (MockRouterVMHandler,), {"fail_with_429": False, "fail_with_503": False, "requests_received": []})

        cls.server1 = ThreadingHTTPServer(("127.0.0.1", 4193), cls.handler1_class)
        cls.server2 = ThreadingHTTPServer(("127.0.0.1", 4194), cls.handler2_class)

        cls.thread1 = threading.Thread(target=cls.server1.serve_forever, daemon=True)
        cls.thread2 = threading.Thread(target=cls.server2.serve_forever, daemon=True)
        cls.thread1.start()
        cls.thread2.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server1.server_close()
        cls.server2.server_close()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "router_reg.db"
        self.registry = VMRegistry(db_path=self.db_path)
        self.router = ModelRouter(registry=self.registry, max_retries=3, default_cooldown=10)

        # Register VM1 and VM2 offering the same model
        self.registry.register_or_update_vm("vm-1", "VM-1", "127.0.0.1", 4193, "tok1")
        self.registry.register_or_update_vm("vm-2", "VM-2", "127.0.0.1", 4194, "tok2")

        models = [{"id": "anthropic/claude-3-5-sonnet", "name": "Claude 3.5", "provider": "anthropic"}]
        self.registry.sync_models_for_vm("vm-1", models)
        self.registry.sync_models_for_vm("vm-2", models)

        self.handler1_class.fail_with_429 = False
        self.handler1_class.fail_with_503 = False
        self.handler1_class.requests_received = []

        self.handler2_class.fail_with_429 = False
        self.handler2_class.fail_with_503 = False
        self.handler2_class.requests_received = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_direct_routing_success(self) -> None:
        payload = {
            "model": "VM-1__anthropic_claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        resp, _, _ = self.router.route_chat_completion(payload)
        self.assertEqual(resp["object"], "chat.completion")
        self.assertIn("Success from 4193", resp["choices"][0]["message"]["content"])
        self.assertEqual(len(self.handler1_class.requests_received), 1)
        self.assertEqual(len(self.handler2_class.requests_received), 0)

    def test_automatic_failover_on_429_rate_limit(self) -> None:
        # Make VM1 return 429
        self.handler1_class.fail_with_429 = True

        payload = {
            "model": "VM-1__anthropic_claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello with failover"}],
        }
        # The router must attempt VM1, detect 429, set cooldown on VM1, and fail over to VM2!
        resp, _, _ = self.router.route_chat_completion(payload)
        self.assertEqual(resp["object"], "chat.completion")
        self.assertIn("Success from 4194", resp["choices"][0]["message"]["content"])

        # Check that VM1 was placed on cooldown
        vm1 = self.registry.get_vm("vm-1")
        self.assertTrue(vm1.is_rate_limited())

    def test_pooled_model_routing(self) -> None:
        payload = {
            "model": "pool__anthropic_claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Pool query"}],
        }
        resp, _, _ = self.router.route_chat_completion(payload)
        self.assertEqual(resp["object"], "chat.completion")
        # One of the healthy VMs handled it
        total_reqs = len(self.handler1_class.requests_received) + len(self.handler2_class.requests_received)
        self.assertEqual(total_reqs, 1)

    def test_all_vms_rate_limited_returns_429(self) -> None:
        # Both VMs fail with 429
        self.handler1_class.fail_with_429 = True
        self.handler2_class.fail_with_429 = True

        payload = {
            "model": "pool__anthropic_claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Query"}],
        }
        with self.assertRaises(RoutingError) as ctx:
            self.router.route_chat_completion(payload)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("Retry-After", ctx.exception.headers)


if __name__ == "__main__":
    unittest.main()

