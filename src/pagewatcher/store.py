"""SQLite persistence for page snapshots and HTTP validators."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from pagewatcher.fetch import FetchValidators


@dataclass(frozen=True, slots=True)
class PageState:
    """Durable comparison state for one watched URL."""

    url: str
    snapshot_text: str
    snapshot_hash: str
    validators: FetchValidators
    checked_at: datetime
    last_notified_hash: str | None


class SqliteStateStore:
    """Transactional state storage keyed by watched URL."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path) if path != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def get(self, url: str) -> PageState | None:
        """Return the saved state for a URL, if one exists."""

        row = self._connection.execute(
            """
            SELECT url, snapshot_text, snapshot_hash, etag, last_modified,
                   checked_at, last_notified_hash
            FROM page_state
            WHERE url = ?
            """,
            (url,),
        ).fetchone()
        if row is None:
            return None
        return _page_state_from_row(row)

    def save_snapshot(
        self,
        url: str,
        snapshot_text: str,
        validators: FetchValidators,
        *,
        notification_sent: bool = False,
        checked_at: datetime | None = None,
    ) -> PageState:
        """Atomically insert or advance a URL's comparison snapshot.

        A successful notification records the new snapshot hash. Saving without a
        notification preserves any prior notification hash.
        """

        timestamp = _timestamp(checked_at)
        snapshot_hash = _snapshot_hash(snapshot_text)
        notified_hash = snapshot_hash if notification_sent else None
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO page_state (
                    url, snapshot_text, snapshot_hash, etag, last_modified,
                    checked_at, last_notified_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    snapshot_text = excluded.snapshot_text,
                    snapshot_hash = excluded.snapshot_hash,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    checked_at = excluded.checked_at,
                    last_notified_hash = COALESCE(
                        excluded.last_notified_hash,
                        page_state.last_notified_hash
                    )
                """,
                (
                    url,
                    snapshot_text,
                    snapshot_hash,
                    validators.etag,
                    validators.last_modified,
                    timestamp,
                    notified_hash,
                ),
            )
        state = self.get(url)
        assert state is not None
        return state

    def record_check(
        self,
        url: str,
        validators: FetchValidators,
        *,
        checked_at: datetime | None = None,
    ) -> PageState:
        """Update check metadata without advancing the comparison snapshot."""

        timestamp = _timestamp(checked_at)
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE page_state
                SET etag = ?, last_modified = ?, checked_at = ?
                WHERE url = ?
                """,
                (validators.etag, validators.last_modified, timestamp, url),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"no state exists for URL: {url}")
        state = self.get(url)
        assert state is not None
        return state

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SqliteStateStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS page_state (
                    url TEXT PRIMARY KEY,
                    snapshot_text TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    etag TEXT,
                    last_modified TEXT,
                    checked_at TEXT NOT NULL,
                    last_notified_hash TEXT
                )
                """
            )


def _page_state_from_row(row: sqlite3.Row) -> PageState:
    return PageState(
        url=row["url"],
        snapshot_text=row["snapshot_text"],
        snapshot_hash=row["snapshot_hash"],
        validators=FetchValidators(
            etag=row["etag"],
            last_modified=row["last_modified"],
        ),
        checked_at=datetime.fromisoformat(row["checked_at"]),
        last_notified_hash=row["last_notified_hash"],
    )


def _timestamp(value: datetime | None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    return timestamp.astimezone(timezone.utc).isoformat()


def _snapshot_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
