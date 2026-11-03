"""OpenCode Host Gateway Server.

Exposes an OpenAI-compatible API to the host OpenCode client, aggregates models across
all registered VMs, and routes completion requests with automatic health checks and failover.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import parse_qs, urlparse

# Ensure local imports work regardless of execution location
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from host.health_monitor import HealthMonitor
from host.model_discovery import ModelDiscovery
from host.model_router import ModelRouter, RoutingError
from host.registry import VMRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("opencode_host_gateway")


class GatewayHTTPHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for Host Gateway."""

    server: HostGatewayServer  # Type annotation

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                return {}
            if content_length > 20 * 1024 * 1024:  # 20MB max
                return None
            body_bytes = self.rfile.read(content_length)
            return json.loads(body_bytes.decode("utf-8"))
        except Exception as e:
            logger.warning("Failed to parse request JSON: %s", e)
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # 1. Health endpoint: /health
        if path == "/health":
            vms = self.server.registry.list_vms()
            models = self.server.registry.list_models(only_healthy=True)
            healthy_vms = [v for v in vms if v.is_healthy()]
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "opencode-vm-pool-host-gateway",
                    "vms_total": len(vms),
                    "vms_healthy": len(healthy_vms),
                    "models_count": len(models),
                    "uptime_seconds": int(time.time() - self.server.start_time),
                },
            )
            return

        # 2. OpenAI-compatible models listing: /v1/models or /models
        if path in ["/v1/models", "/models"]:
            models = self.server.registry.list_models(only_healthy=False)
            model_items = []
            seen_originals = set()

            for m in models:
                model_items.append({
                    "id": m.unique_id,
                    "object": "model",
                    "created": int(m.last_checked),
                    "owned_by": m.provider,
                    "display_name": m.display_name,
                    "context_length": m.context_length,
                })
                # Add virtual pool entry for easy selection
                if m.original_model_id not in seen_originals:
                    seen_originals.add(m.original_model_id)
                    sanitized = m.original_model_id.replace("/", "_").replace(":", "_")
                    model_items.append({
                        "id": f"pool__{sanitized}",
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "pool",
                        "display_name": f"Auto-Pool: {m.original_model_id}",
                        "context_length": m.context_length,
                    })

            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": model_items,
                },
            )
            return

        # 3. Management: List VMs: /api/vms
        if path == "/api/vms":
            vms = self.server.registry.list_vms()
            self._send_json(
                HTTPStatus.OK,
                {
                    "vms": [
                        {
                            "vm_id": v.vm_id,
                            "vm_name": v.vm_name,
                            "host": v.host,
                            "port": v.port,
                            "status": v.status,
                            "is_rate_limited": v.is_rate_limited(),
                            "rate_limited_until": v.rate_limited_until,
                            "error_count": v.error_count,
                            "last_seen": v.last_seen,
                        }
                        for v in vms
                    ]
                },
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Endpoint not found"}})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # 1. OpenAI-compatible chat completions: /v1/chat/completions
        if path == "/v1/chat/completions":
            payload = self._read_json_body()
            if not payload or "messages" not in payload:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"message": "Invalid request: 'messages' array required"}},
                )
                return

            stream = bool(payload.get("stream", False))

            try:
                resp_data, stream_gen, headers = self.server.router.route_chat_completion(
                    payload=payload,
                    stream=stream,
                )

                if stream and stream_gen:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    try:
                        for chunk in stream_gen:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        logger.debug("Client disconnected during stream")
                    return

                # Non-streaming response
                self._send_json(HTTPStatus.OK, resp_data, headers=headers)

            except RoutingError as e:
                self._send_json(
                    e.status_code,
                    {"error": {"message": str(e), "type": "routing_error", "code": e.status_code}},
                    headers=e.headers,
                )
            except Exception as e:
                logger.error("Unhandled error during completion routing: %s", e)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"message": f"Internal gateway error: {e}", "type": "server_error"}},
                )
            return

        # 2. Management: Register VM: /api/vms/register
        if path == "/api/vms/register":
            payload = self._read_json_body()
            if not payload or not all(k in payload for k in ["vm_id", "vm_name", "host", "port", "api_token"]):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"message": "Missing required fields (vm_id, vm_name, host, port, api_token)"}},
                )
                return

            vm = self.server.registry.register_or_update_vm(
                vm_id=payload["vm_id"],
                vm_name=payload["vm_name"],
                host=payload["host"],
                port=int(payload["port"]),
                api_token=payload["api_token"],
                metadata=payload.get("metadata", {}),
            )

            # Synchronize models immediately
            synced_models = self.server.model_discovery.discover_and_sync_vm(vm.vm_id)

            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "vm": {
                        "vm_id": vm.vm_id,
                        "vm_name": vm.vm_name,
                        "host": vm.host,
                        "port": vm.port,
                    },
                    "synced_models_count": len(synced_models),
                },
            )
            return

        # 3. Management: Refresh VM models: /api/vms/{vm_id}/refresh
        if path.startswith("/api/vms/") and path.endswith("/refresh"):
            vm_id = path[len("/api/vms/") : -len("/refresh")]
            try:
                synced = self.server.model_discovery.discover_and_sync_vm(vm_id)
                self._send_json(HTTPStatus.OK, {"status": "success", "models_count": len(synced)})
            except Exception as e:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": str(e)}})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Endpoint not found"}})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Management: Unregister VM: /api/vms/{vm_id}
        if path.startswith("/api/vms/"):
            vm_id = path[len("/api/vms/") :]
            removed = self.server.registry.unregister_vm(vm_id)
            if removed:
                self.server.model_discovery.update_host_opencode_config()
                self._send_json(HTTPStatus.OK, {"status": "success", "removed_vm_id": vm_id})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": f"VM '{vm_id}' not found"}})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Endpoint not found"}})


