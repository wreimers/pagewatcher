from pathlib import Path

import pytest

from pagewatcher.config import ConfigError, NotificationProvider, WatcherConfig


def valid_env(tmp_path: Path) -> dict[str, str]:
    private_key = tmp_path / "AuthKey_TEST.p8"
    private_key.write_text("test key", encoding="utf-8")
    return {
        "PAGEWATCHER_URL": "https://example.com/status",
        "PAGEWATCHER_APNS_TEAM_ID": "TEAM123",
        "PAGEWATCHER_APNS_KEY_ID": "KEY123",
        "PAGEWATCHER_APNS_BUNDLE_ID": "com.example.pagewatcher",
        "PAGEWATCHER_APNS_DEVICE_TOKEN": "device-token",
        "PAGEWATCHER_APNS_PRIVATE_KEY_PATH": str(private_key),
    }


def test_from_env_uses_defaults(tmp_path: Path) -> None:
    config = WatcherConfig.from_env(valid_env(tmp_path))

    assert config.url == "https://example.com/status"
    assert config.database_path == Path("pagewatcher.db")
    assert config.poll_interval_seconds == 300.0
    assert config.request_timeout_seconds == 20.0
    assert config.max_response_bytes == 2_000_000
    assert config.include_selectors == ()
    assert config.ignore_selectors == ("script", "style", "noscript", "template")
    assert config.similarity_threshold == 0.98
    assert config.minimum_changed_characters == 20
    assert config.notification_provider is NotificationProvider.APNS
    assert config.apns is not None
    assert config.apns.use_sandbox is True
    assert config.pushover is None


def test_from_env_parses_overrides(tmp_path: Path) -> None:
    env = valid_env(tmp_path)
    env.update(
        {
            "PAGEWATCHER_DATABASE_PATH": "~/state/pagewatcher.sqlite3",
            "PAGEWATCHER_POLL_INTERVAL_SECONDS": "45.5",
            "PAGEWATCHER_REQUEST_TIMEOUT_SECONDS": "4",
            "PAGEWATCHER_MAX_RESPONSE_BYTES": "4096",
            "PAGEWATCHER_INCLUDE_SELECTORS": "main, #availability",
            "PAGEWATCHER_IGNORE_SELECTORS": ".timestamp, aside",
            "PAGEWATCHER_SIMILARITY_THRESHOLD": "0.9",
            "PAGEWATCHER_MINIMUM_CHANGED_CHARACTERS": "5",
            "PAGEWATCHER_APNS_USE_SANDBOX": "no",
        }
    )

    config = WatcherConfig.from_env(env)

    assert config.database_path == Path("~/state/pagewatcher.sqlite3").expanduser()
    assert config.poll_interval_seconds == 45.5
    assert config.request_timeout_seconds == 4.0
    assert config.max_response_bytes == 4096
    assert config.include_selectors == ("main", "#availability")
    assert config.ignore_selectors == (".timestamp", "aside")
    assert config.similarity_threshold == 0.9
    assert config.minimum_changed_characters == 5
    assert config.apns is not None
    assert config.apns.use_sandbox is False


def test_from_env_parses_pushover_configuration_without_apns_credentials() -> None:
    config = WatcherConfig.from_env(
        {
            "PAGEWATCHER_URL": "https://example.com/status",
            "PAGEWATCHER_NOTIFICATION_PROVIDER": "PUSHOVER",
            "PAGEWATCHER_PUSHOVER_APP_TOKEN": "a" * 30,
            "PAGEWATCHER_PUSHOVER_USER_KEY": "U" * 30,
            "PAGEWATCHER_PUSHOVER_DEVICE": "personal_iphone-15",
        }
    )

    assert config.notification_provider is NotificationProvider.PUSHOVER
    assert config.apns is None
    assert config.pushover is not None
    assert config.pushover.app_token == "a" * 30
    assert config.pushover.user_key == "U" * 30
    assert config.pushover.device == "personal_iphone-15"


def test_from_env_allows_pushover_without_specific_device() -> None:
    config = WatcherConfig.from_env(
        {
            "PAGEWATCHER_URL": "https://example.com/status",
            "PAGEWATCHER_NOTIFICATION_PROVIDER": "pushover",
            "PAGEWATCHER_PUSHOVER_APP_TOKEN": "a" * 30,
            "PAGEWATCHER_PUSHOVER_USER_KEY": "u" * 30,
        }
    )

    assert config.pushover is not None
    assert config.pushover.device is None


