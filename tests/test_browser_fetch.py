from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pagewatcher.browser_fetch import BrowserPageFetcher
from pagewatcher.fetch import (
    FetchStatus,
    FetchValidators,
    PermanentFetchError,
    ResponseTooLargeError,
    TransientFetchError,
)


URL = "https://example.com/product"


def make_fetcher(
    tmp_path: Path,
    *,
    response: Mock | None = None,
    content: str = "<html><body>Available</body></html>",
    **options: object,
) -> tuple[BrowserPageFetcher, Mock, Mock, Mock, Mock]:
    page = Mock()
    page.url = URL
    page.content.return_value = content
    page.goto.return_value = response or make_response()
    context = Mock()
    context.pages = []
    context.new_page.return_value = page
    chromium = Mock()
    chromium.launch_persistent_context.return_value = context
    playwright = Mock()
    playwright.chromium = chromium
    manager = Mock()
    manager.start.return_value = playwright
    factory = Mock(return_value=manager)
    fetcher = BrowserPageFetcher(
        profile_path=tmp_path / "browser-profile",
        playwright_factory=factory,
        **options,
    )
    return fetcher, factory, manager, chromium, page


def make_response(
    status: int = 200,
    *,
    headers: dict[str, str] | None = None,
) -> Mock:
    response = Mock()
    response.status = status
    response.headers = headers or {}
    return response


def test_fetches_rendered_content_with_browser_response_metadata(
    tmp_path: Path,
) -> None:
    response = make_response(
        headers={
            "etag": '"revision-2"',
            "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
        }
    )
    fetcher, factory, manager, chromium, page = make_fetcher(
        tmp_path,
        response=response,
        timeout_seconds=12.5,
        settle_seconds=1.25,
        headless=False,
    )

    result = fetcher.fetch(URL, validators=FetchValidators(etag='"old"'))

    assert result.status is FetchStatus.CONTENT
    assert result.final_url == URL
    assert result.content == b"<html><body>Available</body></html>"
    assert result.validators == FetchValidators(
        etag='"revision-2"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
    )
    assert (tmp_path / "browser-profile").is_dir()
    factory.assert_called_once_with()
    manager.start.assert_called_once_with()
    chromium.launch_persistent_context.assert_called_once_with(
        tmp_path / "browser-profile",
        channel="chromium",
        headless=False,
        accept_downloads=False,
    )
    page.goto.assert_called_once_with(URL, timeout=12_500, wait_until="load")
    page.wait_for_timeout.assert_called_once_with(1_250)


def test_reuses_browser_page_across_fetches(tmp_path: Path) -> None:
    fetcher, factory, manager, chromium, page = make_fetcher(tmp_path)

    fetcher.fetch(URL)
    fetcher.fetch("https://example.com/other")

    factory.assert_called_once_with()
    manager.start.assert_called_once_with()
    chromium.launch_persistent_context.assert_called_once()
    assert page.goto.call_count == 2


def test_uses_existing_persistent_context_page(tmp_path: Path) -> None:
    fetcher, _, _, chromium, page = make_fetcher(tmp_path)
    existing_page = Mock()
    existing_page.url = URL
    existing_page.goto.return_value = make_response()
    existing_page.content.return_value = "<html>Existing page</html>"
    context = chromium.launch_persistent_context.return_value
    context.pages = [existing_page]

    result = fetcher.fetch(URL)

    assert result.content == b"<html>Existing page</html>"
    context.new_page.assert_not_called()
    page.goto.assert_not_called()


def test_skips_settle_wait_when_configured_as_zero(tmp_path: Path) -> None:
    fetcher, _, _, _, page = make_fetcher(tmp_path, settle_seconds=0)

    fetcher.fetch(URL)

    page.wait_for_timeout.assert_not_called()


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
def test_classifies_retryable_http_statuses_as_transient(
    tmp_path: Path,
    status_code: int,
) -> None:
    fetcher, _, _, _, _ = make_fetcher(
        tmp_path, response=make_response(status_code)
    )

    with pytest.raises(TransientFetchError) as raised:
        fetcher.fetch(URL)

    assert raised.value.status_code == status_code


@pytest.mark.parametrize("status_code", [301, 400, 401, 403, 404])
def test_classifies_other_http_errors_as_permanent(
    tmp_path: Path,
    status_code: int,
) -> None:
    fetcher, _, _, _, _ = make_fetcher(
        tmp_path, response=make_response(status_code)
    )

    with pytest.raises(PermanentFetchError) as raised:
        fetcher.fetch(URL)

    assert raised.value.status_code == status_code


def test_wraps_navigation_timeout_as_transient(tmp_path: Path) -> None:
    fetcher, _, _, _, page = make_fetcher(tmp_path)
    page.goto.side_effect = PlaywrightTimeoutError("timed out")

    with pytest.raises(TransientFetchError, match="browser timed out"):
        fetcher.fetch(URL)


def test_wraps_navigation_error_as_transient(tmp_path: Path) -> None:
    fetcher, _, _, _, page = make_fetcher(tmp_path)
    page.goto.side_effect = PlaywrightError("connection reset")

    with pytest.raises(TransientFetchError, match="browser request failed"):
        fetcher.fetch(URL)


def test_reports_browser_start_failure_as_permanent(tmp_path: Path) -> None:
    fetcher, _, manager, chromium, _ = make_fetcher(tmp_path)
    chromium.launch_persistent_context.side_effect = PlaywrightError(
        "Executable doesn't exist"
    )

    with pytest.raises(PermanentFetchError, match="playwright install chromium"):
        fetcher.fetch(URL)

    manager.stop.assert_called_once_with()


def test_rejects_rendered_content_larger_than_limit(tmp_path: Path) -> None:
    fetcher, _, _, _, _ = make_fetcher(
        tmp_path,
        content="eleven bytes",
        max_response_bytes=10,
    )

    with pytest.raises(ResponseTooLargeError, match="10-byte limit"):
        fetcher.fetch(URL)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"settle_seconds": -1}, "settle_seconds"),
    ],
)
def test_validates_fetcher_settings(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        BrowserPageFetcher(profile_path=tmp_path, **arguments)


def test_context_manager_closes_context_and_driver(tmp_path: Path) -> None:
    fetcher, _, manager, chromium, _ = make_fetcher(tmp_path)
    context = chromium.launch_persistent_context.return_value

    with fetcher:
        fetcher.fetch(URL)

    context.close.assert_called_once_with()
    manager.stop.assert_called_once_with()


def test_close_without_starting_is_safe(tmp_path: Path) -> None:
    fetcher, factory, manager, _, _ = make_fetcher(tmp_path)

    fetcher.close()

    factory.assert_not_called()
    manager.stop.assert_not_called()
