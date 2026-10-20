"""Application settings.

Every value can be overridden with an environment variable prefixed with
``SPIKE_`` (for example ``SPIKE_PORT=9000``).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("PROGRAMDATA"):
        return Path(os.environ["PROGRAMDATA"]) / "SpikeNetMonitor"
    return Path.cwd() / "data"


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SPIKE_{name}", default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(_default_data_dir()))))
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8000")))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))

    # First-run admin account. The password must be changed on first login.
    bootstrap_admin_user: str = field(default_factory=lambda: _env("BOOTSTRAP_ADMIN_USER", "admin"))
    bootstrap_admin_key: str = field(default_factory=lambda: _env("BOOTSTRAP_ADMIN_KEY", "admin@2026"))

    # Monitoring
    ping_interval: int = field(default_factory=lambda: int(_env("PING_INTERVAL", "30")))
    ping_count: int = field(default_factory=lambda: int(_env("PING_COUNT", "3")))
    ping_timeout: float = field(default_factory=lambda: float(_env("PING_TIMEOUT", "1.0")))
    ping_concurrency: int = field(default_factory=lambda: int(_env("PING_CONCURRENCY", "100")))
    raw_retention_days: int = field(default_factory=lambda: int(_env("RAW_RETENTION_DAYS", "7")))
    # Device data (MikroTik API / Ubiquiti SSH)
    stats_interval: int = field(default_factory=lambda: int(_env("STATS_INTERVAL", "60")))
    stats_concurrency: int = field(default_factory=lambda: int(_env("STATS_CONCURRENCY", "20")))
    rollup_retention_days: int = field(default_factory=lambda: int(_env("ROLLUP_RETENTION_DAYS", "365")))
    command_concurrency: int = field(default_factory=lambda: int(_env("COMMAND_CONCURRENCY", "10")))
    monitoring_enabled: bool = field(
        default_factory=lambda: _env("MONITORING_ENABLED", "1") not in ("0", "false", "no")
    )

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.data_dir / 'spike.db').as_posix()}"

    @property
    def secret_key_file(self) -> Path:
        return self.data_dir / "secret.key"


settings = Settings()
