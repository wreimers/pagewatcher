"""Command-line interface for configuring and running pagewatcher."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from jwt import PyJWTError

from pagewatcher.apns import ApnsError, TransientApnsError
from pagewatcher.config import ConfigError, WatcherConfig
from pagewatcher.fetch import FetchError, PageFetcher, TransientFetchError
from pagewatcher.html import HtmlNormalizationError
from pagewatcher.notifier import Notifier
from pagewatcher.pushover import PushoverError, TransientPushoverError
from pagewatcher.store import SqliteStateStore
from pagewatcher.watcher import PageWatcher, WatcherError, WatchResult


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected command and return a process exit status."""

    arguments = _parser().parse_args(argv)
    try:
        config = WatcherConfig.from_env()
        if arguments.command == "validate":
            print(f"Configuration is valid for {config.url}")
            return 0
        if arguments.command == "test-notification":
            _send_test_notification(config)
            return 0
        if arguments.command == "check":
            with _open_watcher(config) as watcher:
                _print_result(watcher.check_once())
            return 0
        if arguments.command == "watch":
            with _open_watcher(config) as watcher:
                _watch_forever(watcher, config.poll_interval_seconds)
            return 0
        raise AssertionError(f"unhandled command: {arguments.command}")
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except (
        FetchError,
        HtmlNormalizationError,
        ApnsError,
        PushoverError,
        WatcherError,
        PyJWTError,
        sqlite3.Error,
        OSError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pagewatcher",
        description="Monitor an HTML page and notify when it changes.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate environment configuration")
    commands.add_parser("check", help="perform one page check")
    commands.add_parser("watch", help="check continuously at the configured interval")
    commands.add_parser(
        "test-notification",
        help="send a test notification without fetching the page",
    )
    return parser


@contextmanager
def _open_watcher(config: WatcherConfig) -> Iterator[PageWatcher]:
    with (
        PageFetcher(
            timeout_seconds=config.request_timeout_seconds,
            max_response_bytes=config.max_response_bytes,
        ) as fetcher,
        SqliteStateStore(config.database_path) as store,
        Notifier(config) as notifier,
    ):
        yield PageWatcher(config, fetcher, store, notifier)


def _watch_forever(watcher: PageWatcher, interval_seconds: float) -> None:
    while True:
        try:
            _print_result(watcher.check_once())
        except (
            TransientFetchError,
            TransientApnsError,
            TransientPushoverError,
        ) as error:
            print(f"Transient error; will retry: {error}", file=sys.stderr)
        time.sleep(interval_seconds)


def _send_test_notification(config: WatcherConfig) -> None:
    with Notifier(config) as notifier:
        response = notifier.send_alert(
            "Pagewatcher test",
            "Notifications are configured correctly.",
            url=config.url,
            deduplication_key="pagewatcher-test",
        )
    identifier = response.request_id or "not provided"
    print(
        f"Test notification accepted by {response.provider.value} "
        f"(request-id: {identifier})"
    )


def _print_result(result: WatchResult) -> None:
    detail = ""
    if result.assessment is not None and result.assessment.similarity is not None:
        detail = (
            f", similarity={result.assessment.similarity:.4f}, "
            f"changed_characters={result.assessment.changed_characters}"
        )
    print(f"Check complete: {result.outcome.value}{detail}")
