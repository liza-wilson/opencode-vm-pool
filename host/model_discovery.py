"""Model discovery and OpenCode configuration synchronization.

Queries models from registered VMs, aggregates them, and configures the host OpenCode client.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from host.registry import ModelRecord, VMRecord, VMRegistry

logger = logging.getLogger("opencode_vm_pool.host.model_discovery")

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8080/v1"


class ModelDiscovery:
    """Discovers models from VMs and updates the host OpenCode configuration."""

    def __init__(
        self,
        registry: VMRegistry,
        gateway_base_url: str = DEFAULT_GATEWAY_URL,
        opencode_config_path: Optional[Path | str] = None,
    ) -> None:
        self.registry = registry
        self.gateway_base_url = gateway_base_url
        self.opencode_config_path = (
            Path(opencode_config_path)
            if opencode_config_path
            else self._get_default_opencode_config_path()
        )

    @staticmethod
    def _get_default_opencode_config_path() -> Path:
        env_path = os.environ.get("OPENCODE_CONFIG")
        if env_path:
            return Path(env_path)
        primary = Path.home() / ".config" / "opencode" / "opencode.json"
        try:
            primary.parent.mkdir(parents=True, exist_ok=True)
            test_f = primary.parent / ".test_write"
            test_f.touch()
            test_f.unlink()
            return primary
        except OSError:
            fallback = Path("/tmp/opencode/opencode.json")
            fallback.parent.mkdir(parents=True, exist_ok=True)
            return fallback

    def fetch_models_from_vm(self, vm: VMRecord, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Query the VM's /models endpoint using standard urllib/HTTP."""
        url = f"{vm.base_url}/models"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {vm.api_token}",
                "User-Agent": "OpenCode-VM-Pool-Host/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = data.get("data", []) or data.get("models", [])
                    return models
        except urllib.error.HTTPError as e:
            logger.warning("VM %s returned HTTP error %d: %s", vm.vm_id, e.code, e.reason)
        except Exception as e:
            logger.warning("Failed to connect to VM %s at %s: %s", vm.vm_id, url, e)
        return []

    def discover_and_sync_vm(self, vm_id: str) -> List[ModelRecord]:
        """Fetch models from a single VM and persist them in the registry."""
        vm = self.registry.get_vm(vm_id)
        if not vm:
            raise ValueError(f"VM {vm_id} not registered")

        models = self.fetch_models_from_vm(vm)
        synced = self.registry.sync_models_for_vm(vm_id, models)
        self.update_host_opencode_config()
        return synced

    def refresh_all_vms(self) -> Dict[str, int]:
        """Fetch models from all active registered VMs and refresh host configuration."""
        vms = self.registry.list_vms(include_offline=False)
        results: Dict[str, int] = {}
        for vm in vms:
            try:
                models = self.fetch_models_from_vm(vm)
                synced = self.registry.sync_models_for_vm(vm.vm_id, models)
                results[vm.vm_id] = len(synced)
            except Exception as e:
                logger.error("Error refreshing VM %s: %s", vm.vm_id, e)
                results[vm.vm_id] = 0

        self.update_host_opencode_config()
        return results

    def update_host_opencode_config(self) -> Path:
        """Update host's opencode.json with aggregated models from all healthy VMs."""
        all_models = self.registry.list_models(only_healthy=False)

        # Build model entries dictionary for OpenCode provider
        provider_models: Dict[str, Dict[str, str]] = {}
        original_model_counts: Dict[str, int] = {}

        for m in all_models:
            # Explicit model entry: VM-scoped
            provider_models[m.unique_id] = {
                "name": m.display_name,
            }
            original_model_counts[m.original_model_id] = (
                original_model_counts.get(m.original_model_id, 0) + 1
            )

        # Also add auto-failover virtual pooled models if multiple VMs provide the same model
        for orig_id, count in original_model_counts.items():
            sanitized = orig_id.replace("/", "_").replace(":", "_")
            pool_id = f"pool__{sanitized}"
            provider_models[pool_id] = {
                "name": f"Auto-Pool ({count} VMs): {orig_id}",
            }

        # Load or create opencode.json
        cfg_data: Dict[str, Any] = {}
        if self.opencode_config_path.exists():
            try:
                with open(self.opencode_config_path, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
            except Exception as e:
                logger.warning("Could not parse existing %s: %s", self.opencode_config_path, e)

        # Ensure schema
        if "$schema" not in cfg_data:
            cfg_data["$schema"] = "https://opencode.ai/config.json"

        # Update or create the 'vm-pool' provider entry
        providers = cfg_data.setdefault("provider", {})
        providers["vm-pool"] = {
            "name": "OpenCode VM Pool",
            "package": "@ai-sdk/openai-compatible",
            "settings": {
                "baseURL": self.gateway_base_url,
            },
            "models": provider_models,
        }

        # Write safely
        self.opencode_config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.opencode_config_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f, indent=2)
        tmp_path.replace(self.opencode_config_path)

        logger.info(
            "Host OpenCode configuration successfully updated with %d models at %s",
            len(provider_models),
            self.opencode_config_path,
        )
        return self.opencode_config_path
