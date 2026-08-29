import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Deal, Listing
from used_pc_finder.notifications import (
    EmailNotifier,
    bunjang_listing_url,
    build_deal_digest_email,
    send_unnotified_deal_digest,
)
from used_pc_finder.secrets import SMTP_PASSWORD_ENVIRONMENT_VARIABLE, load_smtp_password, setup_smtp_password


def deal() -> Deal:
    return Deal(
        Listing(
            "4070s 팝니다",
            520000,
            "https://m.bunjang.co.kr/products/4070-super",
            "Haan-dong",
            "bunjang_search",
            "bunjang:4070-super",
            marketplace="bunjang",
            product_id="4070-super",
            canonical_url="https://m.bunjang.co.kr/products/canonical-4070-super",
            ai_scope="standalone",
        ),
        "RTX 4070 SUPER",
        600000,
        13.3,
    )


def second_deal() -> Deal:
    return Deal(
        Listing(
            "3060 Ti 팝니다",
            240000,
            "https://m.bunjang.co.kr/products/3060-ti",
            "Gwangmyeong",
            "bunjang_search",
            "bunjang:3060-ti",
            condition_status="normal",
            ai_is_computer_part=True,
            ai_normalized_product_name="RTX 3060 Ti",
            ai_confidence=0.97,
            marketplace="bunjang",
            product_id="3060-ti",
            canonical_url="https://m.bunjang.co.kr/products/canonical-3060-ti",
            ai_scope="standalone",
        ),
        "RTX 3060 Ti",
        300000,
        20.0,
    )


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send_digest(self, values, pricing_sources):
        self.sent.append((list(values), dict(pricing_sources)))


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
    def test_hidden_setup_writes_mode_600_secret_and_loader_sets_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "used_pc_finder" / "secrets.env"
            setup_smtp_password(path, prompt=lambda _prompt: "test-app-password")
            environment: dict[str, str] = {}

            self.assertTrue(load_smtp_password(path, environment))
            self.assertEqual(environment[SMTP_PASSWORD_ENVIRONMENT_VARIABLE], "test-app-password")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_secret_loader_preserves_an_explicit_environment_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.env"
            setup_smtp_password(path, prompt=lambda _prompt: "saved-password")
            environment = {SMTP_PASSWORD_ENVIRONMENT_VARIABLE: "explicit-password"}

            self.assertTrue(load_smtp_password(path, environment))
            self.assertEqual(environment[SMTP_PASSWORD_ENVIRONMENT_VARIABLE], "explicit-password")

    def test_digest_contains_required_fields_and_sorts_highest_discount_first(self):
        message = build_deal_digest_email(
            [deal(), second_deal()],
            {"RTX 4070 SUPER": "manual", "RTX 3060 Ti": "automatic"},
            "sender@example.com",
            "to@example.com",
        )

        plain = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        for value in (
            "RTX 4070 SUPER",
            "RTX 3060 Ti",
            "520,000 KRW",
            "600,000 KRW",
            "13.3%",
            "20.0%",
            "normal",
            "97.0%",
            "automatic",
            "https://m.bunjang.co.kr/products/canonical-4070-super",
            "https://m.bunjang.co.kr/products/canonical-3060-ti",
        ):
            self.assertIn(value, plain)
        self.assertIn('href="https://m.bunjang.co.kr/products/canonical-4070-super"', html)
        self.assertIn('href="https://m.bunjang.co.kr/products/canonical-3060-ti"', html)
        self.assertIn("Open listing", html)
        self.assertIn("Bunjang computer-parts deal digest", plain)
        self.assertIn("Bunjang computer-parts deal digest", html)
        self.assertLess(html.index("RTX 3060 Ti"), html.index("RTX 4070 SUPER"))
        self.assertNotIn("Karrot", plain)
        self.assertNotIn("Daangn", plain)
        self.assertNotIn("Karrot", html)
        self.assertNotIn("Daangn", html)
        self.assertTrue(message["Subject"].startswith("Bunjang deal digest: 2"))
        self.assertEqual(message["From"], "sender@example.com")
        self.assertEqual(message["To"], "to@example.com")

    def test_bunjang_url_falls_back_to_the_product_id_not_a_legacy_url(self):
        listing = Listing(
            "RTX 4070 SUPER",
            520000,
            "https://www.daangn.com/kr/buy-sell/legacy-link",
            "",
            "bunjang_search",
            marketplace="bunjang",
            product_id="123456789",
        )
        value = Deal(listing, "RTX 4070 SUPER", 600000, 13.3)

        self.assertEqual(
            bunjang_listing_url(value),
            "https://m.bunjang.co.kr/products/123456789",
        )

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
            notifier.send_digest([deal(), second_deal()], {"RTX 4070 SUPER": "manual"})

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].login_arguments, ("sender@example.com", "test-password"))
        self.assertTrue(instances[0].started_tls)
        self.assertEqual(len(instances[0].messages), 1)

    def test_one_digest_marks_all_listings_and_excludes_already_notified_listings(self):
        database = ListingDatabase(":memory:")
        self.addCleanup(database.close)
        database.initialize()
        first = deal()
        second = second_deal()
        database.add(first.listing)
        database.add(second.listing)
        notifier = RecordingNotifier()
        settings = {"enabled": True}

        self.assertEqual(
            send_unnotified_deal_digest(
                [first, second], database, settings,
                {"RTX 4070 SUPER": "manual", "RTX 3060 Ti": "automatic"}, notifier,
            ),
            2,
        )
        self.assertTrue(database.was_notified(first.listing))
        self.assertTrue(database.was_notified(second.listing))
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][0], [first, second])
        self.assertEqual(
            send_unnotified_deal_digest(
                [first, second], database, settings,
                {"RTX 4070 SUPER": "manual", "RTX 3060 Ti": "automatic"}, notifier,
            ),
            0,
        )
        self.assertEqual(len(notifier.sent), 1)

    def test_smtp_failure_does_not_mark_any_digest_listing_notified(self):
        class FailingNotifier:
            def send_digest(self, _values, _pricing_sources):
                raise RuntimeError("SMTP unavailable")

        database = ListingDatabase(":memory:")
        self.addCleanup(database.close)
        database.initialize()
        first = deal()
        second = second_deal()
        database.add(first.listing)
        database.add(second.listing)

        self.assertEqual(
            send_unnotified_deal_digest(
                [first, second], database, {"enabled": True}, {}, FailingNotifier()
            ),
            0,
        )
        self.assertFalse(database.was_notified(first.listing))
        self.assertFalse(database.was_notified(second.listing))

    def test_empty_digest_is_not_sent(self):
        database = ListingDatabase(":memory:")
        self.addCleanup(database.close)
        database.initialize()
        notifier = RecordingNotifier()

        self.assertEqual(
            send_unnotified_deal_digest([], database, {"enabled": True}, {}, notifier), 0
        )
        self.assertEqual(notifier.sent, [])

    def test_sold_reserved_and_nonstandalone_deals_are_never_emailed(self):
        database = ListingDatabase(":memory:")
        self.addCleanup(database.close)
        database.initialize()
        active = deal()
        blocked = [
            Deal(replace(active.listing, product_id="sold", url="https://m.bunjang.co.kr/products/sold", listing_id="bunjang:sold", listing_status="sold"), active.normalized_name, active.reference_price, active.discount_percent),
            Deal(replace(active.listing, product_id="reserved", url="https://m.bunjang.co.kr/products/reserved", listing_id="bunjang:reserved", listing_status="reserved"), active.normalized_name, active.reference_price, active.discount_percent),
            Deal(replace(active.listing, product_id="bundle", url="https://m.bunjang.co.kr/products/bundle", listing_id="bunjang:bundle", ai_scope="bundle"), active.normalized_name, active.reference_price, active.discount_percent),
        ]
        for value in [active, *blocked]:
            database.add(value.listing)
        notifier = RecordingNotifier()
        self.assertEqual(
            send_unnotified_deal_digest([active, *blocked], database, {"enabled": True}, {}, notifier),
            1,
        )
        self.assertEqual(notifier.sent[0][0], [active])


if __name__ == "__main__":
    unittest.main()
