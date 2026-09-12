import json
from dataclasses import replace
from pathlib import Path

import httpx
import jwt
import pytest

from pagewatcher.apns import (
    APNS_PAYLOAD_LIMIT,
    ApnsClient,
    PayloadTooLargeError,
    PermanentApnsError,
    TransientApnsError,
)
from pagewatcher.config import ApnsConfig


@pytest.fixture
def apns_config(tmp_path: Path) -> ApnsConfig:
    private_key = tmp_path / "AuthKey_TEST.p8"
    private_key.write_text("private key contents", encoding="utf-8")
    return ApnsConfig(
        team_id="TEAM123",
        key_id="KEY123",
        bundle_id="com.example.pagewatcher",
        device_token="device-token",
        private_key_path=private_key,
        use_sandbox=True,
    )


@pytest.fixture(autouse=True)
def encoded_tokens(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def encode(payload, key, *, algorithm, headers):
        calls.append(
            {
                "payload": payload,
                "key": key,
                "algorithm": algorithm,
                "headers": headers,
            }
        )
        return f"signed-token-{len(calls)}"

    monkeypatch.setattr(jwt, "encode", encode)
    return calls


def make_client(
    config: ApnsConfig,
    handler,
    *,
    clock=lambda: 1_700_000_000,
) -> ApnsClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return ApnsClient(config, client=http_client, clock=clock)


def test_sends_alert_to_sandbox_with_required_headers_and_payload(
    apns_config: ApnsConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://api.sandbox.push.apple.com/3/device/device-token"
        )
        assert request.headers["authorization"] == "bearer signed-token-1"
        assert request.headers["apns-topic"] == "com.example.pagewatcher"
        assert request.headers["apns-push-type"] == "alert"
        assert request.headers["apns-priority"] == "10"
        assert request.headers["apns-expiration"] == "0"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "aps": {
                "alert": {"title": "Page changed", "body": "Widget is available"},
                "sound": "default",
            },
            "url": "https://example.com/widget",
        }
        return httpx.Response(
            200,
            headers={"apns-id": "request-id", "apns-unique-id": "delivery-id"},
        )

    result = make_client(apns_config, handler).send_alert(
        "Page changed",
        "Widget is available",
        url="https://example.com/widget",
    )

    assert result.apns_id == "request-id"
    assert result.apns_unique_id == "delivery-id"


def test_uses_production_endpoint(apns_config: ApnsConfig) -> None:
    config = replace(apns_config, use_sandbox=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.push.apple.com"
        return httpx.Response(200)

    make_client(config, handler).send_alert("Changed", "New content")


def test_generates_es256_provider_token_with_apple_claims(
    apns_config: ApnsConfig,
    encoded_tokens: list[dict[str, object]],
) -> None:
    make_client(apns_config, lambda request: httpx.Response(200)).send_alert(
        "Changed", "New content"
    )

    assert encoded_tokens == [
        {
            "payload": {"iss": "TEAM123", "iat": 1_700_000_000},
            "key": "private key contents",
            "algorithm": "ES256",
            "headers": {"kid": "KEY123"},
        }
    ]


def test_reuses_then_refreshes_provider_token(
    apns_config: ApnsConfig,
    encoded_tokens: list[dict[str, object]],
) -> None:
    current_time = [1_700_000_000]
    client = make_client(
        apns_config,
        lambda request: httpx.Response(200),
        clock=lambda: current_time[0],
    )

    client.send_alert("Changed", "First")
    current_time[0] += 2_999
    client.send_alert("Changed", "Second")
    current_time[0] += 1
    client.send_alert("Changed", "Third")

    assert len(encoded_tokens) == 2


def test_sends_collapse_identifier(apns_config: ApnsConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apns-collapse-id"] == "snapshot-hash"
        return httpx.Response(200)

    make_client(apns_config, handler).send_alert(
        "Changed", "New content", collapse_id="snapshot-hash"
    )


@pytest.mark.parametrize("collapse_id", ["", "x" * 65])
def test_rejects_invalid_collapse_identifier(
    apns_config: ApnsConfig, collapse_id: str
) -> None:
    with pytest.raises(ValueError, match="between 1 and 64 bytes"):
        make_client(apns_config, lambda request: httpx.Response(200)).send_alert(
            "Changed", "New content", collapse_id=collapse_id
        )


def test_rejects_payload_larger_than_apns_limit(apns_config: ApnsConfig) -> None:
    with pytest.raises(PayloadTooLargeError) as raised:
        make_client(apns_config, lambda request: httpx.Response(200)).send_alert(
            "Changed", "\N{SNOWMAN}" * APNS_PAYLOAD_LIMIT
        )

    assert str(APNS_PAYLOAD_LIMIT) in str(raised.value)


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_classifies_retryable_statuses_as_transient(
    apns_config: ApnsConfig, status_code: int
) -> None:
    client = make_client(
        apns_config,
        lambda request: httpx.Response(status_code, json={"reason": "RetryLater"}),
    )

    with pytest.raises(TransientApnsError) as raised:
        client.send_alert("Changed", "New content")

    assert raised.value.status_code == status_code
    assert raised.value.reason == "RetryLater"


@pytest.mark.parametrize("status_code", [400, 404, 410, 413])
def test_classifies_nonretryable_statuses_as_permanent(
    apns_config: ApnsConfig, status_code: int
) -> None:
    client = make_client(
        apns_config,
        lambda request: httpx.Response(
            status_code,
            json={"reason": "BadDeviceToken", "timestamp": 1_700_000_000_000},
            headers={"apns-id": "request-id"},
        ),
    )

    with pytest.raises(PermanentApnsError) as raised:
        client.send_alert("Changed", "New content")

    assert raised.value.status_code == status_code
    assert raised.value.reason == "BadDeviceToken"
    assert raised.value.apns_id == "request-id"
    assert raised.value.timestamp == 1_700_000_000_000


def test_expired_provider_token_is_invalidated_for_retry(
    apns_config: ApnsConfig,
    encoded_tokens: list[dict[str, object]],
) -> None:
    responses = iter(
        [
            httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
            httpx.Response(200),
        ]
    )
    client = make_client(apns_config, lambda request: next(responses))

    with pytest.raises(TransientApnsError, match="ExpiredProviderToken"):
        client.send_alert("Changed", "First attempt")
    client.send_alert("Changed", "Retry")

    assert len(encoded_tokens) == 2


def test_wraps_network_errors_as_transient(apns_config: ApnsConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(TransientApnsError, match="request failed"):
        make_client(apns_config, handler).send_alert("Changed", "New content")


@pytest.mark.parametrize(("title", "body"), [("", "body"), ("title", " ")])
def test_rejects_empty_alert_text(
    apns_config: ApnsConfig, title: str, body: str
) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        make_client(apns_config, lambda request: httpx.Response(200)).send_alert(
            title, body
        )


def test_context_manager_closes_owned_client(apns_config: ApnsConfig) -> None:
    with ApnsClient(apns_config) as client:
        http_client = client._client

    assert http_client.is_closed
