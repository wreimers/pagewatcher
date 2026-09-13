"""HTTP client for retrieving watched pages safely and conditionally."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

import httpx

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)


class FetchStatus(StrEnum):
    """Possible successful outcomes of a page fetch."""

    CONTENT = "content"
    NOT_MODIFIED = "not_modified"


@dataclass(frozen=True, slots=True)
class FetchValidators:
    """HTTP validators retained between requests."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A successful fetch result, including validators for the next request."""

    status: FetchStatus
    final_url: str
    validators: FetchValidators
    content: bytes | None

    @property
    def not_modified(self) -> bool:
        return self.status is FetchStatus.NOT_MODIFIED


class FetchError(RuntimeError):
    """Base class for expected page-fetch failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TransientFetchError(FetchError):
    """A fetch failure that may succeed when retried later."""


class PermanentFetchError(FetchError):
    """A fetch failure that requires configuration or page changes."""


class ResponseTooLargeError(PermanentFetchError):
    """The response exceeded the configured memory-safety limit."""


class PageFetcher:
    """Reusable synchronous client for fetching a single watched page."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 2_000_000,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.Client | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be greater than zero")
        if not user_agent.strip():
            raise ValueError("user_agent must not be empty")

        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.user_agent = user_agent
        self._client = client or httpx.Client(http2=True)
        self._owns_client = client is None

    def fetch(
        self,
        url: str,
        *,
        validators: FetchValidators | None = None,
    ) -> FetchResult:
        """Fetch a page, returning content or a conditional 304 result."""

        previous_validators = validators or FetchValidators()
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": self.user_agent,
        }
        if previous_validators.etag:
            headers["If-None-Match"] = previous_validators.etag
        if previous_validators.last_modified:
            headers["If-Modified-Since"] = previous_validators.last_modified

        try:
            with self._client.stream(
                "GET",
                url,
                headers=headers,
                follow_redirects=True,
                timeout=self.timeout_seconds,
            ) as response:
                if response.status_code == 304:
                    return FetchResult(
                        status=FetchStatus.NOT_MODIFIED,
                        final_url=str(response.url),
                        validators=_response_validators(
                            response, fallback=previous_validators
                        ),
                        content=None,
                    )
                _raise_for_status(response)
                _check_declared_size(response, self.max_response_bytes)
                content = _read_limited(response, self.max_response_bytes)
                return FetchResult(
                    status=FetchStatus.CONTENT,
                    final_url=str(response.url),
                    validators=_response_validators(response),
                    content=content,
                )
        except httpx.RequestError as error:
            raise TransientFetchError(f"request failed for {url}: {error}") from error

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PageFetcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _response_validators(
    response: httpx.Response,
    *,
    fallback: FetchValidators | None = None,
) -> FetchValidators:
    return FetchValidators(
        etag=response.headers.get("etag")
        or (fallback.etag if fallback is not None else None),
        last_modified=response.headers.get("last-modified")
        or (fallback.last_modified if fallback is not None else None),
    )


def _raise_for_status(response: httpx.Response) -> None:
    status_code = response.status_code
    if 200 <= status_code < 300:
        return
    message = f"page returned HTTP {status_code} for {response.url}"
    if status_code in {408, 425, 429} or 500 <= status_code < 600:
        raise TransientFetchError(message, status_code=status_code)
    raise PermanentFetchError(message, status_code=status_code)


def _check_declared_size(response: httpx.Response, limit: int) -> None:
    value = response.headers.get("content-length")
    if value is None:
        return
    try:
        declared_size = int(value)
    except ValueError:
        return
    if declared_size > limit:
        raise ResponseTooLargeError(
            f"response declared {declared_size} bytes, exceeding the {limit}-byte limit",
            status_code=response.status_code,
        )


def _read_limited(response: httpx.Response, limit: int) -> bytes:
    content = bytearray()
    for chunk in response.iter_bytes():
        content.extend(chunk)
        if len(content) > limit:
            raise ResponseTooLargeError(
                f"response exceeded the {limit}-byte limit",
                status_code=response.status_code,
            )
    return bytes(content)
