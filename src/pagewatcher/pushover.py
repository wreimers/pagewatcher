"""Client for sending notifications through the Pushover Message API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import TracebackType

import httpx

from pagewatcher.config import PushoverConfig

PUSHOVER_MESSAGES_ENDPOINT = "https://api.pushover.net/1/messages.json"
MAX_MESSAGE_CHARACTERS = 1_024
MAX_TITLE_CHARACTERS = 250
MAX_URL_CHARACTERS = 512
MAX_URL_TITLE_CHARACTERS = 100


@dataclass(frozen=True, slots=True)
class PushoverResponse:
    """Identifiers returned after Pushover accepts a message."""

    request_id: str


class PushoverError(RuntimeError):
    """Base class for expected Pushover delivery failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        errors: tuple[str, ...] = (),
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors
        self.request_id = request_id


class TransientPushoverError(PushoverError):
    """A Pushover failure that may succeed when retried later."""


class PermanentPushoverError(PushoverError):
    """A Pushover failure requiring a credential or request change."""


class PushoverClient:
    """Reusable synchronous client for normal-priority Pushover messages."""

    def __init__(
        self,
        config: PushoverConfig,
        *,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.config = config
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.Client()
        self._owns_client = client is None

    def send_message(
        self,
        title: str,
        message: str,
        *,
        url: str | None = None,
        url_title: str = "View page",
    ) -> PushoverResponse:
        """Send a normal-priority message and return its Pushover request ID."""

        _validate_message(title, message, url, url_title)
        data = {
            "token": self.config.app_token,
            "user": self.config.user_key,
            "title": title,
            "message": message,
            "priority": "0",
        }
        if self.config.device is not None:
            data["device"] = self.config.device
        if url is not None:
            data["url"] = url
            data["url_title"] = url_title

        try:
            response = self._client.post(
                PUSHOVER_MESSAGES_ENDPOINT,
                data=data,
                timeout=self.timeout_seconds,
            )
        except httpx.RequestError as error:
            raise TransientPushoverError(
                f"Pushover request failed: {error}"
            ) from error

        body = _response_body(response)
        if response.status_code == 200 and body.get("status") == 1:
            request_id = body.get("request")
            if isinstance(request_id, str) and request_id:
                return PushoverResponse(request_id=request_id)
            raise TransientPushoverError(
                "Pushover success response omitted its request ID",
                status_code=response.status_code,
            )

        errors = _response_errors(body)
        request_id = body.get("request")
        request_id = request_id if isinstance(request_id, str) else None
        detail = "; ".join(errors) if errors else "request was not accepted"
        message_text = f"Pushover returned HTTP {response.status_code}: {detail}"
        arguments = {
            "status_code": response.status_code,
            "errors": errors,
            "request_id": request_id,
        }
        if response.status_code in {408, 425} or response.status_code >= 500:
            raise TransientPushoverError(message_text, **arguments)
        raise PermanentPushoverError(message_text, **arguments)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PushoverClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _validate_message(
    title: str,
    message: str,
    url: str | None,
    url_title: str,
) -> None:
    if not title.strip():
        raise ValueError("title must not be empty")
    if len(title) > MAX_TITLE_CHARACTERS:
        raise ValueError(f"title must not exceed {MAX_TITLE_CHARACTERS} characters")
    if not message.strip():
        raise ValueError("message must not be empty")
    if len(message) > MAX_MESSAGE_CHARACTERS:
        raise ValueError(
            f"message must not exceed {MAX_MESSAGE_CHARACTERS} characters"
        )
    if url is None:
        if url_title != "View page":
            raise ValueError("url_title requires url")
        return
    if not url:
        raise ValueError("url must not be empty")
    if len(url) > MAX_URL_CHARACTERS:
        raise ValueError(f"url must not exceed {MAX_URL_CHARACTERS} characters")
    if not url_title:
        raise ValueError("url_title must not be empty")
    if len(url_title) > MAX_URL_TITLE_CHARACTERS:
        raise ValueError(
            f"url_title must not exceed {MAX_URL_TITLE_CHARACTERS} characters"
        )


def _response_body(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _unexpected_response_error(
            response, "Pushover returned an invalid JSON response"
        ) from error
    if not isinstance(body, dict):
        raise _unexpected_response_error(
            response, "Pushover returned an unexpected JSON response"
        )
    return body


def _response_errors(body: dict[str, object]) -> tuple[str, ...]:
    errors = body.get("errors")
    if not isinstance(errors, list):
        return ()
    return tuple(error for error in errors if isinstance(error, str))


def _unexpected_response_error(
    response: httpx.Response, message: str
) -> PushoverError:
    error_type = (
        TransientPushoverError
        if response.status_code in {200, 408, 425} or response.status_code >= 500
        else PermanentPushoverError
    )
    return error_type(message, status_code=response.status_code)
