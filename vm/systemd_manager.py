"""Linux systemd integration manager.

Handles creation, installation, activation, and lifecycle of the OpenCode VM agent systemd service.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("opencode_vm_pool.vm.systemd_manager")

SERVICE_NAME = "opencode-vm-agent.service"


class SystemdManager:
    """Manages systemd unit installation and service lifecycle."""

    def __init__(
        self,
        service_name: str = SERVICE_NAME,
        is_user_service: Optional[bool] = None,
    ) -> None:
        self.service_name = service_name
        self.is_user_service = (
            is_user_service if is_user_service is not None else (os.geteuid() != 0)
        )
        self.systemctl_path = shutil.which("systemctl")

    @property
    def unit_path(self) -> Path:
        """Return target path for the unit file."""
        if self.is_user_service:
            return Path.home() / ".config" / "systemd" / "user" / self.service_name
        return Path("/etc/systemd/system") / self.service_name

    def is_systemd_available(self) -> bool:
        """Check whether systemd is present and running."""
        if not self.systemctl_path:
            return False
        try:
            res = subprocess.run(
                [self.systemctl_path, "is-system-running"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            # 'running', 'degraded', or 'maintenance' indicate systemd is active
            state = res.stdout.strip()
            return state in ["running", "degraded", "maintenance"]
        except Exception:
            return False

    def generate_unit_content(
        self,
        python_exec: str,
        agent_script_path: str,
        config_path: str,
        working_dir: Optional[str] = None,
    ) -> str:
        """Generate hardened systemd unit file content."""
        work_dir = working_dir or str(Path(agent_script_path).parent.resolve())
        return f"""[Unit]
Description=OpenCode VM Pool Agent
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={work_dir}
ExecStart={python_exec} {agent_script_path} --config {config_path}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

# Hardening
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""

    def install_unit(
        self,
        python_exec: str,
        agent_script_path: str,
        config_path: str,
        working_dir: Optional[str] = None,
    ) -> bool:
        """Write unit file to systemd directory and set permissions."""
        content = self.generate_unit_content(
            python_exec=python_exec,
            agent_script_path=agent_script_path,
            config_path=config_path,
            working_dir=working_dir,
        )
        target = self.unit_path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(target, 0o644)
            self._run_systemctl(["daemon-reload"])
            logger.info("Installed systemd unit at %s", target)
            return True
        except Exception as e:
            logger.error("Failed to install systemd unit at %s: %s", target, e)
            return False

    def enable_and_start(self) -> bool:
        """Enable service on boot and start it immediately."""
        ok_enable = self._run_systemctl(["enable", self.service_name])
        ok_start = self._run_systemctl(["start", self.service_name])
        return ok_enable and ok_start

    def stop(self) -> bool:
        """Stop running service."""
        return self._run_systemctl(["stop", self.service_name])

    def restart(self) -> bool:
        """Restart service."""
        return self._run_systemctl(["restart", self.service_name])

    def is_active(self) -> bool:
        """Check if service is active and running."""
        cmd = self._systemctl_cmd(["is-active", self.service_name])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return res.stdout.strip() == "active"
        except Exception:
            return False

    def uninstall(self) -> bool:
        """Stop, disable, and remove unit file."""
        self.stop()
        self._run_systemctl(["disable", self.service_name])
        if self.unit_path.exists():
            try:
                self.unit_path.unlink()
                self._run_systemctl(["daemon-reload"])
                return True
            except Exception as e:
                logger.error("Failed to remove unit file: %s", e)
                return False
        return True

    def _systemctl_cmd(self, args: list[str]) -> list[str]:
        base = [self.systemctl_path or "systemctl"]
        if self.is_user_service:
            base.append("--user")
        return base + args

    def _run_systemctl(self, args: list[str]) -> bool:
        if not self.systemctl_path:
            return False
        cmd = self._systemctl_cmd(args)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return res.returncode == 0
        except Exception as e:
            logger.debug("systemctl %s failed: %s", args, e)
            return False

