"""Chromium-backed retrieval of rendered HTML pages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    PlaywrightContextManager,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from pagewatcher.fetch import (
    FetchResult,
    FetchStatus,
    FetchValidators,
    PermanentFetchError,
    ResponseTooLargeError,
    TransientFetchError,
)


class BrowserPageFetcher:
    """Fetch rendered HTML through persistent or per-fetch browser contexts."""

    def __init__(
        self,
        *,
        profile_path: str | Path = Path(".pagewatcher-browser"),
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 2_000_000,
        headless: bool = True,
        settle_seconds: float = 2.0,
        channel: str = "chromium",
        session_mode: str = "persistent",
        playwright_factory: Callable[[], PlaywrightContextManager] = sync_playwright,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be greater than zero")
        if settle_seconds < 0:
            raise ValueError("settle_seconds must not be negative")
        if not channel.strip():
            raise ValueError("channel must not be empty")
        normalized_session_mode = session_mode.strip().lower()
        if normalized_session_mode not in {"persistent", "ephemeral"}:
            raise ValueError("session_mode must be one of: persistent, ephemeral")

        self.profile_path = Path(profile_path).expanduser()
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.headless = headless
        self.settle_seconds = settle_seconds
        self.channel = channel.strip().lower()
        self.session_mode = normalized_session_mode
        self._playwright_factory = playwright_factory
        self._manager: PlaywrightContextManager | None = None
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def fetch(
        self,
        url: str,
        *,
        validators: FetchValidators | None = None,
    ) -> FetchResult:
        """Navigate to a page and return its rendered HTML document."""

        del validators  # Browser fetches return rendered content instead of 304s.
        if self.session_mode == "ephemeral":
            context, page = self._launch_page("")
            try:
                return self._fetch_page(url, page)
            finally:
                context.close()
        return self._fetch_page(url, self._ensure_page())

    def _fetch_page(self, url: str, page: Page) -> FetchResult:
        timeout_milliseconds = self.timeout_seconds * 1_000
        try:
            response = page.goto(
                url,
                timeout=timeout_milliseconds,
                wait_until="load",
            )
            if self.settle_seconds:
                page.wait_for_timeout(self.settle_seconds * 1_000)
            _raise_for_status(response, page.url)
            content = page.content().encode("utf-8")
        except PlaywrightTimeoutError as error:
            raise TransientFetchError(
                f"browser timed out fetching {url}: {error}"
            ) from error
        except PlaywrightError as error:
            raise TransientFetchError(
                f"browser request failed for {url}: {error}"
            ) from error

        if len(content) > self.max_response_bytes:
            raise ResponseTooLargeError(
                f"rendered response exceeded the {self.max_response_bytes}-byte limit",
                status_code=response.status if response is not None else None,
            )
        return FetchResult(
            status=FetchStatus.CONTENT,
            final_url=page.url,
            validators=_response_validators(response),
            content=content,
        )

    def close(self) -> None:
        """Close Chromium and its Playwright driver, if they were started."""

        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            self._page = None
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self._manager = None
                self._playwright = None

    def __enter__(self) -> BrowserPageFetcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _ensure_page(self) -> Page:
        if self._page is not None:
            return self._page

        self.profile_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        context, page = self._launch_page(self.profile_path)
        self._context = context
        self._page = page
        return page

    def _launch_page(self, profile_path: str | Path) -> tuple[BrowserContext, Page]:
        playwright = self._ensure_playwright()
        context: BrowserContext | None = None
        try:
            context = playwright.chromium.launch_persistent_context(
                profile_path,
                channel=self.channel,
                headless=self.headless,
                accept_downloads=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
            return context, page
        except PlaywrightError as error:
            if context is not None:
                context.close()
            self.close()
            raise self._browser_start_error(error) from error

    def _ensure_playwright(self) -> Playwright:
        if self._playwright is not None:
            return self._playwright

        manager = self._playwright_factory()
        self._manager = manager
        try:
            playwright = manager.start()
            self._playwright = playwright
        except PlaywrightError as error:
            self._manager = None
            raise self._browser_start_error(error) from error
        return playwright

    def _browser_start_error(self, error: PlaywrightError) -> PermanentFetchError:
        if self.channel == "chromium":
            guidance = "run 'python -m playwright install chromium'"
        else:
            guidance = f"ensure browser channel '{self.channel}' is installed"
        return PermanentFetchError(
            f"could not start browser; {guidance}: {error}"
        )


def _raise_for_status(response: Response | None, final_url: str) -> None:
    if response is None or response.status == 304:
        return
    status_code = response.status
    if 200 <= status_code < 300:
        return
    message = f"page returned HTTP {status_code} for {final_url}"
    if 400 <= status_code < 600:
        raise TransientFetchError(message, status_code=status_code)
    raise PermanentFetchError(message, status_code=status_code)


def _response_validators(response: Response | None) -> FetchValidators:
    if response is None:
        return FetchValidators()
    return FetchValidators(
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )
