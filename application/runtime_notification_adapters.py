"""Builder helpers for IBKR runtime notification adapters."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from notifications.events import NotificationPublisher, RenderedNotification
from quant_platform_kit.common.port_adapters import CallableNotificationPort
from quant_platform_kit.common.ports import NotificationPort


@dataclass(frozen=True)
class IBKRNotificationAdapters:
    notification_port: NotificationPort
    cycle_publisher: NotificationPublisher
    delivery_events: list[dict[str, Any]]

    def publish_cycle_notification(self, *, detailed_text: str, compact_text: str) -> None:
        self.cycle_publisher.publish(
            RenderedNotification(
                detailed_text=detailed_text,
                compact_text=compact_text,
            )
        )


def build_runtime_notification_adapters(
    *,
    send_message,
    log_message=None,
    delivery_events: list[dict[str, Any]] | None = None,
) -> IBKRNotificationAdapters:
    recorded_delivery_events = delivery_events if delivery_events is not None else []

    def send_recorded_message(message: str) -> None:
        send_message(message)
        compact = str(message or "")
        recorded_delivery_events.append(
            {
                "sink": "telegram",
                "delivery_status": "sent",
                "compact_text_sha256": hashlib.sha256(compact.encode("utf-8")).hexdigest(),
                "compact_text_length": len(compact),
            }
        )

    return IBKRNotificationAdapters(
        notification_port=CallableNotificationPort(send_recorded_message),
        cycle_publisher=NotificationPublisher(
            log_message=log_message or (lambda message: print(message, flush=True)),
            send_message=send_recorded_message,
        ),
        delivery_events=recorded_delivery_events,
    )
