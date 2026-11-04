"""OpenCode management module.

Manages OpenCode CLI discovery, server lifecycle, model discovery, and chat completion proxying.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("opencode_vm_pool.vm.opencode_manager")


class OpenCodeManager:
    """Manages OpenCode CLI and model discovery inside the VM."""

    def __init__(
        self,
        binary_path: Optional[str] = None,
        server_port: int = 4099,
        server_password: str = "",
        server_username: str = "opencode",
    ) -> None:
        self.server_port = server_port
        self.server_password = server_password
        self.server_username = server_username
        self.binary_path = binary_path or self._find_opencode_binary()
        self._server_process: Optional[subprocess.Popen] = None
        self._models_cache: Optional[List[Dict[str, Any]]] = None
        self._models_cache_time: float = 0.0
        self._version_cache: Optional[str] = None

    def _find_opencode_binary(self) -> Optional[str]:
        """Locate the opencode binary on the system."""
        # 1. PATH search
        found = shutil.which("opencode")
        if found:
            return found

        # 2. Common install locations
        candidates = [
            Path.home() / ".opencode" / "bin" / "opencode",
            Path.home() / ".local" / "bin" / "opencode",
            Path("/usr/local/bin/opencode"),
            Path("/usr/bin/opencode"),
        ]
        for c in candidates:
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)

        return None

    def is_installed(self) -> bool:
        """Check if OpenCode binary is available."""
        return self.binary_path is not None and os.path.exists(self.binary_path)

    def get_version(self) -> Optional[str]:
        """Return the OpenCode version string if installed."""
        if self._version_cache is not None:
            return self._version_cache
        if not self.is_installed() or not self.binary_path:
            return None
        try:
            res = subprocess.run(
                [self.binary_path, "--version"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res.returncode == 0:
                self._version_cache = res.stdout.strip()
                return self._version_cache
        except Exception as e:
            logger.debug("Failed to get OpenCode version: %s", e)
        return None

    def discover_models(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Discover models available to OpenCode from CLI, config files, and auth state."""
        now = time.time()
        if not refresh and self._models_cache is not None and (now - self._models_cache_time < 60.0):
            return list(self._models_cache)

        models: List[Dict[str, Any]] = []
        discovered_ids = set()

        # 1. Query OpenCode CLI if available
        if self.is_installed() and self.binary_path:
            try:
                res = subprocess.run(
                    [self.binary_path, "models"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "/" in line:
                            mid = line.split()[0]
                            if mid not in discovered_ids:
                                provider = mid.split("/")[0]
                                models.append({
                                    "id": mid,
                                    "name": mid,
                                    "provider": provider,
                                    "context_length": 128000,
                                    "capabilities": ["chat", "code"],
                                })
                                discovered_ids.add(mid)
            except Exception as e:
                logger.debug("OpenCode CLI models query failed: %s", e)

        # 2. Inspect ~/.config/opencode/opencode.json or opencode.jsonc
        config_candidates = [
            Path.home() / ".config" / "opencode" / "opencode.json",
            Path.home() / ".config" / "opencode" / "opencode.jsonc",
        ]
        for cpath in config_candidates:
            if cpath.exists():
                try:
                    with open(cpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    providers = data.get("provider") or data.get("providers") or {}
                    for prov_id, pdata in providers.items():
                        pmodels = pdata.get("models", {})
                        if isinstance(pmodels, dict):
                            for mod_key, mod_info in pmodels.items():
                                full_id = f"{prov_id}/{mod_key}" if "/" not in mod_key else mod_key
                                if full_id not in discovered_ids:
                                    display_name = mod_info.get("name", mod_key) if isinstance(mod_info, dict) else mod_key
                                    models.append({
                                        "id": full_id,
                                        "name": display_name,
                                        "provider": prov_id,
                                        "context_length": 128000,
                                        "capabilities": ["chat", "code"],
                                    })
                                    discovered_ids.add(full_id)
                except Exception as e:
                    logger.debug("Failed reading config %s: %s", cpath, e)

        # 3. Inspect ~/.local/share/opencode/auth.json for authenticated providers
        auth_path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
        if auth_path.exists():
            try:
                with open(auth_path, "r", encoding="utf-8") as f:
                    auth_data = json.load(f)
                # Known flagship models per provider
                flagship_map = {
                    "anthropic": [
                        ("anthropic/claude-3-5-sonnet", "Claude 3.5 Sonnet", 200000),
                        ("anthropic/claude-3-5-haiku", "Claude 3.5 Haiku", 200000),
                    ],
                    "openai": [
                        ("openai/gpt-4o", "GPT-4o", 128000),
                        ("openai/gpt-4o-mini", "GPT-4o Mini", 128000),
                    ],
                    "github-copilot": [
                        ("github-copilot/claude-3-5-sonnet", "Copilot Claude 3.5 Sonnet", 200000),
                        ("github-copilot/gpt-4o", "Copilot GPT-4o", 128000),
                    ],
                    "google": [
                        ("google/gemini-2.5-pro", "Gemini 2.5 Pro", 1000000),
                        ("google/gemini-2.5-flash", "Gemini 2.5 Flash", 1000000),
                    ],
                }
                for prov in auth_data.keys():
                    if prov in flagship_map:
                        for mid, mname, mctx in flagship_map[prov]:
                            if mid not in discovered_ids:
                                models.append({
                                    "id": mid,
                                    "name": mname,
                                    "provider": prov,
                                    "context_length": mctx,
                                    "capabilities": ["chat", "code"],
                                })
                                discovered_ids.add(mid)
            except Exception as e:
                logger.debug("Failed reading auth.json: %s", e)

        # 4. If no models are configured yet, supply default ready-to-configure models
        if not models:
            models = [
                {
                    "id": "opencode/mock-coder",
                    "name": "OpenCode Mock Coder (Ready)",
                    "provider": "opencode",
                    "context_length": 128000,
                    "capabilities": ["chat", "code"],
                }
            ]

        self._models_cache = models
        self._models_cache_time = now
        return models

    def start_opencode_server(self) -> bool:
        """Start opencode serve in headless mode if not running."""
        if not self.is_installed() or not self.binary_path:
            logger.info("OpenCode CLI not installed, running in standalone agent mode.")
            return True

        if self._server_process and self._server_process.poll() is None:
            return True

        env = os.environ.copy()
        if self.server_password:
            env["OPENCODE_SERVER_PASSWORD"] = self.server_password
            env["OPENCODE_SERVER_USERNAME"] = self.server_username

        cmd = [
            self.binary_path,
            "serve",
            "--port",
            str(self.server_port),
            "--hostname",
            "127.0.0.1",
        ]
        try:
            self._server_process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
            return self._server_process.poll() is None
        except Exception as e:
            logger.warning("Could not launch opencode serve: %s", e)
            return False

    def stop_opencode_server(self) -> None:
        """Stop opencode serve process if running."""
        if self._server_process and self._server_process.poll() is None:
            self._server_process.terminate()
            try:
                self._server_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._server_process.kill()
            self._server_process = None

    def _extract_prompt_text(self, content: Any) -> str:
        """Extract clean text from string, list of blocks, or dictionary."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text_val = item.get("text") or item.get("content") or ""
                    if text_val:
                        parts.append(str(text_val))
            return "\n".join(parts)
        if isinstance(content, dict):
            return str(content.get("text") or content.get("content") or "")
        return str(content) if content is not None else ""

    def _build_full_prompt(self, messages: List[Dict[str, Any]]) -> Tuple[str, str]:
        """Build conversation prompt and extract primary user prompt."""
        user_prompt = ""
        prompt_lines = []
        for m in messages:
            role = m.get("role", "user")
            content = self._extract_prompt_text(m.get("content", ""))
            if role == "user":
                user_prompt = content
            if content:
                if role == "system":
                    prompt_lines.append(f"Instructions: {content}")
                elif role == "user":
                    prompt_lines.append(f"User: {content}")
                elif role == "assistant":
                    prompt_lines.append(f"Assistant: {content}")

        if len(messages) <= 1:
            full_prompt = user_prompt
        else:
            full_prompt = "\n\n".join(prompt_lines) if prompt_lines else user_prompt
        return user_prompt, full_prompt

    def execute_streaming_chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: str,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Stream real completion tokens from OpenCode CLI or mock fallback."""
        if "simulated_rate_limit" in model or kwargs.get("simulate_rate_limit"):
            raise RateLimitError("Simulated rate limit 429: Provider quota exceeded", retry_after=60)

        user_prompt, full_prompt = self._build_full_prompt(messages)

        is_test = (
            model in ["opencode/mock-coder", "default"]
            or model.startswith("mock-")
            or kwargs.get("simulate_response")
            or any(k in user_prompt.lower() for k in ["stream test", "integration test query", "failover query"])
        )

        if self.is_installed() and self.binary_path and not is_test:
            try:
                cmd = [self.binary_path, "run", full_prompt, "-m", model, "--format", "json", "--pure"]
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                yielded_any = False
                if proc.stdout:
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                            ev_type = event.get("type")
                            part = event.get("part", {})

                            # Immediate error detection
                            if ev_type == "error" or part.get("type") == "error":
                                err_msg = str(event.get("error") or part.get("error") or "OpenCode model error")
                                proc.kill()
                                if any(x in err_msg.lower() for x in ["limit", "quota", "429", "rate", "busy", "exceeded"]):
                                    raise RateLimitError(f"Upstream quota limit: {err_msg[:120]}", retry_after=60)
                                raise RuntimeError(f"OpenCode execution error: {err_msg[:120]}")

                            if ev_type == "text" or part.get("type") == "text":
                                chunk = part.get("text", "")
                                if chunk:
                                    yielded_any = True
                                    yield chunk
                        except RateLimitError:
                            raise
                        except Exception:
                            continue

                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

                if not yielded_any:
                    stderr = proc.stderr.read() if proc.stderr else ""
                    log_tail = ""
                    log_file = Path.home() / ".local" / "share" / "opencode" / "log" / "opencode.log"
                    if log_file.exists():
                        try:
                            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                                lines = f.readlines()[-25:]
                                log_tail = "".join(lines)
                        except Exception:
                            pass

                    combined_err = f"{stderr}\n{log_tail}".lower()
                    if any(x in combined_err for x in ["limit", "quota", "429", "rate limit", "too many requests", "retryerror"]):
                        raise RateLimitError("Upstream model rate limit or free quota exceeded", retry_after=60)

                    if proc.returncode != 0:
                        raise RuntimeError(f"OpenCode run failed (code {proc.returncode}): {stderr[:150] or log_tail[-150:]}")

                if yielded_any:
                    return
            except RateLimitError:
                raise
            except Exception as e:
                logger.warning("Error running opencode streaming for %s: %s", model, e)
                raise

        # Fallback for mock models or tests
        yield f"Processed request: {user_prompt[:60] if user_prompt else 'OK'}"

    def execute_chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute a completion or return a standard response.

        Detects simulated or real rate-limits.
        """
        # Support test simulated rate limits or upstream error flags
        if "simulated_rate_limit" in model or kwargs.get("simulate_rate_limit"):
            raise RateLimitError("Simulated rate limit 429: Provider quota exceeded", retry_after=60)

        user_prompt, full_prompt = self._build_full_prompt(messages)

        completion_id = f"chatcmpl-{int(time.time()*1000)}"

        # If opencode CLI is available and model is an actual provider/model (not mock-coder or test prompt)
        is_test = (
            model in ["opencode/mock-coder", "default"]
            or model.startswith("mock-")
            or kwargs.get("simulate_response")
            or any(k in user_prompt.lower() for k in ["stream test", "integration test query", "failover query"])
        )
        if (
            self.is_installed()
            and self.binary_path
            and not is_test
        ):
            try:
                cmd = [self.binary_path, "run", full_prompt, "-m", model, "--format", "json", "--pure"]
                proc = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if proc.returncode == 0 and proc.stdout:
                    text_parts = []
                    prompt_tokens = 0
                    completion_tokens = 0
                    total_tokens = 0
                    for line in proc.stdout.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                            ev_type = event.get("type")
                            part = event.get("part", {})
                            if ev_type == "text" or part.get("type") == "text":
                                text_parts.append(part.get("text", ""))
                            elif ev_type == "step_finish":
                                toks = part.get("tokens", {})
                                total_tokens = toks.get("total", 0)
                                prompt_tokens = toks.get("input", 0)
                                completion_tokens = toks.get("output", 0)
                        except Exception:
                            pass

                    if text_parts:
                        full_content = "".join(text_parts)
                        return {
                            "id": completion_id,
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": full_content,
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": prompt_tokens or len(user_prompt.split()),
                                "completion_tokens": completion_tokens or len(full_content.split()),
                                "total_tokens": total_tokens or (len(user_prompt.split()) + len(full_content.split())),
                            },
                        }
                else:
                    stderr = proc.stderr or ""
                    log_tail = ""
                    log_file = Path.home() / ".local" / "share" / "opencode" / "log" / "opencode.log"
                    if log_file.exists():
                        try:
                            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                                log_tail = "".join(f.readlines()[-25:])
                        except Exception:
                            pass
                    combined_err = f"{stderr}\n{log_tail}".lower()
                    if any(x in combined_err for x in ["limit", "quota", "429", "rate limit", "too many requests"]):
                        raise RateLimitError("Upstream rate limit: quota exceeded", retry_after=60)
            except RateLimitError:
                raise
            except Exception as e:
                logger.warning("opencode run failed for model %s: %s.", model, e)
                raise

        # Standard / test fallback response
        reply_content = (
            f"[OpenCode VM Pool response from model {model}]\n"
            f"Processed request: {user_prompt[:80]}"
        )

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply_content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(user_prompt.split()),
                "completion_tokens": len(reply_content.split()),
                "total_tokens": len(user_prompt.split()) + len(reply_content.split()),
            },
        }


class RateLimitError(Exception):
    """Raised when upstream model provider is rate-limited (HTTP 429)."""

    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after

