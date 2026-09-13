from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from pagewatcher.apns import TransientApnsError
from pagewatcher.change import ChangeReason
from pagewatcher.config import ApnsConfig, NotificationProvider, WatcherConfig
from pagewatcher.fetch import FetchResult, FetchStatus, FetchValidators, PageFetcher
from pagewatcher.html import HtmlNormalizationError
from pagewatcher.notifier import NotificationResponse, NotificationSender
from pagewatcher.store import SqliteStateStore
from pagewatcher.watcher import PageWatcher, WatcherError, WatchOutcome


NOW = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)
URL = "https://example.com/product"


def make_config(tmp_path: Path, **overrides) -> WatcherConfig:
    private_key = tmp_path / "AuthKey_TEST.p8"
    private_key.write_text("unused", encoding="utf-8")
    config = WatcherConfig(
        url=URL,
        apns=ApnsConfig(
            team_id="TEAM123",
            key_id="KEY123",
            bundle_id="com.example.pagewatcher",
            device_token="device-token",
            private_key_path=private_key,
        ),
    )
    return replace(config, **overrides)


def content_result(
    html: str,
    *,
    etag: str | None = '"v2"',
    final_url: str = URL,
) -> FetchResult:
    return FetchResult(
        status=FetchStatus.CONTENT,
        final_url=final_url,
        validators=FetchValidators(etag=etag),
        content=html.encode(),
    )


def make_watcher(
    config: WatcherConfig,
    store: SqliteStateStore,
    fetch_result: FetchResult,
) -> tuple[PageWatcher, Mock, Mock]:
    fetcher = Mock(spec=PageFetcher)
    fetcher.fetch.return_value = fetch_result
    notifier = Mock(spec=NotificationSender)
    notifier.send_alert.return_value = NotificationResponse(
        NotificationProvider.APNS, "request-id"
    )
    watcher = PageWatcher(
        config,
        fetcher,
        store,
        notifier,
        clock=lambda: NOW,
    )
    return watcher, fetcher, notifier


