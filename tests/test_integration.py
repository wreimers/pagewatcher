import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from pagewatcher.apns import ApnsClient, TransientApnsError
from pagewatcher.config import (
    ApnsConfig,
    NotificationProvider,
    PushoverConfig,
    WatcherConfig,
)
from pagewatcher.fetch import PageFetcher
from pagewatcher.notifier import Notifier
from pagewatcher.pushover import PushoverClient
from pagewatcher.store import SqliteStateStore
from pagewatcher.watcher import PageWatcher, WatchOutcome


URL = "https://shop.example.com/product"


def make_config(tmp_path: Path) -> WatcherConfig:
    private_key = tmp_path / "AuthKey_TEST.p8"
    private_key.write_text("mock private key", encoding="utf-8")
    return WatcherConfig(
        url=URL,
        apns=ApnsConfig(
            team_id="TEAM123",
            key_id="KEY123",
            bundle_id="com.example.pagewatcher",
            device_token="device-token",
            private_key_path=private_key,
        ),
        database_path=tmp_path / "state" / "pagewatcher.sqlite3",
    )


def make_pushover_config(tmp_path: Path) -> WatcherConfig:
    return WatcherConfig(
        url=URL,
        notification_provider=NotificationProvider.PUSHOVER,
        pushover=PushoverConfig(
            app_token="a" * 30,
            user_key="u" * 30,
            device="personal-iphone",
        ),
        database_path=tmp_path / "state" / "pagewatcher.sqlite3",
    )


def test_complete_lifecycle_from_baseline_to_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "provider-token")
    page_requests: list[httpx.Request] = []
    page_responses = iter(
        [
            httpx.Response(
                200,
                content=f"<main>{'a' * 99}x</main>".encode(),
                headers={"ETag": '"v1"'},
            ),
            httpx.Response(304, headers={"ETag": '"v1"'}),
            httpx.Response(
                200,
                content=f"<main>{'a' * 99}y</main>".encode(),
                headers={"ETag": '"v2"'},
            ),
            httpx.Response(
                200,
                content=f"<main>{'b' * 100}</main>".encode(),
                headers={"ETag": '"v3"'},
            ),
        ]
    )

    def page_handler(request: httpx.Request) -> httpx.Response:
        page_requests.append(request)
        return next(page_responses)

    apns_requests: list[httpx.Request] = []

    def apns_handler(request: httpx.Request) -> httpx.Response:
        apns_requests.append(request)
        return httpx.Response(200, headers={"apns-id": "notification-id"})

    with (
        httpx.Client(transport=httpx.MockTransport(page_handler)) as page_http,
        httpx.Client(transport=httpx.MockTransport(apns_handler)) as apns_http,
        SqliteStateStore(config.database_path) as store,
    ):
        watcher = PageWatcher(
            config,
            PageFetcher(client=page_http),
            store,
            ApnsClient(config.apns, client=apns_http),
        )

        outcomes = [watcher.check_once().outcome for _ in range(4)]
        final_state = store.get(URL)

    assert outcomes == [
        WatchOutcome.BASELINE_CREATED,
        WatchOutcome.HTTP_NOT_MODIFIED,
        WatchOutcome.IMMATERIAL_CHANGE,
        WatchOutcome.NOTIFICATION_SENT,
    ]
    assert "if-none-match" not in page_requests[0].headers
    assert page_requests[1].headers["if-none-match"] == '"v1"'
    assert page_requests[2].headers["if-none-match"] == '"v1"'
    assert page_requests[3].headers["if-none-match"] == '"v2"'

    assert len(apns_requests) == 1
    notification_request = apns_requests[0]
    assert notification_request.url.host == "api.sandbox.push.apple.com"
    assert notification_request.headers["authorization"] == "bearer provider-token"
    assert notification_request.headers["apns-topic"] == config.apns.bundle_id
    assert len(notification_request.headers["apns-collapse-id"]) == 64
    assert json.loads(notification_request.content) == {
        "aps": {
            "alert": {
                "title": "Page changed: shop.example.com",
                "body": "b" * 100,
            },
            "sound": "default",
        },
        "url": URL,
    }

    assert final_state is not None
    assert final_state.snapshot_text == "b" * 100
    assert final_state.validators.etag == '"v3"'
    assert final_state.last_notified_hash == final_state.snapshot_hash

    with SqliteStateStore(config.database_path) as reopened:
        assert reopened.get(URL) == final_state


