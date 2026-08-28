import os
import unittest
from unittest.mock import patch

from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Deal, Listing
from used_pc_finder.notifications import (
    EmailNotifier,
    build_deal_email,
    send_unnotified_deals,
)


def deal() -> Deal:
    return Deal(
        Listing(
            "4070s 팝니다",
            520000,
            "https://www.daangn.com/kr/buy-sell/sample-4070-super",
            "Haan-dong",
            "local",
            "sample-4070-super",
        ),
        "RTX 4070 SUPER",
        600000,
        13.3,
    )


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send_deal(self, value):
        self.sent.append(value)


class FakeSMTP:
    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_arguments = None
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def ehlo(self):
        return None

    def starttls(self, context):
        self.started_tls = context is not None

    def login(self, sender, password):
        self.login_arguments = (sender, password)

    def send_message(self, message):
        self.messages.append(message)


class NotificationTests(unittest.TestCase):
    def test_email_contains_plain_text_and_html_deal_details(self):
        message = build_deal_email(deal(), "sender@example.com", "to@example.com")

        plain = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        for value in (
            "RTX 4070 SUPER",
            "520,000 KRW",
            "600,000 KRW",
            "13.3%",
            "Haan-dong",
            "local",
            "https://www.daangn.com/kr/buy-sell/sample-4070-super",
        ):
            self.assertIn(value, plain)
        self.assertIn('href="https://www.daangn.com/kr/buy-sell/sample-4070-super"', html)
        self.assertIn("Open original Karrot listing", html)
        self.assertEqual(message["From"], "sender@example.com")
        self.assertEqual(message["To"], "to@example.com")

    def test_smtp_notification_uses_environment_password_without_real_smtp(self):
        instances = []

        def factory(*args, **kwargs):
            client = FakeSMTP(*args, **kwargs)
            instances.append(client)
            return client

        notifier = EmailNotifier(
            "to@example.com",
            "smtp.example.com",
            587,
            "sender@example.com",
            smtp_factory=factory,
        )
        with patch.dict(os.environ, {"KARROT_SMTP_PASSWORD": "test-password"}):
            notifier.send_deal(deal())

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].login_arguments, ("sender@example.com", "test-password"))
        self.assertTrue(instances[0].started_tls)
        self.assertEqual(len(instances[0].messages), 1)

    def test_previously_notified_listing_is_not_sent_again(self):
        database = ListingDatabase(":memory:")
        self.addCleanup(database.close)
        database.initialize()
        value = deal()
        database.add(value.listing)
        notifier = RecordingNotifier()
        settings = {"enabled": True}

        self.assertEqual(send_unnotified_deals([value], database, settings, notifier), 1)
        self.assertTrue(database.was_notified(value.listing))
        self.assertEqual(send_unnotified_deals([value], database, settings, notifier), 0)
        self.assertEqual(notifier.sent, [value])


if __name__ == "__main__":
    unittest.main()
