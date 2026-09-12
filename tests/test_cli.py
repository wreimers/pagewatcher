from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from pagewatcher import cli
from pagewatcher.apns import ApnsResponse, TransientApnsError
from pagewatcher.change import assess_change
from pagewatcher.config import ApnsConfig, ConfigError, WatcherConfig
from pagewatcher.fetch import TransientFetchError
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
        TransientApnsError("service unavailable"),
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    set_config(monkeypatch, config)
    notifier = Mock()
    notifier.__enter__ = Mock(return_value=notifier)
    notifier.__exit__ = Mock(return_value=None)
    notifier.send_alert.return_value = ApnsResponse("request-id", None)
    constructor = Mock(return_value=notifier)
    monkeypatch.setattr(cli, "ApnsClient", constructor)

    status = cli.main(["test-notification"])

    assert status == 0
    constructor.assert_called_once_with(config.apns)
    notifier.send_alert.assert_called_once_with(
        "Pagewatcher test",
        "APNs notifications are configured correctly.",
        url=URL,
        collapse_id="pagewatcher-test",
    )
    assert "apns-id: request-id" in capsys.readouterr().out
