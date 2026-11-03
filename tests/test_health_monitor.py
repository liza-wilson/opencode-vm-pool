"""Tests for Health Monitoring Subsystem."""

import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

from host.health_monitor import HealthMonitor
from host.registry import VMRegistry


class MockHealthHandler(BaseHTTPRequestHandler):
    is_healthy: bool = True

    def log_message(self, format: str, *args: any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/health":
            if self.is_healthy:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()


class TestHealthMonitor(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.handler = type("MockH", (MockHealthHandler,), {"is_healthy": True})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 4195), cls.handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.server_close()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "health_reg.db"
        self.registry = VMRegistry(db_path=self.db_path)
        self.mock_discovery = MagicMock()
        self.monitor = HealthMonitor(
            registry=self.registry,
            interval_seconds=1.0,
            model_discovery=self.mock_discovery,
        )
        self.handler.is_healthy = True

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_healthy_cycle_and_cooldown_clearance(self) -> None:
        vm = self.registry.register_or_update_vm("vm-h", "Health-VM", "127.0.0.1", 4195, "tok")
        # Place on an expired cooldown
        self.registry.record_rate_limit("vm-h", cooldown_seconds=-1)

        results = self.monitor.run_health_check_cycle()
        self.assertEqual(results.get("vm-h"), "healthy")

        updated = self.registry.get_vm("vm-h")
        self.assertEqual(updated.status, "active")
        self.assertFalse(updated.is_rate_limited())

    def test_offline_transition_and_recovery(self) -> None:
        self.registry.register_or_update_vm("vm-rec", "Recovery-VM", "127.0.0.1", 4195, "tok")

        # Make server fail
        self.handler.is_healthy = False

        # Run 3 cycles -> status must transition to offline
        self.monitor.run_health_check_cycle()  # error 1 (degraded)
        self.monitor.run_health_check_cycle()  # error 2 (degraded)
        self.monitor.run_health_check_cycle()  # error 3 (offline)

        vm = self.registry.get_vm("vm-rec")
        self.assertEqual(vm.status, "offline")

        # Now recover server
        self.handler.is_healthy = True
        self.monitor.run_health_check_cycle()

        vm_recovered = self.registry.get_vm("vm-rec")
        self.assertEqual(vm_recovered.status, "active")
        self.assertEqual(vm_recovered.error_count, 0)

        # Resync models should have been triggered
        self.mock_discovery.discover_and_sync_vm.assert_called_with("vm-rec")


if __name__ == "__main__":
    unittest.main()

