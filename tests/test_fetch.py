import httpx
import pytest

from pagewatcher.fetch import (
    DEFAULT_USER_AGENT,
    FetchStatus,
    FetchValidators,
    PageFetcher,
    PermanentFetchError,
    ResponseTooLargeError,
    TransientFetchError,
)


def make_fetcher(handler, *, max_response_bytes: int = 2_000_000) -> PageFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PageFetcher(client=client, max_response_bytes=max_response_bytes)


def test_fetches_content_with_expected_headers_and_validators() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/html,application/xhtml+xml"
        assert request.headers["user-agent"] == DEFAULT_USER_AGENT
        assert request.headers["user-agent"].startswith("Mozilla/5.0 ")
        assert "Chrome/153.0.0.0" in request.headers["user-agent"]
        return httpx.Response(
            200,
            content=b"<html>Available</html>",
            headers={
                "ETag": '"revision-2"',
                "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            },
        )

    result = make_fetcher(handler).fetch("https://example.com/product")

    assert result.status is FetchStatus.CONTENT
    assert result.not_modified is False
    assert result.final_url == "https://example.com/product"
    assert result.content == b"<html>Available</html>"
    assert result.validators == FetchValidators(
        etag='"revision-2"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
    )


def test_sends_conditional_headers_and_handles_not_modified() -> None:
    validators = FetchValidators(
        etag='"revision-1"',
        last_modified="Tue, 20 Oct 2015 07:28:00 GMT",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"revision-1"'
        assert request.headers["if-modified-since"] == validators.last_modified
        return httpx.Response(304, headers={"ETag": '"revision-2"'})

    result = make_fetcher(handler).fetch(
        "https://example.com/product", validators=validators
    )

    assert result.status is FetchStatus.NOT_MODIFIED
    assert result.not_modified is True
    assert result.content is None
    assert result.validators == FetchValidators(
        etag='"revision-2"',
        last_modified=validators.last_modified,
    )


def test_follows_redirects_and_reports_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "/new"})
        return httpx.Response(200, content=b"New page")

    result = make_fetcher(handler).fetch("https://example.com/old")

    assert result.content == b"New page"
    assert result.final_url == "https://example.com/new"


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
def test_classifies_retryable_http_statuses_as_transient(status_code: int) -> None:
    fetcher = make_fetcher(lambda request: httpx.Response(status_code))

    with pytest.raises(TransientFetchError) as raised:
        fetcher.fetch("https://example.com/product")

    assert raised.value.status_code == status_code


@pytest.mark.parametrize("status_code", [301, 400, 401, 404])
def test_classifies_other_error_statuses_as_permanent(status_code: int) -> None:
    fetcher = make_fetcher(lambda request: httpx.Response(status_code))

    with pytest.raises(PermanentFetchError) as raised:
        fetcher.fetch("https://example.com/product")

    assert raised.value.status_code == status_code


def test_wraps_transport_errors_as_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientFetchError, match="request failed"):
        make_fetcher(handler).fetch("https://example.com/product")


def test_rejects_declared_response_larger_than_limit() -> None:
    fetcher = make_fetcher(
        lambda request: httpx.Response(
            200,
            content=b"small",
            headers={"Content-Length": "100"},
        ),
        max_response_bytes=10,
    )

    with pytest.raises(ResponseTooLargeError, match="declared 100 bytes"):
        fetcher.fetch("https://example.com/product")


def test_rejects_streamed_response_larger_than_limit() -> None:
    fetcher = make_fetcher(
        lambda request: httpx.Response(200, stream=httpx.ByteStream(b"eleven bytes")),
        max_response_bytes=10,
    )

    with pytest.raises(ResponseTooLargeError, match="response exceeded"):
        fetcher.fetch("https://example.com/product")


def test_modified_response_does_not_reuse_stale_validators() -> None:
    result = make_fetcher(lambda request: httpx.Response(200, content=b"new")).fetch(
        "https://example.com/product",
        validators=FetchValidators(etag='"stale"', last_modified="yesterday"),
    )

    assert result.validators == FetchValidators()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"user_agent": " "}, "user_agent"),
    ],
)
def test_validates_fetcher_settings(arguments: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PageFetcher(**arguments)


def test_context_manager_closes_an_owned_client() -> None:
    with PageFetcher() as fetcher:
        client = fetcher._client

    assert client.is_closed
