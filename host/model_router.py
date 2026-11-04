"""Model routing and failover engine.

Routes LLM requests to healthy VMs, detects rate limits (429) and quota exhaustion,
and safely fails over to compatible VMs according to configured policy.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Generator, Iterator, List, Optional, Tuple

from host.registry import ModelRecord, VMRecord, VMRegistry

logger = logging.getLogger("opencode_vm_pool.host.model_router")

DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_MAX_RETRIES = 3


class RoutingError(Exception):
    """Raised when routing fails completely."""
    def __init__(self, message: str, status_code: int = 503, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


class ModelRouter:
    """Intelligent model router with health-aware failover and rate-limit detection."""

    def __init__(
        self,
        registry: VMRegistry,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_cooldown: int = DEFAULT_COOLDOWN_SECONDS,
        allow_cross_vm_failover: bool = True,
    ) -> None:
        self.registry = registry
        self.max_retries = max_retries
        self.default_cooldown = default_cooldown
        self.allow_cross_vm_failover = allow_cross_vm_failover

    def resolve_candidates(self, requested_model_id: str) -> List[Tuple[VMRecord, str]]:
        """Resolve requested model identifier to prioritized list of (VMRecord, original_model_id) pairs."""
        candidates: List[Tuple[VMRecord, str]] = []

        # 1. Check if model ID is a specific registered unique_id (e.g. vm1__anthropic_claude-3-5-sonnet)
        model_rec = self.registry.get_model(requested_model_id)
        if model_rec:
            primary_vm = self.registry.get_vm(model_rec.vm_id)
            if primary_vm and primary_vm.is_healthy():
                candidates.append((primary_vm, model_rec.original_model_id))

            # If cross-VM failover is allowed, add other healthy VMs offering the exact same original model
            if self.allow_cross_vm_failover:
                other_vms = self.registry.find_healthy_vms_for_model(model_rec.original_model_id)
                for vm, m in other_vms:
                    if vm.vm_id != model_rec.vm_id and (vm, model_rec.original_model_id) not in candidates:
                        candidates.append((vm, model_rec.original_model_id))
            return candidates

        # 2. Check if model ID is a pooled virtual model (e.g. pool__anthropic_claude-3-5-sonnet)
        if requested_model_id.startswith("pool__"):
            clean_name = requested_model_id[len("pool__") :]
            # Search for models matching clean_name
            all_models = self.registry.list_models(only_healthy=True)
            for m in all_models:
                sanitized = m.original_model_id.replace("/", "_").replace(":", "_")
                if sanitized == clean_name or m.original_model_id == clean_name:
                    vm = self.registry.get_vm(m.vm_id)
                    if vm and vm.is_healthy():
                        pair = (vm, m.original_model_id)
                        if pair not in candidates:
                            candidates.append(pair)
            return candidates

        # 3. Direct original model lookup (e.g. anthropic/claude-3-5-sonnet)
        matches = self.registry.find_healthy_vms_for_model(requested_model_id)
        for vm, m in matches:
            candidates.append((vm, m.original_model_id))

        return candidates

    def route_chat_completion(
        self,
        payload: Dict[str, Any],
        stream: bool = False,
    ) -> Tuple[Dict[str, Any], Optional[Iterator[bytes]], Dict[str, str]]:
        """Route a chat completion request with automatic retry and failover.

        Returns:
            Tuple of (response_json, stream_generator_or_none, headers_dict)
        """
        requested_model = payload.get("model", "default")
        candidates = self.resolve_candidates(requested_model)

        if not candidates:
            # Check if models exist but are all on cooldown
            all_vms = self.registry.list_vms()
            rate_limited = [v for v in all_vms if v.is_rate_limited()]
            if rate_limited:
                cooldown_left = max(1, int(max(v.rate_limited_until for v in rate_limited) - time.time()))
                raise RoutingError(
                    f"All candidate VMs for model '{requested_model}' are currently rate-limited.",
                    status_code=429,
                    headers={"Retry-After": str(cooldown_left)},
                )
            raise RoutingError(
                f"No healthy VM available for model '{requested_model}'. Check registered VMs and models.",
                status_code=503,
            )

        attempts = 0
        last_error = ""
        last_status_code = 503
        retry_after_header = str(self.default_cooldown)
        failed_vm_ids: List[str] = []

        for vm, original_model in candidates:
            if attempts >= self.max_retries:
                logger.warning("Max retries (%d) reached for model %s", self.max_retries, requested_model)
                break

            attempts += 1
            start_time = time.time()
            target_url = f"{vm.base_url}/v1/chat/completions"

            # Prepare forwarded payload targeting the original model name expected by VM
            fwd_payload = dict(payload)
            fwd_payload["model"] = original_model
            fwd_payload["stream"] = stream
            req_data = json.dumps(fwd_payload).encode("utf-8")

            req = urllib.request.Request(
                target_url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {vm.api_token}",
                    "User-Agent": "OpenCode-VM-Pool-Router/1.0",
                },
                method="POST",
            )

            try:
                logger.info(
                    "Routing request for '%s' to VM '%s' (%s:%d) [attempt %d]",
                    requested_model,
                    vm.vm_name,
                    vm.host,
                    vm.port,
                    attempts,
                )

                # Open connection
                resp = urllib.request.urlopen(req, timeout=30.0)
                latency = (time.time() - start_time) * 1000

                if stream:
                    # Streaming response handler
                    # We wrap the response reader into a generator that verifies initial stream health
                    first_chunk = resp.readline()
                    if not first_chunk and resp.status != 200:
                        raise urllib.error.HTTPError(target_url, resp.status, "Empty stream response", resp.headers, None)  # type: ignore

                    def stream_generator() -> Iterator[bytes]:
                        try:
                            if first_chunk:
                                yield first_chunk
                            while True:
                                chunk = resp.readline()
                                if not chunk:
                                    break
                                yield chunk
                        finally:
                            resp.close()

                    self.registry.record_routing_event(
                        requested_model=requested_model,
                        routed_vm_id=vm.vm_id,
                        status="success" if not failed_vm_ids else "failover_success",
                        failover_from_vm_id=failed_vm_ids[-1] if failed_vm_ids else None,
                        latency_ms=latency,
                    )
                    return {}, stream_generator(), {"Content-Type": "text/event-stream"}

                # Non-streaming response
                resp_bytes = resp.read()
                data = json.loads(resp_bytes.decode("utf-8"))
                resp.close()

                self.registry.record_routing_event(
                    requested_model=requested_model,
                    routed_vm_id=vm.vm_id,
                    status="success" if not failed_vm_ids else "failover_success",
                    failover_from_vm_id=failed_vm_ids[-1] if failed_vm_ids else None,
                    latency_ms=latency,
                )
                return data, None, {"Content-Type": "application/json"}

            except urllib.error.HTTPError as e:
                latency = (time.time() - start_time) * 1000
                last_status_code = e.code
                last_error = f"HTTP {e.code}: {e.reason}"

                # Extract retry-after if provided
                retry_header = e.headers.get("Retry-After") if e.headers else None
                cooldown = int(retry_header) if retry_header and retry_header.isdigit() else self.default_cooldown
                retry_after_header = str(cooldown)

                if e.code == 429:
                    logger.warning("VM %s hit rate limit (429). Triggering cooldown of %ds and failover.", vm.vm_id, cooldown)
                    self.registry.record_rate_limit(vm.vm_id, cooldown_seconds=cooldown)
                    self.registry.record_routing_event(
                        requested_model=requested_model,
                        routed_vm_id=vm.vm_id,
                        status="rate_limited",
                        latency_ms=latency,
                        error_message=last_error,
                    )
                    failed_vm_ids.append(vm.vm_id)
                elif 400 <= e.code < 500:
                    # Client error (bad prompt, invalid params) - do not degrade VM or failover
                    logger.warning("Client request error (HTTP %d) on VM %s: %s", e.code, vm.vm_id, e.reason)
                    try:
                        err_body = e.read().decode("utf-8")
                        err_json = json.loads(err_body)
                        msg = err_json.get("error", {}).get("message", last_error) if isinstance(err_json, dict) else last_error
                    except Exception:
                        msg = last_error
                    raise RoutingError(msg, status_code=e.code)
                else:
                    # 5xx Server Error - degrade VM and trigger failover
                    self.registry.update_vm_status(vm.vm_id, status="degraded", error_increment=1)
                    self.registry.record_routing_event(
                        requested_model=requested_model,
                        routed_vm_id=vm.vm_id,
                        status="error",
                        latency_ms=latency,
                        error_message=last_error,
                    )
                    failed_vm_ids.append(vm.vm_id)

            except Exception as e:
                latency = (time.time() - start_time) * 1000
                last_status_code = 503
                last_error = f"Connection failed: {str(e)}"
                logger.warning("VM %s connection error: %s. Failing over.", vm.vm_id, e)
                self.registry.update_vm_status(vm.vm_id, status="degraded", error_increment=1)
                self.registry.record_routing_event(
                    requested_model=requested_model,
                    routed_vm_id=vm.vm_id,
                    status="failed",
                    latency_ms=latency,
                    error_message=last_error,
                )
                failed_vm_ids.append(vm.vm_id)

        # All attempts failed
        headers = {}
        if last_status_code == 429:
            headers["Retry-After"] = retry_after_header

        raise RoutingError(
            f"All available VMs failed for model '{requested_model}'. Last error: {last_error}",
            status_code=last_status_code,
            headers=headers,
        )

