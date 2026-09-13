from urllib.parse import parse_qs

import httpx
import pytest

from pagewatcher.config import PushoverConfig
from pagewatcher.pushover import (
    MAX_MESSAGE_CHARACTERS,
    MAX_TITLE_CHARACTERS,
    MAX_URL_CHARACTERS,
    MAX_URL_TITLE_CHARACTERS,
    PermanentPushoverError,
    PushoverClient,
    TransientPushoverError,
)


@pytest.fixture
def pushover_config() -> PushoverConfig:
    return PushoverConfig(
        app_token="a" * 30,
        user_key="u" * 30,
        device="personal-iphone",
    )


def make_client(config: PushoverConfig, handler) -> PushoverClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return PushoverClient(config, client=http_client)


def test_sends_form_encoded_message_and_returns_request_id(
    pushover_config: PushoverConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.pushover.net/1/messages.json"
        assert request.headers["content-type"].startswith(
            "application/x-www-form-urlencoded"
        )
        assert parse_qs(request.content.decode()) == {
            "token": ["a" * 30],
            "user": ["u" * 30],
            "device": ["personal-iphone"],
            "title": ["Page changed"],
            "message": ["Widget is available"],
            "priority": ["0"],
            "url": ["https://example.com/widget"],
            "url_title": ["Open monitored page"],
        }
        return httpx.Response(
            200,
            json={"status": 1, "request": "request-id"},
        )

    result = make_client(pushover_config, handler).send_message(
        "Page changed",
        "Widget is available",
        url="https://example.com/widget",
        url_title="Open monitored page",
    )

    assert result.request_id == "request-id"


def test_omits_optional_device_and_url() -> None:
    config = PushoverConfig(app_token="a" * 30, user_key="u" * 30)

    def handler(request: httpx.Request) -> httpx.Response:
        fields = parse_qs(request.content.decode())
        assert "device" not in fields
        assert "url" not in fields
        assert "url_title" not in fields
        return httpx.Response(200, json={"status": 1, "request": "request-id"})

    make_client(config, handler).send_message("Page changed", "New content")


def test_parses_validation_errors_and_request_id(
    pushover_config: PushoverConfig,
) -> None:
    client = make_client(
        pushover_config,
        lambda request: httpx.Response(
            400,
            json={
                "status": 0,
                "request": "failed-request-id",
                "errors": ["user identifier is invalid", "message is required"],
            },
        ),
    )

    with pytest.raises(PermanentPushoverError) as raised:
        client.send_message("Page changed", "New content")

    assert raised.value.status_code == 400
    assert raised.value.request_id == "failed-request-id"
    assert raised.value.errors == (
        "user identifier is invalid",
        "message is required",
    )
    assert "user identifier is invalid; message is required" in str(raised.value)


@pytest.mark.parametrize("status_code", [408, 425, 500, 503])
def test_classifies_retryable_statuses_as_transient(
    pushover_config: PushoverConfig, status_code: int
) -> None:
    client = make_client(
        pushover_config,
        lambda request: httpx.Response(
            status_code,
            json={"status": 0, "errors": ["try later"]},
        ),
    )

    with pytest.raises(TransientPushoverError) as raised:
        client.send_message("Page changed", "New content")

    assert raised.value.status_code == status_code
    assert raised.value.errors == ("try later",)


@pytest.mark.parametrize("status_code", [200, 400, 401, 429])
def test_classifies_rejected_requests_as_permanent(
    pushover_config: PushoverConfig, status_code: int
) -> None:
    client = make_client(
        pushover_config,
        lambda request: httpx.Response(
            status_code,
            json={"status": 0, "errors": ["request rejected"]},
        ),
    )

    with pytest.raises(PermanentPushoverError) as raised:
        client.send_message("Page changed", "New content")

    assert raised.value.status_code == status_code


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["unexpected"]),
        httpx.Response(200, json={"status": 1}),
    ],
)
def test_treats_malformed_success_responses_as_transient(
    pushover_config: PushoverConfig, response: httpx.Response
) -> None:
    client = make_client(pushover_config, lambda request: response)

    with pytest.raises(TransientPushoverError):
        client.send_message("Page changed", "New content")


def test_treats_malformed_client_error_as_permanent(
    pushover_config: PushoverConfig,
) -> None:
    client = make_client(
        pushover_config,
        lambda request: httpx.Response(400, content=b"not json"),
    )

    with pytest.raises(PermanentPushoverError):
        client.send_message("Page changed", "New content")


def test_wraps_transport_errors_as_transient(
    pushover_config: PushoverConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientPushoverError, match="request failed"):
        make_client(pushover_config, handler).send_message(
            "Page changed", "New content"
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"title": "", "message": "body"}, "title must not be empty"),
        (
            {"title": "x" * (MAX_TITLE_CHARACTERS + 1), "message": "body"},
            "title must not exceed",
        ),
        ({"title": "title", "message": " "}, "message must not be empty"),
        (
            {
                "title": "title",
                "message": "x" * (MAX_MESSAGE_CHARACTERS + 1),
            },
            "message must not exceed",
        ),
        (
            {
                "title": "title",
                "message": "body",
                "url": "x" * (MAX_URL_CHARACTERS + 1),
            },
            "url must not exceed",
        ),
        (
            {
                "title": "title",
                "message": "body",
                "url": "https://example.com",
                "url_title": "x" * (MAX_URL_TITLE_CHARACTERS + 1),
            },
            "url_title must not exceed",
        ),
    ],
)
def test_validates_documented_message_limits(
    pushover_config: PushoverConfig,
    arguments: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        make_client(pushover_config, lambda request: httpx.Response(200)).send_message(
            **arguments
        )


def test_rejects_url_title_without_url(pushover_config: PushoverConfig) -> None:
    with pytest.raises(ValueError, match="url_title requires url"):
        make_client(pushover_config, lambda request: httpx.Response(200)).send_message(
            "Page changed", "New content", url_title="Open"
        )


def test_validates_timeout(pushover_config: PushoverConfig) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        PushoverClient(pushover_config, timeout_seconds=0)


def test_context_manager_closes_owned_client(pushover_config: PushoverConfig) -> None:
    with PushoverClient(pushover_config) as client:
        http_client = client._client

    assert http_client.is_closed
