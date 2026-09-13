"""Provider-neutral notification delivery."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, cast, runtime_checkable

from pagewatcher.apns import ApnsClient
from pagewatcher.config import NotificationProvider, WatcherConfig
from pagewatcher.pushover import PushoverClient


@dataclass(frozen=True, slots=True)
class NotificationResponse:
    """A provider-neutral accepted-notification identifier."""

    provider: NotificationProvider
    request_id: str | None


@runtime_checkable
class NotificationSender(Protocol):
    """Interface consumed by the page watcher."""

    def send_alert(
        self,
        title: str,
        body: str,
        *,
        url: str | None = None,
        deduplication_key: str | None = None,
    ) -> NotificationResponse:
        """Send an alert through the configured provider."""


class Notifier:
    """Select and adapt the notification provider configured for a watcher."""

    def __init__(
        self,
        config: WatcherConfig,
        *,
        apns_client: ApnsClient | None = None,
        pushover_client: PushoverClient | None = None,
    ) -> None:
        self.provider = config.notification_provider
        if self.provider is NotificationProvider.APNS:
            if config.apns is None:
                raise ValueError("APNs configuration is required for the APNs provider")
            if pushover_client is not None:
                raise ValueError("a Pushover client cannot be used with the APNs provider")
            self._client: ApnsClient | PushoverClient = apns_client or ApnsClient(
                config.apns
            )
            self._owns_client = apns_client is None
        elif self.provider is NotificationProvider.PUSHOVER:
            if config.pushover is None:
                raise ValueError(
                    "Pushover configuration is required for the Pushover provider"
                )
            if apns_client is not None:
                raise ValueError("an APNs client cannot be used with the Pushover provider")
            self._client = pushover_client or PushoverClient(config.pushover)
            self._owns_client = pushover_client is None
        else:
            raise ValueError(f"unsupported notification provider: {self.provider}")

    def send_alert(
        self,
        title: str,
        body: str,
        *,
        url: str | None = None,
        deduplication_key: str | None = None,
    ) -> NotificationResponse:
        """Send an alert and normalize the provider's response."""

        if self.provider is NotificationProvider.APNS:
            client = cast(ApnsClient, self._client)
            response = client.send_alert(
                title,
                body,
                url=url,
                collapse_id=deduplication_key,
            )
            return NotificationResponse(
                provider=self.provider,
                request_id=response.apns_id,
            )

        client = cast(PushoverClient, self._client)
        response = client.send_message(
            title,
            body,
            url=url,
            url_title="View monitored page",
        )
        return NotificationResponse(
            provider=self.provider,
            request_id=response.request_id,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Notifier:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
