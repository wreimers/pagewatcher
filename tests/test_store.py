import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pagewatcher.fetch import FetchValidators
from pagewatcher.store import SqliteStateStore


NOW = datetime(2026, 9, 12, 18, 30, tzinfo=timezone.utc)


def test_get_returns_none_for_unknown_url() -> None:
    with SqliteStateStore(":memory:") as store:
        assert store.get("https://example.com/missing") is None


def test_saves_and_reads_a_baseline_snapshot() -> None:
    validators = FetchValidators(etag='"v1"', last_modified="yesterday")
    with SqliteStateStore(":memory:") as store:
        saved = store.save_snapshot(
            "https://example.com/product",
            "In stock",
            validators,
            checked_at=NOW,
        )

        loaded = store.get("https://example.com/product")

    assert loaded == saved
    assert loaded is not None
    assert loaded.snapshot_text == "In stock"
    assert loaded.snapshot_hash == hashlib.sha256(b"In stock").hexdigest()
    assert loaded.validators == validators
    assert loaded.checked_at == NOW
    assert loaded.last_notified_hash is None


def test_advances_snapshot_and_records_successful_notification() -> None:
    with SqliteStateStore(":memory:") as store:
        store.save_snapshot(
            "https://example.com/product",
            "In stock",
            FetchValidators(etag='"v1"'),
            checked_at=NOW,
        )

        state = store.save_snapshot(
            "https://example.com/product",
            "Sold out",
            FetchValidators(etag='"v2"'),
            notification_sent=True,
            checked_at=NOW + timedelta(minutes=5),
        )

    assert state.snapshot_text == "Sold out"
    assert state.validators.etag == '"v2"'
    assert state.last_notified_hash == state.snapshot_hash


def test_snapshot_without_notification_preserves_prior_notification_hash() -> None:
    url = "https://example.com/product"
    with SqliteStateStore(":memory:") as store:
        notified = store.save_snapshot(
            url,
            "Sold out",
            FetchValidators(),
            notification_sent=True,
            checked_at=NOW,
        )
        updated = store.save_snapshot(
            url,
            "Back soon",
            FetchValidators(),
            checked_at=NOW + timedelta(minutes=5),
        )

    assert updated.last_notified_hash == notified.snapshot_hash
    assert updated.snapshot_hash != notified.snapshot_hash


def test_record_check_does_not_advance_comparison_snapshot() -> None:
    url = "https://example.com/product"
    with SqliteStateStore(":memory:") as store:
        original = store.save_snapshot(
            url,
            "In stock",
            FetchValidators(etag='"v1"'),
            checked_at=NOW,
        )

        checked = store.record_check(
            url,
            FetchValidators(etag='"v2"', last_modified="today"),
            checked_at=NOW + timedelta(minutes=5),
        )

    assert checked.snapshot_text == original.snapshot_text
    assert checked.snapshot_hash == original.snapshot_hash
    assert checked.last_notified_hash == original.last_notified_hash
    assert checked.validators == FetchValidators(etag='"v2"', last_modified="today")
    assert checked.checked_at == NOW + timedelta(minutes=5)


def test_record_check_requires_existing_state() -> None:
    with SqliteStateStore(":memory:") as store:
        with pytest.raises(KeyError, match="no state exists"):
            store.record_check(
                "https://example.com/missing",
                FetchValidators(),
                checked_at=NOW,
            )


def test_state_persists_across_store_instances(tmp_path: Path) -> None:
    database = tmp_path / "state" / "pagewatcher.sqlite3"
    url = "https://example.com/product"
    with SqliteStateStore(database) as store:
        expected = store.save_snapshot(
            url,
            "Available",
            FetchValidators(etag='"v1"'),
            checked_at=NOW,
        )

    with SqliteStateStore(database) as reopened:
        assert reopened.get(url) == expected


def test_normalizes_timestamps_to_utc() -> None:
    pacific = timezone(timedelta(hours=-7))
    local_time = datetime(2026, 9, 12, 11, 30, tzinfo=pacific)
    with SqliteStateStore(":memory:") as store:
        state = store.save_snapshot(
            "https://example.com/product",
            "Available",
            FetchValidators(),
            checked_at=local_time,
        )

    assert state.checked_at == NOW
    assert state.checked_at.tzinfo == timezone.utc


def test_rejects_naive_timestamps() -> None:
    with SqliteStateStore(":memory:") as store:
        with pytest.raises(ValueError, match="timezone-aware"):
            store.save_snapshot(
                "https://example.com/product",
                "Available",
                FetchValidators(),
                checked_at=datetime(2026, 9, 12, 18, 30),
            )


def test_context_manager_closes_connection() -> None:
    with SqliteStateStore(":memory:") as store:
        connection = store._connection

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
