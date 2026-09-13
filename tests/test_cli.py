from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from pagewatcher import cli
from pagewatcher.change import assess_change
from pagewatcher.config import (
    ApnsConfig,
    ConfigError,
    FetchMode,
    NotificationProvider,
    PushoverConfig,
    WatcherConfig,
)
from pagewatcher.fetch import TransientFetchError
from pagewatcher.notifier import NotificationResponse
from pagewatcher.pushover import TransientPushoverError
from pagewatcher.watcher import WatchOutcome, WatchResult


URL = "https://example.com/product"


def make_config(tmp_path: Path) -> WatcherConfig:
    private_key = tmp_path / "AuthKey_TEST.p8"
    private_key.write_text("unused", encoding="utf-8")
    return WatcherConfig(
        url=URL,
        apns=ApnsConfig(
            team_id="TEAM123",
            key_id="KEY123",
            bundle_id="com.example.pagewatcher",
            device_token="secret-device-token",
            private_key_path=private_key,
        ),
        poll_interval_seconds=15,
    )


def make_pushover_config() -> WatcherConfig:
    return WatcherConfig(
        url=URL,
        notification_provider=NotificationProvider.PUSHOVER,
        pushover=PushoverConfig(app_token="a" * 30, user_key="u" * 30),
        poll_interval_seconds=15,
    )


def set_config(monkeypatch: pytest.MonkeyPatch, config: WatcherConfig) -> None:
    monkeypatch.setattr(
        cli.WatcherConfig,
        "from_env",
        Mock(return_value=config),
    )


def set_watcher(
    monkeypatch: pytest.MonkeyPatch, watcher: Mock
) -> None:
    @contextmanager
    def open_watcher(config):
        yield watcher

    monkeypatch.setattr(cli, "_open_watcher", open_watcher)


def test_validate_reports_url_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    set_config(monkeypatch, config)

    status = cli.main(["validate"])

    output = capsys.readouterr()
    assert status == 0
    assert URL in output.out
    assert config.apns.device_token not in output.out
    assert config.apns.team_id not in output.out
    assert output.err == ""


def test_configuration_error_returns_status_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.WatcherConfig,
        "from_env",
        Mock(side_effect=ConfigError("PAGEWATCHER_URL is required")),
    )

    status = cli.main(["check"])

    output = capsys.readouterr()
    assert status == 2
    assert output.out == ""
    assert "Configuration error: PAGEWATCHER_URL is required" in output.err


def test_check_runs_once_and_prints_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    set_config(monkeypatch, config)
    watcher = Mock()
    watcher.check_once.return_value = WatchResult(
        WatchOutcome.BASELINE_CREATED,
        URL,
        assessment=assess_change(None, "In stock"),
    )
    set_watcher(monkeypatch, watcher)

    status = cli.main(["check"])

    assert status == 0
    watcher.check_once.assert_called_once_with()
    assert "Check complete: baseline_created" in capsys.readouterr().out


def test_check_prints_materiality_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    set_config(monkeypatch, config)
    watcher = Mock()
    watcher.check_once.return_value = WatchResult(
        WatchOutcome.IMMATERIAL_CHANGE,
        URL,
        assessment=assess_change("a" * 100, "a" * 99 + "b"),
    )
    set_watcher(monkeypatch, watcher)

    status = cli.main(["check"])

    output = capsys.readouterr().out
    assert status == 0
    assert "similarity=0.9900" in output
    assert "changed_characters=1" in output


def test_runtime_error_returns_status_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    set_config(monkeypatch, config)
    watcher = Mock()
    watcher.check_once.side_effect = TransientFetchError("timed out")
    set_watcher(monkeypatch, watcher)

    status = cli.main(["check"])

    assert status == 1
    assert "Error: timed out" in capsys.readouterr().err