def test_first_check_creates_baseline_without_notification(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SqliteStateStore(":memory:") as store:
        watcher, fetcher, notifier = make_watcher(
            config, store, content_result("<main>In stock</main>", etag='"v1"')
        )

        result = watcher.check_once()
        state = store.get(URL)

    assert result.outcome is WatchOutcome.BASELINE_CREATED
    assert result.assessment is not None
    assert result.assessment.reason is ChangeReason.BASELINE
    assert state is not None and state.snapshot_text == "In stock"
    assert state.validators.etag == '"v1"'
    assert state.checked_at == NOW
    fetcher.fetch.assert_called_once_with(URL, validators=None)
    notifier.send_alert.assert_not_called()


def test_conditional_not_modified_updates_only_check_metadata(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SqliteStateStore(":memory:") as store:
        original = store.save_snapshot(
            URL,
            "In stock",
            FetchValidators(etag='"v1"', last_modified="yesterday"),
            checked_at=NOW,
        )
        fetch_result = FetchResult(
            status=FetchStatus.NOT_MODIFIED,
            final_url=URL,
            validators=FetchValidators(etag='"v2"', last_modified="yesterday"),
            content=None,
        )
        watcher, fetcher, notifier = make_watcher(config, store, fetch_result)

        result = watcher.check_once()
        state = store.get(URL)

    assert result.outcome is WatchOutcome.HTTP_NOT_MODIFIED
    assert result.assessment is None
    assert state is not None and state.snapshot_hash == original.snapshot_hash
    assert state.validators.etag == '"v2"'
    fetcher.fetch.assert_called_once_with(URL, validators=original.validators)
    notifier.send_alert.assert_not_called()


def test_rejects_not_modified_response_without_baseline(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fetch_result = FetchResult(
        status=FetchStatus.NOT_MODIFIED,
        final_url=URL,
        validators=FetchValidators(),
        content=None,
    )
    with SqliteStateStore(":memory:") as store:
        watcher, _, _ = make_watcher(config, store, fetch_result)

        with pytest.raises(WatcherError, match="before a baseline"):
            watcher.check_once()


def test_identical_content_updates_validators_without_notification(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    with SqliteStateStore(":memory:") as store:
        original = store.save_snapshot(
            URL,
            "In stock",
            FetchValidators(etag='"v1"'),
            checked_at=NOW,
        )
        watcher, _, notifier = make_watcher(
            config, store, content_result("<p>In stock</p>", etag='"v2"')
        )

        result = watcher.check_once()
        state = store.get(URL)

    assert result.outcome is WatchOutcome.CONTENT_UNCHANGED
    assert result.assessment is not None
    assert result.assessment.reason is ChangeReason.IDENTICAL
    assert state is not None and state.snapshot_hash == original.snapshot_hash
    assert state.validators.etag == '"v2"'
    notifier.send_alert.assert_not_called()


def test_immaterial_change_preserves_comparison_snapshot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    baseline = "a" * 99 + "x"
    with SqliteStateStore(":memory:") as store:
        original = store.save_snapshot(
            URL, baseline, FetchValidators(etag='"v1"'), checked_at=NOW
        )
        watcher, _, notifier = make_watcher(
            config,
            store,
            content_result(f"<p>{'a' * 99}y</p>", etag='"v2"'),
        )

        result = watcher.check_once()
        state = store.get(URL)

    assert result.outcome is WatchOutcome.IMMATERIAL_CHANGE
    assert state is not None and state.snapshot_hash == original.snapshot_hash
    assert state.validators.etag == '"v2"'
    notifier.send_alert.assert_not_called()


def test_immaterial_changes_accumulate_against_baseline(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    baseline = "a" * 100
    with SqliteStateStore(":memory:") as store:
        store.save_snapshot(
            URL, baseline, FetchValidators(etag='"v1"'), checked_at=NOW
        )
        watcher, _, notifier = make_watcher(
            config,
            store,
            content_result(f"<p>{'a' * 99}b</p>", etag='"v2"'),
        )
        first = watcher.check_once()
        watcher.fetcher.fetch.return_value = content_result(
            f"<p>{'a' * 50}{'b' * 50}</p>", etag='"v3"'
        )

        second = watcher.check_once()

    assert first.outcome is WatchOutcome.IMMATERIAL_CHANGE
    assert second.outcome is WatchOutcome.NOTIFICATION_SENT
    notifier.send_alert.assert_called_once()


def test_material_change_notifies_then_advances_snapshot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SqliteStateStore(":memory:") as store:
        store.save_snapshot(
            URL, "In stock", FetchValidators(etag='"v1"'), checked_at=NOW
        )
        watcher, _, notifier = make_watcher(
            config,
            store,
            content_result(
                "<main>Sold out</main>",
                etag='"v2"',
                final_url="https://example.com/current-product",
            ),
        )

        result = watcher.check_once()
        state = store.get(URL)

    assert result.outcome is WatchOutcome.NOTIFICATION_SENT
    assert result.final_url == "https://example.com/current-product"
    assert result.notification == NotificationResponse(
        NotificationProvider.APNS, "request-id"
    )
    assert result.assessment is not None and result.assessment.is_material
    notifier.send_alert.assert_called_once_with(
        "Page changed: example.com",
        "Sold out",
        url=URL,
        deduplication_key=result.assessment.current_hash,
    )
    assert state is not None and state.snapshot_text == "Sold out"
    assert state.last_notified_hash == state.snapshot_hash


def test_notification_failure_preserves_snapshot_and_validators(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SqliteStateStore(":memory:") as store:
        original = store.save_snapshot(
            URL, "In stock", FetchValidators(etag='"v1"'), checked_at=NOW
        )
        watcher, _, notifier = make_watcher(
            config, store, content_result("<p>Sold out</p>", etag='"v2"')
        )
        notifier.send_alert.side_effect = TransientApnsError("try later")

        with pytest.raises(TransientApnsError, match="try later"):
            watcher.check_once()
        state = store.get(URL)

    assert state == original


def test_normalization_failure_preserves_state(tmp_path: Path) -> None:
    config = make_config(tmp_path, include_selectors=("#availability",))
    with SqliteStateStore(":memory:") as store:
        original = store.save_snapshot(
            URL, "In stock", FetchValidators(etag='"v1"'), checked_at=NOW
        )
        watcher, _, notifier = make_watcher(
            config, store, content_result("<p>Sold out</p>", etag='"v2"')
        )

        with pytest.raises(HtmlNormalizationError, match="matched no elements"):
            watcher.check_once()
        state = store.get(URL)

    assert state == original
    notifier.send_alert.assert_not_called()


def test_empty_material_content_uses_readable_notification_body(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    with SqliteStateStore(":memory:") as store:
        store.save_snapshot(
            URL, "In stock", FetchValidators(), checked_at=NOW
        )
        watcher, _, notifier = make_watcher(config, store, content_result("<body/>"))

        watcher.check_once()

    assert notifier.send_alert.call_args.args[1] == (
        "The monitored content is now empty."
    )


def test_long_content_is_truncated_for_notification(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with SqliteStateStore(":memory:") as store:
        store.save_snapshot(URL, "old", FetchValidators(), checked_at=NOW)
        watcher, _, notifier = make_watcher(
            config, store, content_result(f"<p>{'x' * 500}</p>")
        )

        watcher.check_once()

    body = notifier.send_alert.call_args.args[1]
    assert len(body) == 240
    assert body.endswith("...")
