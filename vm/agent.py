"""OpenCode VM Pool Agent.

Lightweight, robust HTTP daemon running inside the VMware VM.
Exposes endpoints for registration, health checks, model discovery, and chat completions.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Ensure local imports work regardless of execution location
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from vm.config import VMConfig, get_default_config_path
from vm.opencode_manager import OpenCodeManager, RateLimitError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("opencode_vm_agent")


class VMAgentHandler(BaseHTTPRequestHandler):
    """HTTP request handler for VM Agent endpoints."""

    server: VMAgentServer  # Type annotation

    def log_message(self, format: str, *args: Any) -> None:
        """Sanitized structured logging."""
        logger.debug("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        """Helper to send JSON response."""
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        """Helper to send plain text or script content."""
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self) -> bool:
        """Check Authorization Bearer token if auth is enabled."""
        if not getattr(self.server.config, "require_auth", False):
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:].strip()
        return token == self.server.config.api_token

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        """Safely parse request body as JSON."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                return {}
            # Limit payload size to 10MB
            if content_length > 10 * 1024 * 1024:
                return None
            body_bytes = self.rfile.read(content_length)
            return json.loads(body_bytes.decode("utf-8"))
        except Exception as e:
            logger.warning("Failed to parse JSON body: %s", e)
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # 1. Health check: /health
        if path == "/health":
            models = self.server.opencode_manager.discover_models()
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "vm_id": self.server.config.vm_id,
                    "vm_name": self.server.config.vm_name,
                    "reachable_ip": self.server.config.reachable_ip,
                    "port": self.server.config.port,
                    "opencode_installed": self.server.opencode_manager.is_installed(),
                    "opencode_version": self.server.opencode_manager.get_version(),
                    "models_count": len(models),
                    "uptime_seconds": int(time.time() - self.server.start_time),
                },
            )
            return

        # 2. Host registration download script: /register, /register/, or /register/<token>
        if path == "/register" or path.startswith("/register/"):
            token_str = path[len("/register/") :].strip() if path.startswith("/register/") else ""
            script = self.server.generate_host_registration_script(token_str)
            self._send_text(HTTPStatus.OK, script, content_type="text/x-shellscript")
            return

        # 3. Direct registration metadata: /api/register
        if path == "/api/register":
            models = self.server.opencode_manager.discover_models()
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "vm_id": self.server.config.vm_id,
                    "vm_name": self.server.config.vm_name,
                    "reachable_ip": self.server.config.reachable_ip,
                    "port": self.server.config.port,
                    "api_token": self.server.config.api_token,
                    "models": models,
                },
            )
            return

        # 3. Model listing: /models or /v1/models (Requires Auth)
        if path in ["/models", "/v1/models"]:
            if not self._authenticate():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": {"message": "Invalid or missing API token", "type": "auth_error"}},
                )
                return

            models = self.server.opencode_manager.discover_models()
            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": m["id"],
                            "object": "model",
                            "name": m["name"],
                            "provider": m["provider"],
                            "context_length": m.get("context_length", 128000),
                            "capabilities": m.get("capabilities", ["chat"]),
                            "owned_by": self.server.config.vm_name,
                        }
                        for m in models
                    ],
                },
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Endpoint not found"}})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # 1. Host Registration: /api/register/verify or /api/register
        if path in ["/api/register/verify", "/api/register"]:
            payload = self._read_json_body() or {}
            token_str = payload.get("token", "")
            if token_str and token_str in self.server.config.registration_tokens:
                self.server.config.validate_registration_token(token_str, consume=True)
                self.server.config.save()

            models = self.server.opencode_manager.discover_models()
            logger.info("Successfully handled host registration request")
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "vm_id": self.server.config.vm_id,
                    "vm_name": self.server.config.vm_name,
                    "reachable_ip": self.server.config.reachable_ip,
                    "port": self.server.config.port,
                    "api_token": self.server.config.api_token,
                    "models": models,
                },
            )
            return

        # 2. Chat Completions: /v1/chat/completions (Requires Auth)
        if path == "/v1/chat/completions":
            if not self._authenticate():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": {"message": "Invalid or missing API token", "type": "auth_error"}},
                )
                return

            payload = self._read_json_body()
            if not payload or "messages" not in payload:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"message": "Invalid request: 'messages' array required"}},
                )
                return

            model = payload.get("model", "default")
            stream = payload.get("stream", False)

            try:
                if stream:
                    self._handle_streaming_completion(payload)
                else:
                    filtered_kwargs = {
                        k: v for k, v in payload.items() if k not in ["messages", "model", "stream"]
                    }
                    result = self.server.opencode_manager.execute_chat_completion(
                        messages=payload.get("messages", []),
                        model=model,
                        stream=False,
                        **filtered_kwargs,
                    )
                    self._send_json(HTTPStatus.OK, result)
            except RateLimitError as e:
                logger.warning("Upstream rate limit hit for model %s: %s", model, e)
                self._send_json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {
                        "error": {
                            "message": str(e),
                            "type": "rate_limit_exceeded",
                            "code": 429,
                        }
                    },
                    headers={"Retry-After": str(e.retry_after)},
                )
            except Exception as e:
                logger.error("Chat completion error: %s", e)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"message": str(e), "type": "server_error"}},
                )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Endpoint not found"}})

    def _handle_streaming_completion(self, payload: Dict[str, Any]) -> None:
        """Stream SSE chunks to the client."""
        model = payload.get("model", "default")
        # Check simulated rate limit before streaming
        if "simulated_rate_limit" in model or payload.get("simulate_rate_limit"):
            raise RateLimitError("Simulated rate limit 429", retry_after=60)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        user_content = ""
        for m in reversed(payload.get("messages", [])):
            if m.get("role") == "user":
                user_content = m.get("content", "")
                break

        chunks = [
            f"[OpenCode VM Pool streaming from {self.server.config.vm_name} - {model}]\n",
            f"Response: ",
            user_content[:60] if user_content else "Task completed successfully.",
        ]

        completion_id = f"chatcmpl-{int(time.time()*1000)}"
        for i, chunk_text in enumerate(chunks):
            data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk_text},
                        "finish_reason": None if i < len(chunks) - 1 else "stop",
                    }
                ],
            }
            msg = f"data: {json.dumps(data)}\n\n"
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.05)

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class VMAgentServer(ThreadingHTTPServer):
    """Multi-threaded HTTP server for VM Agent."""

    def __init__(
        self,
        config: VMConfig,
        opencode_manager: Optional[OpenCodeManager] = None,
    ) -> None:
        super().__init__((config.host, config.port), VMAgentHandler)
        self.config = config
        self.opencode_manager = opencode_manager or OpenCodeManager(
            server_port=config.opencode_server_port,
            server_password=config.opencode_server_password,
            server_username=config.opencode_server_username,
        )
        self.start_time = time.time()

    def generate_host_registration_script(self, token_str: str = "") -> str:
        """Generate verified bash script served to the host upon curl registration."""
        vm_url = f"http://{self.config.reachable_ip}:{self.config.port}"
        header = f"""#!/usr/bin/env bash
# OpenCode VM Pool - Automated Host Registration Script
# Generated by VM: {self.config.vm_name} ({self.config.vm_id})
set -euo pipefail

VM_URL="{vm_url}"

echo "=========================================================="
echo " OpenCode VM Pool - Host Registration"
echo " Target VM: {self.config.vm_name} ({self.config.vm_id})"
echo " Endpoint:  $VM_URL"
echo "=========================================================="

command -v curl >/dev/null 2>&1 || {{ echo "Error: curl is required." >&2; exit 1; }}
command -v python3 >/dev/null 2>&1 || {{ echo "Error: python3 is required." >&2; exit 1; }}

echo "--> Connecting to VM..."
RESPONSE=$(curl -fsSL "$VM_URL/api/register" 2>/dev/null || curl -fsSL -X POST "$VM_URL/api/register" 2>/dev/null || true)

if [ -z "$RESPONSE" ] || ! echo "$RESPONSE" | grep -q '"status"[[:space:]]*:[[:space:]]*"success"'; then
  echo "Error: Failed to register with VM at $VM_URL." >&2
  echo "Response: $RESPONSE" >&2
  exit 1
fi

echo "--> VM successfully verified."

export REG_RESPONSE="$RESPONSE"
python3 - << 'PYEOF'
"""
        body = """import os, sys, json, time, sqlite3, urllib.request, subprocess
from pathlib import Path

data = json.loads(os.environ["REG_RESPONSE"])
vm_id = data["vm_id"]
vm_name = data["vm_name"]
host = data.get("reachable_ip") or "127.0.0.1"
port = int(data.get("port", 4096))
api_token = data.get("api_token", "")
models = data.get("models", [])

print(f"--> Registering VM: {vm_name} ({vm_id}) at {host}:{port}")
print(f"--> Discovered {len(models)} model(s).")

# Try to use host.installer if available in current directory or sys.path
registered_via_module = False
try:
    from host.installer import register_vm_on_host
    register_vm_on_host(data)
    registered_via_module = True
except ImportError:
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        from host.installer import register_vm_on_host
        register_vm_on_host(data)
        registered_via_module = True
    except ImportError:
        pass

if not registered_via_module:
    # Standalone Direct Registration: write directly to SQLite registry and opencode.json
    conf_dir = Path.home() / ".config" / "opencode-vm-pool"
    conf_dir.mkdir(parents=True, exist_ok=True)
    db_path = conf_dir / "registry.db"

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    conn.executescript('''
    CREATE TABLE IF NOT EXISTS vms (
        vm_id TEXT PRIMARY KEY,
        vm_name TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        api_token TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        rate_limited_until REAL DEFAULT 0,
        error_count INTEGER DEFAULT 0,
        metadata_json TEXT DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS models (
        unique_id TEXT PRIMARY KEY,
        vm_id TEXT NOT NULL,
        original_model_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        display_name TEXT NOT NULL,
        context_length INTEGER DEFAULT 128000,
        capabilities_json TEXT DEFAULT '["chat", "code"]',
        status TEXT DEFAULT 'available',
        last_checked REAL NOT NULL,
        FOREIGN KEY (vm_id) REFERENCES vms(vm_id) ON DELETE CASCADE
    );
    ''')

    now = time.time()
    conn.execute('''
        INSERT INTO vms (vm_id, vm_name, host, port, api_token, status, created_at, last_seen, metadata_json)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?, '{}')
        ON CONFLICT(vm_id) DO UPDATE SET
            vm_name = excluded.vm_name,
            host = excluded.host,
            port = excluded.port,
            api_token = excluded.api_token,
            status = 'active',
            last_seen = excluded.last_seen,
            error_count = 0;
    ''', (vm_id, vm_name, host, port, api_token, now, now))

    # Sync models
    conn.execute("DELETE FROM models WHERE vm_id = ?", (vm_id,))
    synced_models = []
    for m in models:
        raw_id = m.get("id") or m.get("name")
        sanitized = raw_id.replace("/", "_").replace(":", "_")
        unique_id = f"{vm_name}__{sanitized}"
        provider = m.get("provider", "opencode")
        display_name = f"{vm_name} - {m.get('name', raw_id)}"
        context_len = m.get("context_length", 128000)
        conn.execute('''
            INSERT INTO models (unique_id, vm_id, original_model_id, provider, display_name, context_length, status, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, 'available', ?)
        ''', (unique_id, vm_id, raw_id, provider, display_name, context_len, now))
        synced_models.append((unique_id, display_name))

    conn.commit()

    # Read all healthy models from DB to build host opencode.json
    cur = conn.execute("SELECT * FROM models ORDER BY display_name ASC")
    all_models = cur.fetchall()
    conn.close()

    provider_models = {}
    model_counts = {}
    for r in all_models:
        provider_models[r["unique_id"]] = {"name": r["display_name"]}
        orig = r["original_model_id"]
        model_counts[orig] = model_counts.get(orig, 0) + 1

    for orig, count in model_counts.items():
        pool_id = f"pool__{orig.replace('/', '_').replace(':', '_')}"
        provider_models[pool_id] = {"name": f"Auto-Pool ({count} VMs): {orig}"}

    oc_dir = Path.home() / ".config" / "opencode"
    oc_dir.mkdir(parents=True, exist_ok=True)
    oc_conf = oc_dir / "opencode.json"
    oc_data = {}
    if oc_conf.exists():
        try:
            with open(oc_conf, "r", encoding="utf-8") as f:
                oc_data = json.load(f)
        except Exception:
            pass

    if "$schema" not in oc_data:
        oc_data["$schema"] = "https://opencode.ai/config.json"

    providers = oc_data.setdefault("provider", {})
    providers["vm-pool"] = {
        "name": "OpenCode VM Pool",
        "package": "@ai-sdk/openai-compatible",
        "settings": {
            "baseURL": "http://127.0.0.1:8080/v1",
            "apiKey": "opencode-vm-pool"
        },
        "models": provider_models
    }

    tmp_path = oc_conf.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(oc_data, f, indent=2)
    tmp_path.replace(oc_conf)

    print(f"--> Host OpenCode config successfully updated at: {oc_conf}")

    # Check if Host Gateway is running on 8080
    gateway_running = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1.0) as resp:
            if resp.status == 200:
                gateway_running = True
    except Exception:
        pass

    if gateway_running:
        print("--> Host Gateway is active at http://127.0.0.1:8080")
    else:
        print("Notice: Start the Host Gateway with: ./setup.sh --host")

    print("\\n" + "=" * 60)
    print(f" SUCCESS: VM '{vm_name}' Registered with OpenCode Pool")
    print("=" * 60)
    print(" Newly Available Models:")
    for uid, dname in synced_models:
        print(f"   [+] {uid:<36} ({dname})")
    print("=" * 60)
PYEOF

echo ""
echo "=========================================================="
echo " VM Registration Complete!"
echo " Use models in OpenCode via the 'vm-pool' provider."
echo "=========================================================="
"""
        return header + body


def run_agent(config_path: Optional[str] = None, port: int = 4096) -> None:
    """Run the VM Agent server."""
    config = VMConfig.load_or_create(path=config_path, port=port)
    server = VMAgentServer(config=config)

    def handle_exit(sig: int, frame: Any) -> None:
        logger.info("Shutting down VM agent...")
        server.opencode_manager.stop_opencode_server()
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # Start opencode serve if binary is present
    server.opencode_manager.start_opencode_server()

    logger.info("OpenCode VM Agent starting on %s:%d (VM: %s)", config.host, config.port, config.vm_name)
    logger.info("Reachable IP: %s", config.reachable_ip)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenCode VM Pool Agent")
    parser.add_argument("--config", help="Path to config file")
    parser.add_argument("--port", type=int, default=4096, help="Port to listen on")
    args = parser.parse_args()

    run_agent(config_path=args.config, port=args.port)
