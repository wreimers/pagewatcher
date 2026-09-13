"""Orchestrate one complete page check and notification cycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from urllib.parse import urlparse

from pagewatcher.apns import ApnsClient
from pagewatcher.change import ChangeAssessment, assess_change
from pagewatcher.config import WatcherConfig
from pagewatcher.fetch import FetchStatus, PageFetcher
from pagewatcher.html import normalize_html
from pagewatcher.notifier import NotificationResponse, NotificationSender, Notifier
from pagewatcher.store import SqliteStateStore

MAX_NOTIFICATION_BODY_CHARACTERS = 240


class WatchOutcome(StrEnum):
    """Successful outcomes from a single watcher check."""

    BASELINE_CREATED = "baseline_created"
    HTTP_NOT_MODIFIED = "http_not_modified"
    CONTENT_UNCHANGED = "content_unchanged"
    IMMATERIAL_CHANGE = "immaterial_change"
    NOTIFICATION_SENT = "notification_sent"


@dataclass(frozen=True, slots=True)
class WatchResult:
    """Details of a completed watcher check."""

    outcome: WatchOutcome
    final_url: str
    assessment: ChangeAssessment | None = None
    notification: NotificationResponse | None = None


class WatcherError(RuntimeError):
    """Raised when component results violate watcher state invariants."""


class PageWatcher:
    """Coordinate fetching, normalization, comparison, delivery, and storage."""

    def __init__(
        self,
        config: WatcherConfig,
        fetcher: PageFetcher,
        store: SqliteStateStore,
        notifier: NotificationSender | ApnsClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.fetcher = fetcher
        self.store = store
        self.notifier: NotificationSender = (
            Notifier(config, apns_client=notifier)
            if isinstance(notifier, ApnsClient)
            else notifier
        )
        self._clock = clock

    def check_once(self) -> WatchResult:
        """Run one check, raising component errors without mutating unsafe state."""

        state = self.store.get(self.config.url)
        fetch_result = self.fetcher.fetch(
            self.config.url,
            validators=state.validators if state is not None else None,
        )
        checked_at = self._clock()

        if fetch_result.status is FetchStatus.NOT_MODIFIED:
            if state is None:
                raise WatcherError("received HTTP 304 before a baseline was established")
            self.store.record_check(
                self.config.url,
                fetch_result.validators,
                checked_at=checked_at,
            )
            return WatchResult(
                outcome=WatchOutcome.HTTP_NOT_MODIFIED,
                final_url=fetch_result.final_url,
            )

        if fetch_result.content is None:
            raise WatcherError("content fetch result did not contain a response body")

        normalized = normalize_html(
            fetch_result.content,
            include_selectors=self.config.include_selectors,
            ignore_selectors=self.config.ignore_selectors,
        )
        assessment = assess_change(
            state.snapshot_text if state is not None else None,
            normalized,
            similarity_threshold=self.config.similarity_threshold,
            minimum_changed_characters=self.config.minimum_changed_characters,
        )

        if state is None:
            self.store.save_snapshot(
                self.config.url,
                normalized,
                fetch_result.validators,
                checked_at=checked_at,
            )
            return WatchResult(
                outcome=WatchOutcome.BASELINE_CREATED,
                final_url=fetch_result.final_url,
                assessment=assessment,
            )

        if not assessment.has_changed:
            self.store.record_check(
                self.config.url,
                fetch_result.validators,
                checked_at=checked_at,
            )
            return WatchResult(
                outcome=WatchOutcome.CONTENT_UNCHANGED,
                final_url=fetch_result.final_url,
                assessment=assessment,
            )

        if not assessment.is_material:
            self.store.record_check(
                self.config.url,
                fetch_result.validators,
                checked_at=checked_at,
            )
            return WatchResult(
                outcome=WatchOutcome.IMMATERIAL_CHANGE,
                final_url=fetch_result.final_url,
                assessment=assessment,
            )

        notification = self.notifier.send_alert(
            _notification_title(self.config.url),
            _notification_body(normalized),
            url=self.config.url,
            deduplication_key=assessment.current_hash,
        )
        self.store.save_snapshot(
            self.config.url,
            normalized,
            fetch_result.validators,
            notification_sent=True,
            checked_at=checked_at,
        )
        return WatchResult(
            outcome=WatchOutcome.NOTIFICATION_SENT,
            final_url=fetch_result.final_url,
            assessment=assessment,
            notification=notification,
        )


def _notification_title(url: str) -> str:
    hostname = urlparse(url).hostname
    return f"Page changed: {hostname}" if hostname else "Page changed"


def _notification_body(normalized: str) -> str:
    if not normalized:
        return "The monitored content is now empty."
    if len(normalized) <= MAX_NOTIFICATION_BODY_CHARACTERS:
        return normalized
    return normalized[: MAX_NOTIFICATION_BODY_CHARACTERS - 3].rstrip() + "..."