def test_from_env_rejects_unknown_notification_provider(tmp_path: Path) -> None:
    env = valid_env(tmp_path)
    env["PAGEWATCHER_NOTIFICATION_PROVIDER"] = "carrier-pigeon"

    with pytest.raises(ConfigError, match="must be one of: apns, pushover"):
        WatcherConfig.from_env(env)


@pytest.mark.parametrize(
    "missing_name",
    ["PAGEWATCHER_PUSHOVER_APP_TOKEN", "PAGEWATCHER_PUSHOVER_USER_KEY"],
)
def test_from_env_requires_selected_pushover_credentials(missing_name: str) -> None:
    env = {
        "PAGEWATCHER_URL": "https://example.com/status",
        "PAGEWATCHER_NOTIFICATION_PROVIDER": "pushover",
        "PAGEWATCHER_PUSHOVER_APP_TOKEN": "a" * 30,
        "PAGEWATCHER_PUSHOVER_USER_KEY": "u" * 30,
    }
    del env[missing_name]

    with pytest.raises(ConfigError, match=f"{missing_name} is required"):
        WatcherConfig.from_env(env)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PAGEWATCHER_PUSHOVER_APP_TOKEN", "short", "30 alphanumeric"),
        ("PAGEWATCHER_PUSHOVER_USER_KEY", "!" * 30, "30 alphanumeric"),
        ("PAGEWATCHER_PUSHOVER_DEVICE", "spaces are invalid", "1 to 25"),
        ("PAGEWATCHER_PUSHOVER_DEVICE", "x" * 26, "1 to 25"),
    ],
)
def test_from_env_rejects_invalid_pushover_values(
    name: str, value: str, message: str
) -> None:
    env = {
        "PAGEWATCHER_URL": "https://example.com/status",
        "PAGEWATCHER_NOTIFICATION_PROVIDER": "pushover",
        "PAGEWATCHER_PUSHOVER_APP_TOKEN": "a" * 30,
        "PAGEWATCHER_PUSHOVER_USER_KEY": "u" * 30,
    }
    env[name] = value

    with pytest.raises(ConfigError, match=message):
        WatcherConfig.from_env(env)


@pytest.mark.parametrize(
    "missing_name",
    [
        "PAGEWATCHER_URL",
        "PAGEWATCHER_APNS_TEAM_ID",
        "PAGEWATCHER_APNS_KEY_ID",
        "PAGEWATCHER_APNS_BUNDLE_ID",
        "PAGEWATCHER_APNS_DEVICE_TOKEN",
        "PAGEWATCHER_APNS_PRIVATE_KEY_PATH",
    ],
)
def test_from_env_rejects_missing_required_values(
    tmp_path: Path, missing_name: str
) -> None:
    env = valid_env(tmp_path)
    del env[missing_name]

    with pytest.raises(ConfigError, match=f"{missing_name} is required"):
        WatcherConfig.from_env(env)


@pytest.mark.parametrize("url", ["example.com", "ftp://example.com", "https:///path"])
def test_from_env_rejects_invalid_url(tmp_path: Path, url: str) -> None:
    env = valid_env(tmp_path)
    env["PAGEWATCHER_URL"] = url

    with pytest.raises(ConfigError, match="absolute HTTP or HTTPS URL"):
        WatcherConfig.from_env(env)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PAGEWATCHER_POLL_INTERVAL_SECONDS", "0", "greater than zero"),
        ("PAGEWATCHER_REQUEST_TIMEOUT_SECONDS", "soon", "must be a number"),
        ("PAGEWATCHER_MAX_RESPONSE_BYTES", "1.5", "must be an integer"),
        ("PAGEWATCHER_MINIMUM_CHANGED_CHARACTERS", "-1", "greater than zero"),
        ("PAGEWATCHER_SIMILARITY_THRESHOLD", "1.1", "must be between"),
        ("PAGEWATCHER_APNS_USE_SANDBOX", "maybe", "must be a boolean"),
    ],
)
def test_from_env_rejects_invalid_typed_values(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    env = valid_env(tmp_path)
    env[name] = value

    with pytest.raises(ConfigError, match=message):
        WatcherConfig.from_env(env)


def test_from_env_requires_existing_private_key(tmp_path: Path) -> None:
    env = valid_env(tmp_path)
    env["PAGEWATCHER_APNS_PRIVATE_KEY_PATH"] = str(tmp_path / "missing.p8")

    with pytest.raises(ConfigError, match="must point to an existing file"):
        WatcherConfig.from_env(env)
