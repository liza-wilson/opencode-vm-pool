"""Persistent SQLite registry for OpenCode VM Pool.

Maintains registered VMs, model mappings, health status, and routing metrics.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterator

logger = logging.getLogger("opencode_vm_pool.host.registry")


@dataclasses.dataclass
class VMRecord:
    vm_id: str
    vm_name: str
    host: str
    port: int
    api_token: str
    status: str = "active"  # 'active', 'degraded', 'offline'
    created_at: float = 0.0
    last_seen: float = 0.0
    rate_limited_until: float = 0.0
    error_count: int = 0
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_rate_limited(self) -> bool:
        return time.time() < self.rate_limited_until

    def is_healthy(self) -> bool:
        return self.status != "offline" and not self.is_rate_limited()


@dataclasses.dataclass
class ModelRecord:
    unique_id: str
    vm_id: str
    original_model_id: str
    provider: str
    display_name: str
    context_length: int = 128000
    capabilities: List[str] = dataclasses.field(default_factory=lambda: ["chat", "code"])
    status: str = "available"
    last_checked: float = 0.0


class VMRegistry:
    """Thread-safe SQLite registry for virtual machines and model mappings."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        if db_path is None:
            env_dir = os.environ.get("OPENCODE_VM_POOL_DATA_DIR")
            if env_dir:
                config_dir = Path(env_dir)
            else:
                config_dir = Path.home() / ".config" / "opencode-vm-pool"
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
                test_f = config_dir / ".test_write"
                test_f.touch()
                test_f.unlink()
            except OSError:
                config_dir = Path("/tmp/opencode-vm-pool")
                config_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = config_dir / "registry.db"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            conn.executescript("""
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

            CREATE INDEX IF NOT EXISTS idx_models_original_id ON models(original_model_id);
            CREATE INDEX IF NOT EXISTS idx_models_vm_id ON models(vm_id);

            CREATE TABLE IF NOT EXISTS routing_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                requested_model TEXT NOT NULL,
                routed_vm_id TEXT NOT NULL,
                failover_from_vm_id TEXT,
                status TEXT NOT NULL,
                latency_ms REAL DEFAULT 0,
                error_message TEXT
            );
            """)

    def register_or_update_vm(
        self,
        vm_id: str,
        vm_name: str,
        host: str,
        port: int,
        api_token: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> VMRecord:
        """Register a new VM or update an existing registration (idempotent)."""
        now = time.time()
        meta_json = json.dumps(metadata or {})
        with self._get_connection() as conn:
            # Check existing
            cur = conn.execute("SELECT created_at FROM vms WHERE vm_id = ?", (vm_id,))
            row = cur.fetchone()
            created_at = row["created_at"] if row else now

            conn.execute(
                """
                INSERT INTO vms (vm_id, vm_name, host, port, api_token, status, created_at, last_seen, metadata_json)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(vm_id) DO UPDATE SET
                    vm_name = excluded.vm_name,
                    host = excluded.host,
                    port = excluded.port,
                    api_token = excluded.api_token,
                    status = 'active',
                    last_seen = excluded.last_seen,
                    error_count = 0,
                    metadata_json = excluded.metadata_json;
                """,
                (vm_id, vm_name, host, port, api_token, created_at, now, meta_json),
            )
            conn.commit()

        logger.info("Registered/updated VM in registry: %s (%s) at %s:%d", vm_name, vm_id, host, port)
        return self.get_vm(vm_id)  # type: ignore

    def unregister_vm(self, vm_id: str) -> bool:
        """Remove a VM and all its associated models."""
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM vms WHERE vm_id = ?", (vm_id,))
            conn.commit()
            removed = cur.rowcount > 0
        if removed:
            logger.info("Unregistered VM: %s", vm_id)
        return removed

    def get_vm(self, vm_id: str) -> Optional[VMRecord]:
        """Fetch VM record by ID."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM vms WHERE vm_id = ?", (vm_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_vm(row)

    def list_vms(self, include_offline: bool = True) -> List[VMRecord]:
        """List registered VMs."""
        with self._get_connection() as conn:
            if include_offline:
                cur = conn.execute("SELECT * FROM vms ORDER BY vm_name ASC")
            else:
                cur = conn.execute("SELECT * FROM vms WHERE status != 'offline' ORDER BY vm_name ASC")
            return [self._row_to_vm(r) for r in cur.fetchall()]

    def update_vm_status(
        self,
        vm_id: str,
        status: str,
        error_increment: int = 0,
        rate_limited_until: Optional[float] = None,
    ) -> None:
        """Update VM health status, error count, and last seen."""
        now = time.time()
        with self._get_connection() as conn:
            if rate_limited_until is not None:
                conn.execute(
                    """
                    UPDATE vms SET
                        status = ?,
                        last_seen = ?,
                        error_count = error_count + ?,
                        rate_limited_until = ?
                    WHERE vm_id = ?
                    """,
                    (status, now, error_increment, rate_limited_until, vm_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE vms SET
                        status = ?,
                        last_seen = ?,
                        error_count = error_count + ?
                    WHERE vm_id = ?
                    """,
                    (status, now, error_increment, vm_id),
                )
            conn.commit()

    def record_rate_limit(self, vm_id: str, cooldown_seconds: int = 60) -> None:
        """Mark VM as rate-limited until cooldown expires."""
        until = time.time() + cooldown_seconds
        self.update_vm_status(vm_id, status="degraded", error_increment=1, rate_limited_until=until)
        logger.warning("VM %s placed on rate-limit cooldown for %d seconds", vm_id, cooldown_seconds)

    def sync_models_for_vm(self, vm_id: str, models: List[Dict[str, Any]]) -> List[ModelRecord]:
        """Synchronize the model list discovered from a VM, generating unique model IDs."""
        vm = self.get_vm(vm_id)
        if not vm:
            raise ValueError(f"VM {vm_id} not found in registry")

        now = time.time()
        synced_records: List[ModelRecord] = []

        with self._get_connection() as conn:
            # Delete stale models for this VM
            conn.execute("DELETE FROM models WHERE vm_id = ?", (vm_id,))

            for m in models:
                raw_id = m.get("id") or m.get("name")
                # Format unique identifier: vm_name__sanitized_model_id
                sanitized = raw_id.replace("/", "_").replace(":", "_")
                unique_id = f"{vm.vm_name}__{sanitized}"
                provider = m.get("provider", "opencode")
                display_name = f"{vm.vm_name} - {m.get('name', raw_id)}"
                context_length = m.get("context_length", 128000)
                capabilities = json.dumps(m.get("capabilities", ["chat", "code"]))

                conn.execute(
                    """
                    INSERT INTO models (
                        unique_id, vm_id, original_model_id, provider,
                        display_name, context_length, capabilities_json, status, last_checked
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'available', ?)
                    """,
                    (unique_id, vm_id, raw_id, provider, display_name, context_length, capabilities, now),
                )
                synced_records.append(
                    ModelRecord(
                        unique_id=unique_id,
                        vm_id=vm_id,
                        original_model_id=raw_id,
                        provider=provider,
                        display_name=display_name,
                        context_length=context_length,
                        capabilities=m.get("capabilities", ["chat", "code"]),
                        last_checked=now,
                    )
                )
            conn.commit()

        logger.info("Synchronized %d models for VM %s (%s)", len(synced_records), vm.vm_name, vm_id)
        return synced_records

    def list_models(self, only_healthy: bool = True) -> List[ModelRecord]:
        """List all discovered models across VMs."""
        with self._get_connection() as conn:
            if only_healthy:
                now = time.time()
                cur = conn.execute(
                    """
                    SELECT m.* FROM models m
                    JOIN vms v ON m.vm_id = v.vm_id
                    WHERE v.status != 'offline' AND v.rate_limited_until <= ?
                    ORDER BY m.display_name ASC
                    """,
                    (now,),
                )
            else:
                cur = conn.execute("SELECT * FROM models ORDER BY display_name ASC")
            return [self._row_to_model(r) for r in cur.fetchall()]

    def get_model(self, unique_id: str) -> Optional[ModelRecord]:
        """Get model by its unique pool ID."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM models WHERE unique_id = ?", (unique_id,))
            row = cur.fetchone()
            return self._row_to_model(row) if row else None

    def find_healthy_vms_for_model(self, original_model_id: str) -> List[Tuple[VMRecord, ModelRecord]]:
        """Find healthy VMs offering a specific underlying model, ordered by least errors and recent activity."""
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT v.*, m.* FROM models m
                JOIN vms v ON m.vm_id = v.vm_id
                WHERE (m.original_model_id = ? OR m.unique_id = ?)
                  AND v.status != 'offline'
                  AND v.rate_limited_until <= ?
                ORDER BY v.error_count ASC, v.last_seen DESC
                """,
                (original_model_id, original_model_id, now),
            )
            results = []
            for row in cur.fetchall():
                vm = self._row_to_vm(row)
                model = self._row_to_model(row)
                results.append((vm, model))
            return results

    def record_routing_event(
        self,
        requested_model: str,
        routed_vm_id: str,
        status: str,
        failover_from_vm_id: Optional[str] = None,
        latency_ms: float = 0.0,
        error_message: Optional[str] = None,
    ) -> None:
        """Record telemetry and failover history."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO routing_events (
                    timestamp, requested_model, routed_vm_id, failover_from_vm_id,
                    status, latency_ms, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (time.time(), requested_model, routed_vm_id, failover_from_vm_id, status, latency_ms, error_message),
            )
            conn.commit()

    @staticmethod
    def _row_to_vm(row: sqlite3.Row) -> VMRecord:
        meta = {}
        try:
            meta = json.loads(row["metadata_json"])
        except Exception:
            pass
        return VMRecord(
            vm_id=row["vm_id"],
            vm_name=row["vm_name"],
            host=row["host"],
            port=row["port"],
            api_token=row["api_token"],
            status=row["status"],
            created_at=row["created_at"],
            last_seen=row["last_seen"],
            rate_limited_until=row["rate_limited_until"],
            error_count=row["error_count"],
            metadata=meta,
        )

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> ModelRecord:
        caps = ["chat", "code"]
        try:
            caps = json.loads(row["capabilities_json"])
        except Exception:
            pass
        return ModelRecord(
            unique_id=row["unique_id"],
            vm_id=row["vm_id"],
            original_model_id=row["original_model_id"],
            provider=row["provider"],
            display_name=row["display_name"],
            context_length=row["context_length"],
            capabilities=caps,
            status=row["status"],
            last_checked=row["last_checked"],
        )
