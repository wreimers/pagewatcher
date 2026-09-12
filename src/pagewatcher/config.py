"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when pagewatcher configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ApnsConfig:
    """Credentials and routing information for Apple Push Notification service."""

    team_id: str
    key_id: str
    bundle_id: str
    device_token: str
    private_key_path: Path
    use_sandbox: bool = True


@dataclass(frozen=True, slots=True)
class WatcherConfig:
    """Validated settings needed to run a page watcher."""

    url: str
    apns: ApnsConfig
    database_path: Path = Path("pagewatcher.db")
    poll_interval_seconds: float = 300.0
    request_timeout_seconds: float = 20.0
    max_response_bytes: int = 2_000_000
    include_selectors: tuple[str, ...] = ()
    ignore_selectors: tuple[str, ...] = (
        "script",
        "style",
        "noscript",
        "template",
    )
    similarity_threshold: float = 0.98
    minimum_changed_characters: int = 20

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WatcherConfig:
        """Build and validate configuration from PAGEWATCHER_* variables."""

        values = os.environ if env is None else env
        url = _required(values, "PAGEWATCHER_URL")
        _validate_url(url)

        private_key_path = Path(
            _required(values, "PAGEWATCHER_APNS_PRIVATE_KEY_PATH")
        ).expanduser()
        if not private_key_path.is_file():
            raise ConfigError(
                "PAGEWATCHER_APNS_PRIVATE_KEY_PATH must point to an existing file"
            )

        apns = ApnsConfig(
            team_id=_required(values, "PAGEWATCHER_APNS_TEAM_ID"),
            key_id=_required(values, "PAGEWATCHER_APNS_KEY_ID"),
            bundle_id=_required(values, "PAGEWATCHER_APNS_BUNDLE_ID"),
            device_token=_required(values, "PAGEWATCHER_APNS_DEVICE_TOKEN"),
            private_key_path=private_key_path,
            use_sandbox=_boolean(
                values, "PAGEWATCHER_APNS_USE_SANDBOX", default=True
            ),
        )

        return cls(
            url=url,
            apns=apns,
            database_path=Path(
                values.get("PAGEWATCHER_DATABASE_PATH", "pagewatcher.db")
            ).expanduser(),
            poll_interval_seconds=_positive_float(
                values, "PAGEWATCHER_POLL_INTERVAL_SECONDS", default=300.0
            ),
            request_timeout_seconds=_positive_float(
                values, "PAGEWATCHER_REQUEST_TIMEOUT_SECONDS", default=20.0
            ),
            max_response_bytes=_positive_int(
                values, "PAGEWATCHER_MAX_RESPONSE_BYTES", default=2_000_000
            ),
            include_selectors=_selectors(
                values.get("PAGEWATCHER_INCLUDE_SELECTORS", "")
            ),
            ignore_selectors=_selectors(
                values.get(
                    "PAGEWATCHER_IGNORE_SELECTORS",
                    "script,style,noscript,template",
                )
            ),
            similarity_threshold=_bounded_float(
                values,
                "PAGEWATCHER_SIMILARITY_THRESHOLD",
                default=0.98,
                minimum=0.0,
                maximum=1.0,
            ),
            minimum_changed_characters=_positive_int(
                values,
                "PAGEWATCHER_MINIMUM_CHANGED_CHARACTERS",
                default=20,
            ),
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _validate_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("PAGEWATCHER_URL must be an absolute HTTP or HTTPS URL")


def _positive_float(
    env: Mapping[str, str], name: str, *, default: float
) -> float:
    raw = env.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number") from error
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def _positive_int(env: Mapping[str, str], name: str, *, default: int) -> int:
    raw = env.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def _bounded_float(
    env: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = env.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number") from error
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")


def _selectors(value: str) -> tuple[str, ...]:
    return tuple(selector.strip() for selector in value.split(",") if selector.strip())