class HostGatewayServer(ThreadingHTTPServer):
    """Multi-threaded Host Gateway Server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        db_path: Optional[str] = None,
        opencode_config_path: Optional[str] = None,
    ) -> None:
        super().__init__((host, port), GatewayHTTPHandler)
        self.gateway_host = host
        self.gateway_port = port
        self.registry = VMRegistry(db_path=db_path)
        self.model_discovery = ModelDiscovery(
            registry=self.registry,
            gateway_base_url=f"http://{host}:{port}/v1",
            opencode_config_path=opencode_config_path,
        )
        self.router = ModelRouter(registry=self.registry)
        self.health_monitor = HealthMonitor(
            registry=self.registry,
            interval_seconds=30.0,
            model_discovery=self.model_discovery,
        )
        self.start_time = time.time()

    def start_all(self) -> None:
        """Start background health monitor and sync initial state."""
        self.health_monitor.start()
        logger.info("Host Gateway running on http://%s:%d", self.gateway_host, self.gateway_port)
        logger.info("Models available at http://%s:%d/v1/models", self.gateway_host, self.gateway_port)

    def shutdown_all(self) -> None:
        """Gracefully stop background tasks and close server."""
        logger.info("Shutting down Host Gateway...")
        self.health_monitor.stop()
        self.server_close()


def run_gateway(
    host: str = "127.0.0.1",
    port: int = 8080,
    db_path: Optional[str] = None,
    opencode_config_path: Optional[str] = None,
) -> None:
    server = HostGatewayServer(
        host=host,
        port=port,
        db_path=db_path,
        opencode_config_path=opencode_config_path,
    )

    def handle_exit(sig: int, frame: Any) -> None:
        server.shutdown_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    server.start_all()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown_all()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenCode Host Gateway")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--db", help="Path to SQLite registry database")
    parser.add_argument("--opencode-config", help="Path to opencode.json")
    args = parser.parse_args()

    run_gateway(
        host=args.host,
        port=args.port,
        db_path=args.db,
        opencode_config_path=args.opencode_config,
    )

