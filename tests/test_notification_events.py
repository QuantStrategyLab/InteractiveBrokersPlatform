from notifications.events import (
    NotificationPublisher,
    RenderedNotification,
    publish_rendered_notification,
)


def test_publish_rendered_notification_splits_log_and_send_sinks():
    logged = []
    sent = []

    publish_rendered_notification(
        RenderedNotification(
            detailed_text="detailed copy",
            compact_text="compact copy",
        ),
        log_message=logged.append,
        send_message=sent.append,
    )

    assert logged == ["detailed copy"]
    assert sent == ["compact copy"]


def test_publish_rendered_notification_skips_empty_sinks():
    logged = []
    sent = []

    publish_rendered_notification(
        RenderedNotification(detailed_text="  ", compact_text=""),
        log_message=logged.append,
        send_message=sent.append,
    )

    assert logged == []
    assert sent == []


def test_notification_publisher_uses_configured_sinks():
    logged = []
    sent = []
    publisher = NotificationPublisher(
        log_message=logged.append,
        send_message=sent.append,
    )

    publisher.publish(
        RenderedNotification(
            detailed_text="detailed copy",
            compact_text="compact copy",
        )
    )

    assert logged == ["detailed copy"]
    assert sent == ["compact copy"]
