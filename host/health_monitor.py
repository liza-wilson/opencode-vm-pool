"""Health monitoring subsystem for OpenCode VM Pool.

Periodically verifies VM availability, manages recovery, clears rate-limit cooldowns,
and updates model availability status.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from host.registry import VMRecord, VMRegistry

logger = logging.getLogger("opencode_vm_pool.host.health_monitor")

DEFAULT_CHECK_INTERVAL = 30.0
HEALTH_TIMEOUT = 10.0
MAX_CONSECUTIVE_ERRORS = 3


class HealthMonitor:
    """Monitors health of registered VMs and automatically updates registry state."""

    def __init__(
        self,
        registry: VMRegistry,
        interval_seconds: float = DEFAULT_CHECK_INTERVAL,
        model_discovery: Optional[Any] = None,
    ) -> None:
        self.registry = registry
        self.interval_seconds = interval_seconds
        self.model_discovery = model_discovery
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def check_vm_health(self, vm: VMRecord) -> bool:
        """Ping VM /health endpoint."""
        url = f"{vm.base_url}/health"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "OpenCode-VM-Pool-HealthCheck/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("status") == "ok"
        except Exception as e:
            logger.debug("Health check failed for VM %s at %s: %s", vm.vm_id, url, e)
        return False

    def run_health_check_cycle(self) -> Dict[str, str]:
        """Perform one complete health check sweep across all registered VMs."""
        vms = self.registry.list_vms(include_offline=True)
        results: Dict[str, str] = {}
        now = time.time()

        for vm in vms:
            healthy = self.check_vm_health(vm)
            was_offline = vm.status == "offline"

            if healthy:
                # Check rate-limit cooldown
                cooldown_passed = vm.rate_limited_until > 0 and now >= vm.rate_limited_until
                is_still_rate_limited = vm.rate_limited_until > 0 and now < vm.rate_limited_until

                new_status = "degraded" if is_still_rate_limited else "active"
                self.registry.update_vm_status(
                    vm_id=vm.vm_id,
                    status=new_status,
                    error_increment=-vm.error_count if not is_still_rate_limited else 0,
                    rate_limited_until=0.0 if cooldown_passed else None,
                )
                results[vm.vm_id] = "healthy"

                # If VM recovered from offline status, trigger model sync
                if was_offline and self.model_discovery:
                    logger.info("VM %s reconnected. Resyncing models...", vm.vm_id)
                    try:
                        self.model_discovery.discover_and_sync_vm(vm.vm_id)
                    except Exception as e:
                        logger.warning("Failed to resync models for %s: %s", vm.vm_id, e)

            else:
                new_errors = vm.error_count + 1
                new_status = "offline" if new_errors >= MAX_CONSECUTIVE_ERRORS else "degraded"
                self.registry.update_vm_status(
                    vm_id=vm.vm_id,
                    status=new_status,
                    error_increment=1,
                )
                results[vm.vm_id] = new_status
                logger.warning(
                    "VM %s health check failed (errors: %d, status: %s)",
                    vm.vm_id,
                    new_errors,
                    new_status,
                )

        return results

    def start(self) -> None:
        """Start the background monitoring thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="HealthMonitorThread")
        self._thread.start()
        logger.info("Health monitor background thread started (interval: %.1fs)", self.interval_seconds)

    def stop(self) -> None:
        """Signal monitor to stop and wait for completion."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Health monitor background thread stopped.")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_health_check_cycle()
            except Exception as e:
                logger.error("Error in health monitor sweep: %s", e)
            self._stop_event.wait(self.interval_seconds)

