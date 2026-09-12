"""Token-authenticated Apple Push Notification service client."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from urllib.parse import quote

import httpx
import jwt

from pagewatcher.config import ApnsConfig

APNS_PAYLOAD_LIMIT = 4_096
PROVIDER_TOKEN_LIFETIME_SECONDS = 50 * 60
SANDBOX_ENDPOINT = "https://api.sandbox.push.apple.com"
PRODUCTION_ENDPOINT = "https://api.push.apple.com"


@dataclass(frozen=True, slots=True)
class ApnsResponse:
    """Identifiers returned after APNs accepts a notification."""

    apns_id: str | None
    apns_unique_id: str | None


class ApnsError(RuntimeError):
    """Base class for expected APNs delivery failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
        apns_id: str | None = None,
        timestamp: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.apns_id = apns_id
        self.timestamp = timestamp


class TransientApnsError(ApnsError):
    """An APNs failure that may succeed when retried later."""


class PermanentApnsError(ApnsError):
    """An APNs failure requiring a credential, token, or payload change."""


class PayloadTooLargeError(PermanentApnsError):
    """The encoded notification exceeded APNs' alert payload limit."""


class ApnsClient:
    """Reusable HTTP/2 client for APNs alert notifications."""

    def __init__(
        self,
        config: ApnsConfig,
        *,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.config = config
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._private_key = config.private_key_path.read_text(encoding="utf-8")
        self._client = client or httpx.Client(http2=True)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_issued_at: int | None = None

    def send_alert(
        self,
        title: str,
        body: str,
        *,
        url: str | None = None,
        collapse_id: str | None = None,
    ) -> ApnsResponse:
        """Send a visible alert and return APNs request identifiers."""

        if not title.strip():
            raise ValueError("title must not be empty")
        if not body.strip():
            raise ValueError("body must not be empty")
        if collapse_id is not None:
            if not collapse_id or len(collapse_id.encode("utf-8")) > 64:
                raise ValueError("collapse_id must contain between 1 and 64 bytes")

        payload: dict[str, object] = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
            }
        }
        if url is not None:
            payload["url"] = url
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded_payload) > APNS_PAYLOAD_LIMIT:
            raise PayloadTooLargeError(
                f"notification payload is {len(encoded_payload)} bytes; "
                f"APNs allows {APNS_PAYLOAD_LIMIT}"
            )

        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": self.config.bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": "0",
            "content-type": "application/json",
        }
        if collapse_id is not None:
            headers["apns-collapse-id"] = collapse_id

        endpoint = SANDBOX_ENDPOINT if self.config.use_sandbox else PRODUCTION_ENDPOINT
        device_token = quote(self.config.device_token, safe="")
        try:
            response = self._client.post(
                f"{endpoint}/3/device/{device_token}",
                headers=headers,
                content=encoded_payload,
                timeout=self.timeout_seconds,
            )
        except httpx.RequestError as error:
            raise TransientApnsError(f"APNs request failed: {error}") from error

        if response.status_code == 200:
            return ApnsResponse(
                apns_id=response.headers.get("apns-id"),
                apns_unique_id=response.headers.get("apns-unique-id"),
            )
        self._raise_response_error(response)
        raise AssertionError("unreachable")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ApnsClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _provider_token(self) -> str:
        now = int(self._clock())
        token_age = None if self._token_issued_at is None else now - self._token_issued_at
        if (
            self._token is None
            or token_age is None
            or token_age < 0
            or token_age >= PROVIDER_TOKEN_LIFETIME_SECONDS
        ):
            self._token = jwt.encode(
                {"iss": self.config.team_id, "iat": now},
                self._private_key,
                algorithm="ES256",
                headers={"kid": self.config.key_id},
            )
            self._token_issued_at = now
        return self._token

    def _raise_response_error(self, response: httpx.Response) -> None:
        reason, timestamp = _error_details(response)
        message = f"APNs returned HTTP {response.status_code}"
        if reason is not None:
            message += f": {reason}"
        arguments = {
            "status_code": response.status_code,
            "reason": reason,
            "apns_id": response.headers.get("apns-id"),
            "timestamp": timestamp,
        }
        if reason == "ExpiredProviderToken":
            self._token = None
            self._token_issued_at = None
        if (
            response.status_code == 429
            or response.status_code >= 500
            or reason in {"ExpiredProviderToken", "IdleTimeout"}
        ):
            raise TransientApnsError(message, **arguments)
        raise PermanentApnsError(message, **arguments)


def _error_details(response: httpx.Response) -> tuple[str | None, int | None]:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    reason = body.get("reason")
    timestamp = body.get("timestamp")
    return (
        reason if isinstance(reason, str) else None,
        timestamp if isinstance(timestamp, int) else None,
    )