def test_failed_apns_delivery_is_retried_without_advancing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "provider-token")
    page_requests: list[httpx.Request] = []
    page_responses = iter(
        [
            httpx.Response(
                200,
                content=b"<main>In stock</main>",
                headers={"ETag": '"v1"'},
            ),
            httpx.Response(
                200,
                content=b"<main>Sold out</main>",
                headers={"ETag": '"v2"'},
            ),
            httpx.Response(
                200,
                content=b"<main>Sold out</main>",
                headers={"ETag": '"v2"'},
            ),
        ]
    )

    def page_handler(request: httpx.Request) -> httpx.Response:
        page_requests.append(request)
        return next(page_responses)

    apns_requests: list[httpx.Request] = []
    apns_responses = iter(
        [
            httpx.Response(503, json={"reason": "ServiceUnavailable"}),
            httpx.Response(200, headers={"apns-id": "retry-id"}),
        ]
    )

    def apns_handler(request: httpx.Request) -> httpx.Response:
        apns_requests.append(request)
        return next(apns_responses)

    with (
        httpx.Client(transport=httpx.MockTransport(page_handler)) as page_http,
        httpx.Client(transport=httpx.MockTransport(apns_handler)) as apns_http,
        SqliteStateStore(config.database_path) as store,
    ):
        watcher = PageWatcher(
            config,
            PageFetcher(client=page_http),
            store,
            ApnsClient(config.apns, client=apns_http),
        )
        assert watcher.check_once().outcome is WatchOutcome.BASELINE_CREATED
        baseline = store.get(URL)

        with pytest.raises(TransientApnsError, match="ServiceUnavailable"):
            watcher.check_once()
        after_failure = store.get(URL)

        retry = watcher.check_once()
        after_retry = store.get(URL)

    assert after_failure == baseline
    assert page_requests[1].headers["if-none-match"] == '"v1"'
    assert page_requests[2].headers["if-none-match"] == '"v1"'
    assert len(apns_requests) == 2
    assert (
        apns_requests[0].headers["apns-collapse-id"]
        == apns_requests[1].headers["apns-collapse-id"]
    )
    assert retry.outcome is WatchOutcome.NOTIFICATION_SENT
    assert retry.notification is not None and retry.notification.request_id == "retry-id"
    assert after_retry is not None and after_retry.snapshot_text == "Sold out"


def test_complete_pushover_lifecycle(
    tmp_path: Path,
) -> None:
    config = make_pushover_config(tmp_path)
    page_responses = iter(
        [
            httpx.Response(
                200,
                content=b"<main>In stock</main>",
                headers={"ETag": '"v1"'},
            ),
            httpx.Response(
                200,
                content=b"<main>Sold out</main>",
                headers={"ETag": '"v2"'},
            ),
        ]
    )
    pushover_requests: list[httpx.Request] = []

    def pushover_handler(request: httpx.Request) -> httpx.Response:
        pushover_requests.append(request)
        return httpx.Response(
            200,
            json={"status": 1, "request": "pushover-request-id"},
        )

    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: next(page_responses))
        ) as page_http,
        httpx.Client(transport=httpx.MockTransport(pushover_handler)) as pushover_http,
        SqliteStateStore(config.database_path) as store,
    ):
        assert config.pushover is not None
        notifier = Notifier(
            config,
            pushover_client=PushoverClient(config.pushover, client=pushover_http),
        )
        watcher = PageWatcher(
            config,
            PageFetcher(client=page_http),
            store,
            notifier,
        )

        baseline = watcher.check_once()
        changed = watcher.check_once()
        final_state = store.get(URL)

    assert baseline.outcome is WatchOutcome.BASELINE_CREATED
    assert changed.outcome is WatchOutcome.NOTIFICATION_SENT
    assert changed.notification is not None
    assert changed.notification.provider is NotificationProvider.PUSHOVER
    assert changed.notification.request_id == "pushover-request-id"

    assert len(pushover_requests) == 1
    notification_fields = parse_qs(pushover_requests[0].content.decode())
    assert notification_fields == {
        "token": ["a" * 30],
        "user": ["u" * 30],
        "device": ["personal-iphone"],
        "title": ["Page changed: shop.example.com"],
        "message": ["Sold out"],
        "priority": ["0"],
        "url": [URL],
        "url_title": ["View monitored page"],
    }
    assert final_state is not None
    assert final_state.snapshot_text == "Sold out"
    assert final_state.validators.etag == '"v2"'
    assert final_state.last_notified_hash == final_state.snapshot_hash
