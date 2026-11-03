"""VM Agent configuration module.

Handles configuration persistence, secure token generation, IP address discovery,
and token lifecycle management for OpenCode VM Pool agent.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("opencode_vm_pool.vm.config")

DEFAULT_PORT = 4096
DEFAULT_TOKEN_TTL_SECONDS = 900  # 15 minutes


def detect_reachable_ip() -> str:
    """Detect the most likely reachable IPv4 address for VMware Bridged or NAT network.

    Prioritizes non-loopback, non-virtual (docker/virbr) interfaces with a default route.
    """
    # 1. Environment variable override
    env_ip = os.environ.get("OPENCODE_VM_IP")
    if env_ip and env_ip.strip():
        return env_ip.strip()

    # 2. Try routing query to a public or gateway IP (does not actually send packets)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception as e:
        logger.debug("Socket routing check failed: %s", e)

    # 3. Try ip route get
    try:
        output = subprocess.check_output(
            ["ip", "-4", "route", "show", "default"],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
        match = re.search(r"src\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", output)
        if match:
            return match.group(1)
    except Exception as e:
        logger.debug("ip route query failed: %s", e)

    # 4. Enumerate local interfaces
    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
        for line in output.splitlines():
            # Skip loopback, docker, virbr, br-, veth
            if any(v in line for v in ["lo", "docker", "virbr", "veth", "br-"]):
                continue
            match = re.search(r"inet\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", line)
            if match:
                ip = match.group(1)
                if not ip.startswith("127.") and not ip.startswith("169.254."):
                    return ip
    except Exception as e:
        logger.debug("ip addr query failed: %s", e)

    # Fallback to hostname resolution
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception as e:
        logger.debug("Hostname resolution failed: %s", e)

    return "127.0.0.1"


@dataclasses.dataclass
class RegistrationToken:
    token: str
    created_at: float
    expires_at: float
    used: bool = False

    def is_valid(self) -> bool:
        return not self.used and time.time() < self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "used": self.used,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RegistrationToken:
        return cls(
            token=data["token"],
            created_at=data["created_at"],
            expires_at=data["expires_at"],
            used=data.get("used", False),
        )


@dataclasses.dataclass
class VMConfig:
    vm_id: str
    vm_name: str
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    reachable_ip: str = ""
    api_token: str = ""
    registration_tokens: Dict[str, RegistrationToken] = dataclasses.field(default_factory=dict)
    opencode_path: str = ""
    opencode_server_port: int = 4099
    opencode_server_password: str = ""
    opencode_server_username: str = "opencode"
    created_at: float = dataclasses.field(default_factory=time.time)
    config_file_path: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.reachable_ip:
            self.reachable_ip = detect_reachable_ip()
        if not self.api_token:
            self.api_token = secrets.token_urlsafe(32)
        if not self.opencode_server_password:
            self.opencode_server_password = secrets.token_urlsafe(24)

    def create_registration_token(self, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS) -> RegistrationToken:
        """Generate a cryptographically secure, time-limited single-use registration token."""
        token_str = secrets.token_urlsafe(32)
        now = time.time()
        reg_token = RegistrationToken(
            token=token_str,
            created_at=now,
            expires_at=now + ttl_seconds,
            used=False,
        )
        self.registration_tokens[token_str] = reg_token
        return reg_token

    def validate_registration_token(self, token_str: str, consume: bool = True) -> bool:
        """Validate a registration token. If consume=True, marks it as used immediately."""
        token = self.registration_tokens.get(token_str)
        if not token:
            return False
        if not token.is_valid():
            return False
        if consume:
            token.used = True
        return True

    def get_registration_command(self, token_str: str) -> str:
        """Return the curl registration command for host execution."""
        ip = self.reachable_ip or "127.0.0.1"
        return f'curl -fsSL "http://{ip}:{self.port}/register/{token_str}" | bash'

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vm_id": self.vm_id,
            "vm_name": self.vm_name,
            "host": self.host,
            "port": self.port,
            "reachable_ip": self.reachable_ip,
            "api_token": self.api_token,
            "registration_tokens": {
                k: v.to_dict() for k, v in self.registration_tokens.items()
            },
            "opencode_path": self.opencode_path,
            "opencode_server_port": self.opencode_server_port,
            "opencode_server_password": self.opencode_server_password,
            "opencode_server_username": self.opencode_server_username,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VMConfig:
        tokens_raw = data.get("registration_tokens", {})
        tokens = {k: RegistrationToken.from_dict(v) for k, v in tokens_raw.items()}
        return cls(
            vm_id=data["vm_id"],
            vm_name=data["vm_name"],
            host=data.get("host", "0.0.0.0"),
            port=data.get("port", DEFAULT_PORT),
            reachable_ip=data.get("reachable_ip", ""),
            api_token=data.get("api_token", ""),
            registration_tokens=tokens,
            opencode_path=data.get("opencode_path", ""),
            opencode_server_port=data.get("opencode_server_port", 4099),
            opencode_server_password=data.get("opencode_server_password", ""),
            opencode_server_username=data.get("opencode_server_username", "opencode"),
            created_at=data.get("created_at", time.time()),
        )

    def save(self, path: Optional[Path | str] = None) -> Path:
        """Save configuration with strict 0600 permissions."""
        target_path = (
            Path(path)
            if path
            else (Path(self.config_file_path) if self.config_file_path else get_default_config_path())
        )
        self.config_file_path = str(target_path)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(target_path.parent, 0o700)
            except OSError:
                pass

            data = json.dumps(self.to_dict(), indent=2)
            tmp_path = target_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(data)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            tmp_path.replace(target_path)
        except OSError as e:
            logger.warning("Could not write to %s (%s). Attempting /tmp fallback.", target_path, e)
            fallback_dir = Path("/tmp/opencode-vm-pool")
            fallback_dir.mkdir(parents=True, exist_ok=True)
            target_path = fallback_dir / "agent_config.json"
            self.config_file_path = str(target_path)
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)

        return target_path

    @classmethod
    def load_or_create(
        cls,
        path: Optional[Path | str] = None,
        vm_name: Optional[str] = None,
        port: int = DEFAULT_PORT,
    ) -> VMConfig:
        """Load an existing config or create a new one if not found."""
        target_path = Path(path) if path else get_default_config_path()
        if target_path.exists():
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                config = cls.from_dict(data)
                config.config_file_path = str(target_path)
                if not config.reachable_ip:
                    config.reachable_ip = detect_reachable_ip()
                return config
            except Exception as e:
                logger.warning("Failed to load existing config from %s: %s. Creating new.", target_path, e)

        hostname = socket.gethostname().split(".")[0] or "vm"
        resolved_name = vm_name or f"vm-{hostname}"
        vm_id = f"vm-{secrets.token_hex(4)}"
        config = cls(
            vm_id=vm_id,
            vm_name=resolved_name,
            port=port,
            reachable_ip=detect_reachable_ip(),
            config_file_path=str(target_path),
        )
        config.save(target_path)
        return config


def get_default_config_path() -> Path:
    """Return default path based on environment and user privileges with writable fallbacks."""
    env_dir = os.environ.get("OPENCODE_VM_CONFIG_DIR")
    if env_dir:
        return Path(env_dir) / "agent_config.json"

    primary = (
        Path("/etc/opencode-vm-pool/agent_config.json")
        if os.geteuid() == 0
        else Path(os.path.expanduser("~/.config/opencode-vm-pool/agent_config.json"))
    )

    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        # Test writability
        test_file = primary.parent / ".write_test"
        test_file.touch()
        test_file.unlink()
        return primary
    except OSError:
        # Fallback to /tmp if ~/.config or /etc is read-only
        fallback = Path("/tmp/opencode-vm-pool/agent_config.json")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback
