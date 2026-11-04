"""Host Installer and Automated VM Registration.

Handles one-command VM registration, Host Gateway setup, OpenCode config synchronization,
and model connectivity verification.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure local imports work
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from host.model_discovery import ModelDiscovery
from host.registry import VMRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("opencode_host_installer")


def verify_vm_registration(vm_url: str, token: str = "", timeout: float = 10.0) -> Dict[str, Any]:
    """Connect to VM /api/register or /api/register/verify to fetch registration details."""
    clean_url = vm_url.rstrip("/")
    # 1. Try tokenless direct registration GET /api/register
    try:
        get_req = urllib.request.Request(
            f"{clean_url}/api/register",
            headers={"User-Agent": "OpenCode-Host-Installer/1.0"},
        )
        with urllib.request.urlopen(get_req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass

    # 2. Try POST /api/register/verify (or /api/register)
    verify_url = f"{clean_url}/api/register/verify"
    payload = json.dumps({"token": token}).encode("utf-8")
    req = urllib.request.Request(
        verify_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "OpenCode-Host-Installer/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Failed to connect to VM at {clean_url}: {e}")


def ensure_host_gateway_running(
    gateway_host: str = "127.0.0.1",
    gateway_port: int = 8080,
    db_path: Optional[str] = None,
) -> bool:
    """Check if host gateway is running. If not, start it in the background."""
    health_url = f"http://{gateway_host}:{gateway_port}/health"
    try:
        req = urllib.request.Request(health_url)
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    # Start gateway in background
    logger.info("Host Gateway is not running. Starting background process...")
    gateway_script = Path(__file__).parent / "gateway.py"
    cmd = [
        sys.executable,
        str(gateway_script),
        "--host",
        gateway_host,
        "--port",
        str(gateway_port),
    ]
    if db_path:
        cmd.extend(["--db", db_path])

    try:
        # Launch background process detached
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait up to 3 seconds for gateway to bind
        for _ in range(30):
            time.sleep(0.1)
            try:
                req = urllib.request.Request(health_url)
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    if resp.status == 200:
                        logger.info("Host Gateway successfully started (PID: %d)", proc.pid)
                        return True
            except Exception:
                pass
    except Exception as e:
        logger.warning("Could not auto-start Host Gateway: %s", e)

    return False


def register_vm_on_host(
    vm_data: Dict[str, Any],
    db_path: Optional[str] = None,
    opencode_config_path: Optional[str] = None,
    gateway_host: str = "127.0.0.1",
    gateway_port: int = 8080,
) -> Dict[str, Any]:
    """Execute host-side registration workflow."""
    registry = VMRegistry(db_path=db_path)
    discovery = ModelDiscovery(
        registry=registry,
        gateway_base_url=f"http://{gateway_host}:{gateway_port}/v1",
        opencode_config_path=opencode_config_path,
    )

    vm_id = vm_data["vm_id"]
    vm_name = vm_data["vm_name"]
    host = vm_data.get("reachable_ip") or vm_data.get("host", "127.0.0.1")
    port = int(vm_data.get("port", 4096))
    api_token = vm_data["api_token"]

    # 1. Register or update VM in persistent SQLite registry
    vm_rec = registry.register_or_update_vm(
        vm_id=vm_id,
        vm_name=vm_name,
        host=host,
        port=port,
        api_token=api_token,
        metadata={"registered_at": time.time()},
    )

    # 2. Sync models
    models_raw = vm_data.get("models")
    if not models_raw:
        models_raw = discovery.fetch_models_from_vm(vm_rec)
    synced_models = registry.sync_models_for_vm(vm_id, models_raw)

    # 3. Update host OpenCode configuration
    cfg_file = discovery.update_host_opencode_config()

    # 4. Ensure Host Gateway is running
    ensure_host_gateway_running(
        gateway_host=gateway_host,
        gateway_port=gateway_port,
        db_path=db_path,
    )

    # 5. Format and display summary
    print("\n" + "=" * 62)
    print(f" SUCCESS: VM '{vm_name}' Registered with OpenCode Pool")
    print("=" * 62)
    print(f" VM ID:         {vm_id}")
    print(f" Endpoint:      http://{host}:{port}")
    print(f" OpenCode Conf: {cfg_file}")
    print(f" Gateway URL:   http://{gateway_host}:{gateway_port}/v1")
    print("\n Newly Available Models:")
    for m in synced_models:
        print(f"   [+] {m.unique_id:<36} ({m.display_name})")

    print("\n All Pool Models (Total across all VMs):")
    all_models = registry.list_models(only_healthy=True)
    for m in all_models:
        print(f"   * {m.unique_id:<36} [VM: {m.vm_id}]")

    print("=" * 62 + "\n")

    return {
        "status": "success",
        "vm_id": vm_id,
        "vm_name": vm_name,
        "models_count": len(synced_models),
        "total_models": len(all_models),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Host VM Registration")
    parser.add_argument("--vm-url", required=True, help="Reachable URL of VM (e.g. http://192.168.1.150:4096)")
    parser.add_argument("--token", default="", help="Registration token (optional)")
    parser.add_argument("--gateway-host", default="127.0.0.1", help="Gateway host interface")
    parser.add_argument("--gateway-port", type=int, default=8080, help="Gateway port")
    parser.add_argument("--db", help="Path to SQLite database")
    parser.add_argument("--opencode-config", help="Path to opencode.json")
    args = parser.parse_args()

    print(f"--> Connecting to VM at {args.vm_url}...")
    try:
        vm_data = verify_vm_registration(args.vm_url, args.token)
        register_vm_on_host(
            vm_data=vm_data,
            db_path=args.db,
            opencode_config_path=args.opencode_config,
            gateway_host=args.gateway_host,
            gateway_port=args.gateway_port,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

