import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _parse_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc


def _parse_csv(name):
    value = os.environ.get(name, "")
    return [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]


def _resolve_path(name, default):
    configured = os.environ.get(name)
    path = Path(configured).expanduser() if configured else Path(default)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


@dataclass(frozen=True)
class Settings:
    environment: str
    data_dir: Path
    database_path: Path
    backup_dir: Path
    security_config_path: Path
    https_only: bool
    allowed_hosts: tuple[str, ...]
    session_max_age_seconds: int
    enable_auto_backup: bool
    recycle_bin_retention_days: int
    max_backup_upload_bytes: int

    @property
    def is_production(self):
        return self.environment == "production"


def load_settings():
    environment = os.environ.get(
        "HOME_INVENTORY_ENV",
        "development",
    ).strip().lower()
    if environment not in {"development", "production", "test"}:
        raise RuntimeError(
            "HOME_INVENTORY_ENV 只能是 development、production 或 test"
        )

    data_dir = _resolve_path("HOME_INVENTORY_DATA_DIR", BASE_DIR)
    database_path = _resolve_path(
        "HOME_INVENTORY_DB_PATH",
        data_dir / "home_inventory.db",
    )
    backup_dir = _resolve_path(
        "HOME_INVENTORY_BACKUP_DIR",
        data_dir / "data_backups",
    )
    security_config_path = _resolve_path(
        "HOME_INVENTORY_SECURITY_CONFIG",
        data_dir / ".security.json",
    )
    allowed_hosts = tuple(_parse_csv("HOME_INVENTORY_ALLOWED_HOSTS"))

    return Settings(
        environment=environment,
        data_dir=data_dir,
        database_path=database_path,
        backup_dir=backup_dir,
        security_config_path=security_config_path,
        https_only=_parse_bool("HOME_INVENTORY_HTTPS_ONLY", False),
        allowed_hosts=allowed_hosts,
        session_max_age_seconds=_parse_int(
            "HOME_INVENTORY_SESSION_MAX_AGE_SECONDS",
            12 * 60 * 60,
        ),
        enable_auto_backup=_parse_bool(
            "HOME_INVENTORY_ENABLE_AUTO_BACKUP",
            True,
        ),
        recycle_bin_retention_days=_parse_int(
            "HOME_INVENTORY_RECYCLE_BIN_RETENTION_DAYS",
            30,
        ),
        max_backup_upload_bytes=_parse_int(
            "HOME_INVENTORY_MAX_BACKUP_UPLOAD_MB",
            100,
        ) * 1024 * 1024,
    )


settings = load_settings()
