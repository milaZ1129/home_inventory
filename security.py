import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from config import settings

DEFAULT_CONFIG_PATH = settings.security_config_path
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16


@dataclass(frozen=True)
class SecurityConfig:
    username: str
    password_salt: bytes
    password_hash: bytes
    session_secret: str


def get_config_path():
    configured_path = os.environ.get("HOME_INVENTORY_SECURITY_CONFIG")
    return Path(configured_path).expanduser() if configured_path else DEFAULT_CONFIG_PATH


def derive_password_hash(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )


def write_security_config(username, password, path=None):
    username = username.strip()
    if not username:
        raise ValueError("用户名不能为空")
    if not password:
        raise ValueError("密码不能为空")
    if len(username) > 50:
        raise ValueError("用户名不能超过 50 个字符")
    if len(password) > 1024:
        raise ValueError("密码过长")

    config_path = Path(path) if path else get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    salt = secrets.token_bytes(SALT_BYTES)
    password_hash = derive_password_hash(password, salt)
    payload = {
        "username": username,
        "password_salt": base64.b64encode(salt).decode("ascii"),
        "password_hash": base64.b64encode(password_hash).decode("ascii"),
        "session_secret": secrets.token_urlsafe(48),
        "password_algorithm": "pbkdf2-hmac-sha256",
        "password_iterations": PBKDF2_ITERATIONS,
    }

    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(config_path)
    os.chmod(config_path, 0o600)
    return config_path


def load_security_config(path=None):
    config_path = Path(path) if path else get_config_path()
    if not config_path.exists():
        env_username = os.environ.get("HOME_INVENTORY_USERNAME")
        env_password = os.environ.get("HOME_INVENTORY_PASSWORD")
        if env_username and env_password:
            write_security_config(
                env_username,
                env_password,
                path=config_path,
            )
        else:
            hint = (
                "缺少安全配置。请先运行：python scripts/set_password.py"
            )
            if settings.is_production:
                hint += (
                    "，或在首次部署时设置 HOME_INVENTORY_USERNAME "
                    "和 HOME_INVENTORY_PASSWORD 环境变量。"
                )
            raise RuntimeError(hint)

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        if payload.get("password_algorithm") != "pbkdf2-hmac-sha256":
            raise ValueError("不支持的密码算法")
        if payload.get("password_iterations") != PBKDF2_ITERATIONS:
            raise ValueError("不支持的密码迭代参数")

        return SecurityConfig(
            username=str(payload["username"]),
            password_salt=base64.b64decode(
                payload["password_salt"],
                validate=True,
            ),
            password_hash=base64.b64decode(
                payload["password_hash"],
                validate=True,
            ),
            session_secret=str(payload["session_secret"]),
        )
    except (KeyError, OSError, ValueError, TypeError) as exc:
        raise RuntimeError("安全配置损坏，请重新设置密码") from exc


def verify_credentials(config, username, password):
    username_matches = hmac.compare_digest(
        username.encode("utf-8"),
        config.username.encode("utf-8"),
    )
    candidate_hash = derive_password_hash(password, config.password_salt)
    password_matches = hmac.compare_digest(
        candidate_hash,
        config.password_hash,
    )
    return username_matches and password_matches


class LoginRateLimiter:
    def __init__(self, max_attempts=5, window_seconds=300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts = defaultdict(deque)
        self._lock = threading.Lock()

    def _remove_expired(self, attempts, now):
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    def retry_after(self, key):
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            self._remove_expired(attempts, now)
            if len(attempts) < self.max_attempts:
                return 0
            return max(1, int(attempts[0] + self.window_seconds - now))

    def record_failure(self, key):
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            self._remove_expired(attempts, now)
            attempts.append(now)

    def reset(self, key):
        with self._lock:
            self._attempts.pop(key, None)

    def clear(self):
        with self._lock:
            self._attempts.clear()
