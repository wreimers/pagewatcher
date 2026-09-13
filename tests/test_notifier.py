from pathlib import Path
from unittest.mock import Mock

import pytest

from pagewatcher.apns import ApnsClient, ApnsResponse
from pagewatcher.config import (
    ApnsConfig,
    NotificationProvider,
    PushoverConfig,
    WatcherConfig,
)
from pagewatcher.notifier import NotificationResponse, NotificationSender, Notifier
from pagewatcher.pushover import PushoverClient, PushoverResponse


def apns_watcher_config(tmp_path: Path) -> WatcherConfig:
    private_key = tmp_path / "AuthKey_TEST.p8"
    private_key.write_text("unused", encoding="utf-8")
    return WatcherConfig(
        url="https://example.com/product",
        notification_provider=NotificationProvider.APNS,
        apns=ApnsConfig(
            team_id="TEAM123",
            key_id="KEY123",
            bundle_id="com.example.pagewatcher",
            device_token="device-token",
            private_key_path=private_key,
        ),
    )


def pushover_watcher_config() -> WatcherConfig:
    return WatcherConfig(
        url="https://example.com/product",
        notification_provider=NotificationProvider.PUSHOVER,
        pushover=PushoverConfig(app_token="a" * 30, user_key="u" * 30),
    )


def test_adapts_apns_delivery_and_response(tmp_path: Path) -> None:
    config = apns_watcher_config(tmp_path)
    apns_client = Mock(spec=ApnsClient)
    apns_client.send_alert.return_value = ApnsResponse("apns-request-id", None)
    notifier = Notifier(config, apns_client=apns_client)

    result = notifier.send_alert(
        "Page changed",
        "New content",
        url=config.url,
        deduplication_key="snapshot-hash",
    )

    apns_client.send_alert.assert_called_once_with(
        "Page changed",
        "New content",
        url=config.url,
        collapse_id="snapshot-hash",
    )
    assert result == NotificationResponse(
        provider=NotificationProvider.APNS,
        request_id="apns-request-id",
    )


def test_adapts_pushover_delivery_and_response() -> None:
    config = pushover_watcher_config()
    pushover_client = Mock(spec=PushoverClient)
    pushover_client.send_message.return_value = PushoverResponse("pushover-request-id")
    notifier = Notifier(config, pushover_client=pushover_client)

    result = notifier.send_alert(
        "Page changed",
        "New content",
        url=config.url,
        deduplication_key="not-supported-by-pushover",
    )

    pushover_client.send_message.assert_called_once_with(
        "Page changed",
        "New content",
        url=config.url,
        url_title="View monitored page",
    )
    assert result == NotificationResponse(
        provider=NotificationProvider.PUSHOVER,
        request_id="pushover-request-id",
    )


def test_satisfies_notification_sender_protocol(tmp_path: Path) -> None:
    notifier: NotificationSender = Notifier(
        apns_watcher_config(tmp_path),
        apns_client=Mock(spec=ApnsClient),
    )

    assert isinstance(notifier, NotificationSender)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            WatcherConfig(
                url="https://example.com",
                notification_provider=NotificationProvider.APNS,
            ),
            "APNs configuration is required",
        ),
        (
            WatcherConfig(
                url="https://example.com",
                notification_provider=NotificationProvider.PUSHOVER,
            ),
            "Pushover configuration is required",
        ),
    ],
)
def test_requires_configuration_for_selected_provider(
    config: WatcherConfig, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Notifier(config)


def test_rejects_client_for_unselected_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Pushover client cannot be used"):
        Notifier(
            apns_watcher_config(tmp_path),
            pushover_client=Mock(spec=PushoverClient),
        )

    with pytest.raises(ValueError, match="APNs client cannot be used"):
        Notifier(
            pushover_watcher_config(),
            apns_client=Mock(spec=ApnsClient),
        )


def test_context_manager_closes_owned_selected_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Mock(spec=ApnsClient)
    constructor = Mock(return_value=client)
    monkeypatch.setattr("pagewatcher.notifier.ApnsClient", constructor)
    config = apns_watcher_config(tmp_path)

    with Notifier(config) as notifier:
        assert notifier.provider is NotificationProvider.APNS

    constructor.assert_called_once_with(config.apns)
    client.close.assert_called_once_with()


def test_context_manager_creates_and_closes_owned_pushover_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock(spec=PushoverClient)
    constructor = Mock(return_value=client)
    monkeypatch.setattr("pagewatcher.notifier.PushoverClient", constructor)
    config = pushover_watcher_config()

    with Notifier(config) as notifier:
        assert notifier.provider is NotificationProvider.PUSHOVER

    constructor.assert_called_once_with(config.pushover)
    client.close.assert_called_once_with()


def test_does_not_close_injected_client(tmp_path: Path) -> None:
    client = Mock(spec=ApnsClient)

    with Notifier(apns_watcher_config(tmp_path), apns_client=client):
        pass

    client.close.assert_not_called()
