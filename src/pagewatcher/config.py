"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values


_ENV_FILE_VARIABLE = "PAGEWATCHER_ENV_FILE"


class ConfigError(ValueError):
    """Raised when pagewatcher configuration is missing or invalid."""


class NotificationProvider(StrEnum):
    """Supported notification delivery services."""

    APNS = "apns"
    PUSHOVER = "pushover"


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
class PushoverConfig:
    """Credentials and optional device routing for Pushover."""

    app_token: str
    user_key: str
    device: str | None = None


@dataclass(frozen=True, slots=True)
class WatcherConfig:
    """Validated settings needed to run a page watcher."""

    url: str
    apns: ApnsConfig | None = None
    pushover: PushoverConfig | None = None
    notification_provider: NotificationProvider = NotificationProvider.APNS
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
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        dotenv_path: str | Path | None = None,
    ) -> WatcherConfig:
        """Build configuration from a dotenv file and PAGEWATCHER_* variables.

        A supplied ``env`` mapping remains isolated for programmatic use unless a
        ``dotenv_path`` or ``PAGEWATCHER_ENV_FILE`` entry explicitly requests a file.
        Environment values always override values parsed from the dotenv file.
        """

        values = _configuration_values(env, dotenv_path)
        url = _required(values, "PAGEWATCHER_URL")
        _validate_url(url)
        provider = _notification_provider(values)
        apns = _apns_config(values) if provider is NotificationProvider.APNS else None
        pushover = (
            _pushover_config(values)
            if provider is NotificationProvider.PUSHOVER
            else None
        )

        return cls(
            url=url,
            apns=apns,
            pushover=pushover,
            notification_provider=provider,
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


def _configuration_values(
    env: Mapping[str, str] | None,
    dotenv_path: str | Path | None,
) -> Mapping[str, str]:
    if env is not None and dotenv_path is None and _ENV_FILE_VARIABLE not in env:
        return env

    overrides = os.environ if env is None else env
    configured_path = overrides.get(_ENV_FILE_VARIABLE)
    path_was_requested = dotenv_path is not None or configured_path is not None
    if dotenv_path is not None:
        raw_path: str | Path = dotenv_path
    elif configured_path is not None:
        raw_path = configured_path
    else:
        raw_path = ".env"
    if not str(raw_path).strip():
        raise ConfigError(f"{_ENV_FILE_VARIABLE} must not be empty")
    path = Path(raw_path).expanduser()
    if not path.is_file():
        if path_was_requested:
            raise ConfigError(f"dotenv file does not exist: {path}")
        return overrides

    file_values = {
        key: value
        for key, value in dotenv_values(path).items()
        if value is not None
    }
    return {**file_values, **overrides}


def _notification_provider(env: Mapping[str, str]) -> NotificationProvider:
    raw = env.get("PAGEWATCHER_NOTIFICATION_PROVIDER", "apns").strip().lower()
    try:
        return NotificationProvider(raw)
    except ValueError as error:
        choices = ", ".join(provider.value for provider in NotificationProvider)
        raise ConfigError(
            f"PAGEWATCHER_NOTIFICATION_PROVIDER must be one of: {choices}"
        ) from error


def _apns_config(env: Mapping[str, str]) -> ApnsConfig:
    private_key_path = Path(
        _required(env, "PAGEWATCHER_APNS_PRIVATE_KEY_PATH")
    ).expanduser()
    if not private_key_path.is_file():
        raise ConfigError(
            "PAGEWATCHER_APNS_PRIVATE_KEY_PATH must point to an existing file"
        )
    return ApnsConfig(
        team_id=_required(env, "PAGEWATCHER_APNS_TEAM_ID"),
        key_id=_required(env, "PAGEWATCHER_APNS_KEY_ID"),
        bundle_id=_required(env, "PAGEWATCHER_APNS_BUNDLE_ID"),
        device_token=_required(env, "PAGEWATCHER_APNS_DEVICE_TOKEN"),
        private_key_path=private_key_path,
        use_sandbox=_boolean(env, "PAGEWATCHER_APNS_USE_SANDBOX", default=True),
    )


def _pushover_config(env: Mapping[str, str]) -> PushoverConfig:
    app_token = _required(env, "PAGEWATCHER_PUSHOVER_APP_TOKEN")
    user_key = _required(env, "PAGEWATCHER_PUSHOVER_USER_KEY")
    if re.fullmatch(r"[A-Za-z0-9]{30}", app_token) is None:
        raise ConfigError(
            "PAGEWATCHER_PUSHOVER_APP_TOKEN must be 30 alphanumeric characters"
        )
    if re.fullmatch(r"[A-Za-z0-9]{30}", user_key) is None:
        raise ConfigError(
            "PAGEWATCHER_PUSHOVER_USER_KEY must be 30 alphanumeric characters"
        )
    raw_device = env.get("PAGEWATCHER_PUSHOVER_DEVICE", "").strip()
    if raw_device and re.fullmatch(r"[A-Za-z0-9_-]{1,25}", raw_device) is None:
        raise ConfigError(
            "PAGEWATCHER_PUSHOVER_DEVICE must contain 1 to 25 letters, numbers, "
            "underscores, or hyphens"
        )
    return PushoverConfig(
        app_token=app_token,
        user_key=user_key,
        device=raw_device or None,
    )


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