def test_watch_retries_transient_errors_and_prints_later_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    set_config(monkeypatch, config)
    watcher = Mock()
    watcher.check_once.side_effect = [
        TransientPushoverError("service unavailable"),
        WatchResult(WatchOutcome.HTTP_NOT_MODIFIED, URL),
    ]
    set_watcher(monkeypatch, watcher)
    sleep = Mock(side_effect=[None, KeyboardInterrupt])
    monkeypatch.setattr(cli.time, "sleep", sleep)

    status = cli.main(["watch"])

    output = capsys.readouterr()
    assert status == 130
    assert watcher.check_once.call_count == 2
    assert sleep.call_args_list[0].args == (15,)
    assert "Transient error; will retry: service unavailable" in output.err
    assert "Check complete: http_not_modified" in output.out


def test_test_notification_sends_expected_alert(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    timestamp = "2026-09-13T12:42:11-07:00"
    config = make_pushover_config()
    set_config(monkeypatch, config)
    datetime = Mock()
    local_time = datetime.now.return_value.astimezone.return_value
    local_time.isoformat.return_value = timestamp
    monkeypatch.setattr(cli, "datetime", datetime)
    notifier = Mock()
    notifier.__enter__ = Mock(return_value=notifier)
    notifier.__exit__ = Mock(return_value=None)
    notifier.send_alert.return_value = NotificationResponse(
        NotificationProvider.PUSHOVER, "request-id"
    )
    constructor = Mock(return_value=notifier)
    monkeypatch.setattr(cli, "Notifier", constructor)

    status = cli.main(["test-notification"])

    assert status == 0
    constructor.assert_called_once_with(config)
    notifier.send_alert.assert_called_once_with(
        "Pagewatcher test",
        f"Notifications are configured correctly.\nSent at: {timestamp}",
        url=URL,
        deduplication_key="pagewatcher-test",
    )
    datetime.now.assert_called_once_with()
    datetime.now.return_value.astimezone.assert_called_once_with()
    local_time.isoformat.assert_called_once_with(timespec="seconds")
    output = capsys.readouterr().out
    assert "accepted by pushover" in output
    assert "request-id: request-id" in output
    assert f"timestamp: {timestamp}" in output


def test_open_watcher_constructs_configured_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_pushover_config()
    fetcher = MagicMock()
    fetcher.__enter__.return_value = fetcher
    store = MagicMock()
    store.__enter__.return_value = store
    notifier = MagicMock()
    notifier.__enter__.return_value = notifier
    fetcher_constructor = Mock(return_value=fetcher)
    store_constructor = Mock(return_value=store)
    notifier_constructor = Mock(return_value=notifier)
    monkeypatch.setattr(cli, "PageFetcher", fetcher_constructor)
    monkeypatch.setattr(cli, "SqliteStateStore", store_constructor)
    monkeypatch.setattr(cli, "Notifier", notifier_constructor)

    with cli._open_watcher(config) as watcher:
        assert watcher.fetcher is fetcher
        assert watcher.store is store
        assert watcher.notifier is notifier

    fetcher_constructor.assert_called_once_with(
        timeout_seconds=config.request_timeout_seconds,
        max_response_bytes=config.max_response_bytes,
    )
    store_constructor.assert_called_once_with(config.database_path)
    notifier_constructor.assert_called_once_with(config)


def test_create_fetcher_constructs_configured_browser_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        make_pushover_config(),
        fetch_mode=FetchMode.CHROMIUM,
        browser_profile_path=Path("browser-state"),
        browser_headless=False,
        browser_settle_seconds=3.5,
        request_timeout_seconds=45,
        max_response_bytes=8192,
    )
    fetcher = Mock()
    browser_constructor = Mock(return_value=fetcher)
    http_constructor = Mock()
    monkeypatch.setattr(cli, "BrowserPageFetcher", browser_constructor)
    monkeypatch.setattr(cli, "PageFetcher", http_constructor)

    result = cli._create_fetcher(config)

    assert result is fetcher
    browser_constructor.assert_called_once_with(
        profile_path=Path("browser-state"),
        timeout_seconds=45,
        max_response_bytes=8192,
        headless=False,
        settle_seconds=3.5,
    )
    http_constructor.assert_not_called()
