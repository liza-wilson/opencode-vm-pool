#!/usr/bin/env bash
# OpenCode VMware Model Pool - Host Installation Script
# Installs host dependencies, configures Host Gateway, and initializes OpenCode integration
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=========================================================="
echo " OpenCode VMware Model Pool - Host Gateway Installation"
echo "=========================================================="

# 1. Check Python
command -v python3 >/dev/null 2>&1 || { echo "Error: Python 3.11+ is required." >&2; exit 1; }
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "--> Python version: $PY_VER"

# 2. Clean existing OpenCode running processes and stale cache footprints
# DISABLED: the upstream script runs `pkill -9 -f opencode` and deletes the OpenCode
# cache/state directories. That terminates any running OpenCode session (including the
# agent performing this install) and destroys data, so it is skipped here.
echo "--> Skipping destructive OpenCode process/cache cleanup (patched out)"

# 3. Setup Host Config Directory
CONFIG_DIR="$HOME/.config/opencode-vm-pool"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR" 2>/dev/null || true

# 3. Optional package installation
if command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1; then
  echo "--> Installing recommended Python packages (FastAPI, Uvicorn, etc.)..."
  python3 -m pip install -r "$PROJECT_ROOT/requirements.txt" --user --quiet 2>/dev/null || {
    echo "Notice: Pip install skipped or packages already satisfied. Gateway built-in engine ready."
  }
fi

# 4. Initialize Database
python3 -c "
import sys
sys.path.insert(0, '$PROJECT_ROOT')
from host.registry import VMRegistry
reg = VMRegistry()
print('--> SQLite registry database initialized at: ' + str(reg.db_path))
"

# 5. Setup Host Gateway systemd user service if available
echo "--> Checking systemd user service availability..."
if command -v systemctl >/dev/null 2>&1; then
  SERVICE_DIR="$HOME/.config/systemd/user"
  mkdir -p "$SERVICE_DIR"
  SERVICE_PATH="$SERVICE_DIR/opencode-host-gateway.service"

  cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=OpenCode VM Pool Host Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
ExecStart=$(which python3) $PROJECT_ROOT/host/gateway.py --host 127.0.0.1 --port 8080
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now opencode-host-gateway.service 2>/dev/null || true
  echo "--> Host Gateway systemd user service installed at $SERVICE_PATH"
fi

echo "=========================================================="
echo " Host Gateway installation complete!"
echo " Ready to register VMs with the curl registration command."
echo "=========================================================="

