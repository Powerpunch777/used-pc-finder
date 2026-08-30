"""One-email-per-scan Bunjang bargain digest notifications."""

from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from collections.abc import Callable, Mapping, Sequence
from email.message import EmailMessage
from typing import Any, Protocol

from .database import ListingDatabase
from .models import Deal

LOGGER = logging.getLogger(__name__)
DEFAULT_PASSWORD_ENVIRONMENT_VARIABLE = "KARROT_SMTP_PASSWORD"
BJUNJANG_PRODUCT_URL = "https://m.bunjang.co.kr/products/{product_id}"


class Notifier(Protocol):
    def send_digest(
        self, deals: Sequence[Deal], pricing_sources: Mapping[str, str],
        review_metadata: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None: ...


# Compatibility name for injected test and third-party adapters.
DigestNotifier = Notifier


def bunjang_listing_url(deal: Deal) -> str:
    """Return the Bunjang canonical link and never fall back to a legacy URL."""
    listing = deal.listing
    if listing.marketplace != "bunjang":
        raise ValueError("Bunjang notifications require a Bunjang listing")
    if listing.canonical_url:
        return listing.canonical_url
    if not listing.product_id:
        raise ValueError("Bunjang notifications require a product_id")
    return BJUNJANG_PRODUCT_URL.format(product_id=listing.product_id)


def _confidence_text(deal: Deal) -> str:
    confidence = deal.listing.ai_confidence
    return f"{confidence * 100:.1f}%" if confidence is not None else "Not available"


def _sorted_deals(deals: Sequence[Deal]) -> list[Deal]:
    return sorted(deals, key=lambda deal: deal.discount_percent, reverse=True)


def build_deal_digest_email(
    deals: Sequence[Deal],
    pricing_sources: Mapping[str, str],
    sender: str,
    recipient: str,
    review_metadata: Mapping[str, Mapping[str, object]] | None = None,
) -> EmailMessage:
    """Build one multipart Bunjang digest, ordered by highest discount first."""
    ordered_deals = _sorted_deals(deals)
    if not ordered_deals:
        raise ValueError("A Bunjang digest requires at least one deal")

    text_lines = ["Bunjang computer-parts deal digest", ""]
    html_rows: list[str] = []
    for index, deal in enumerate(ordered_deals, start=1):
        listing = deal.listing
        listing_url = bunjang_listing_url(deal)
        pricing_source = pricing_sources.get(deal.normalized_name, "manual")
        review = (review_metadata or {}).get(listing.product_id or "", {})
        second_confidence = review.get("second_stage_confidence")
        second_confidence_text = (
            f"{float(second_confidence) * 100:.1f}%"
            if isinstance(second_confidence, (int, float)) and not isinstance(second_confidence, bool)
            else "Not available"
        )
        review_reason = str(review.get("reason", "")).strip() or "Not available"
        fields = (
            ("Product", deal.normalized_name),
            ("Displayed listing price", f"{listing.price:,} KRW"),
            ("Effective price", f"{(deal.effective_price or listing.price):,} KRW"),
            ("Reference market price", f"{deal.reference_price:,} KRW"),
            ("Discount", f"{deal.discount_percent:.1f}%"),
            ("Condition", listing.condition_status),
            ("First-stage AI confidence", _confidence_text(deal)),
            ("Second-stage AI confidence", second_confidence_text),
            ("Final review reason", review_reason),
            ("Pricing source", pricing_source),
        )
        text_lines.append(f"{index}. {deal.normalized_name}")
        text_lines.extend(f"   {label}: {value}" for label, value in fields[1:])
        text_lines.extend((f"   Bunjang listing: {listing_url}", ""))
        html_rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{html.escape(deal.normalized_name)}</td>"
            f"<td>{listing.price:,} KRW</td>"
            f"<td>{(deal.effective_price or listing.price):,} KRW</td>"
            f"<td>{deal.reference_price:,} KRW</td>"
            f"<td>{deal.discount_percent:.1f}%</td>"
            f"<td>{html.escape(listing.condition_status)}</td>"
            f"<td>{html.escape(_confidence_text(deal))}</td>"
            f"<td>{html.escape(second_confidence_text)}</td>"
            f"<td>{html.escape(review_reason)}</td>"
            f"<td>{html.escape(pricing_source)}</td>"
            f"<td><a href=\"{html.escape(listing_url, quote=True)}\">Open listing</a></td>"
            "</tr>"
        )

    html_body = (
        "<html><body><h2>Bunjang computer-parts deal digest</h2>"
        "<table><thead><tr>"
        "<th>#</th><th>Product</th><th>Displayed listing price</th><th>Effective price</th>"
        "<th>Reference market price</th><th>Discount</th><th>Condition</th>"
        "<th>First-stage AI confidence</th><th>Second-stage AI confidence</th>"
        "<th>Final review reason</th><th>Pricing source</th><th>Bunjang listing</th>"
        "</tr></thead><tbody>"
        f"{''.join(html_rows)}"
        "</tbody></table></body></html>"
    )
    message = EmailMessage()
    message["Subject"] = f"Bunjang deal digest: {len(ordered_deals)} bargain(s)"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("\n".join(text_lines))
    message.add_alternative(html_body, subtype="html")
    return message


class EmailNotifier:
    """SMTP sender configured without storing credentials in project files."""

    def __init__(
        self,
        recipient_address: str,
        smtp_host: str,
        smtp_port: int,
        sender_address: str,
        password_environment_variable: str = DEFAULT_PASSWORD_ENVIRONMENT_VARIABLE,
        use_starttls: bool = True,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
    ):
        self.recipient_address = recipient_address
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender_address = sender_address
        self.password_environment_variable = password_environment_variable
        self.use_starttls = use_starttls
        self.smtp_factory = smtp_factory

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "EmailNotifier":
        return cls(
            recipient_address=str(settings["recipient_address"]),
            smtp_host=str(settings["smtp_host"]),
            smtp_port=int(settings["smtp_port"]),
            sender_address=str(settings["sender_address"]),
            password_environment_variable=str(
                settings.get("password_environment_variable", DEFAULT_PASSWORD_ENVIRONMENT_VARIABLE)
            ),
            use_starttls=bool(settings.get("use_starttls", True)),
        )

    def send_digest(
        self, deals: Sequence[Deal], pricing_sources: Mapping[str, str],
        review_metadata: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        password = os.environ.get(self.password_environment_variable)
        if not password:
            raise ValueError(
                "SMTP password is not set in environment variable "
                f"{self.password_environment_variable}"
            )
        message = build_deal_digest_email(
            deals, pricing_sources, self.sender_address, self.recipient_address, review_metadata,
        )
        with self.smtp_factory(self.smtp_host, self.smtp_port, timeout=20) as client:
            client.ehlo()
            if self.use_starttls:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            client.login(self.sender_address, password)
            client.send_message(message)


class KakaoNotifier:
    """Reserved notifier implementation boundary; no Kakao service is enabled."""

    def send_digest(
        self, deals: Sequence[Deal], pricing_sources: Mapping[str, str],
        review_metadata: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        raise NotImplementedError("KakaoNotifier is not configured in this deployment")


def send_unnotified_deal_digest(
    deals: Sequence[Deal],
    database: ListingDatabase,
    email_settings: Mapping[str, Any],
    pricing_sources: Mapping[str, str],
    notifier: DigestNotifier | None = None,
    review_metadata: Mapping[str, Mapping[str, object]] | None = None,
) -> int:
    """Send one digest after a scan and record all included listings on SMTP success."""
    if not email_settings.get("enabled", False):
        return 0
    pending = [
        deal
        for deal in deals
        if deal.listing.listing_status == "active"
        and deal.listing.ai_scope == "standalone"
        and not database.was_notified(deal.listing)
    ]
    if not pending:
        return 0
    sender = notifier or EmailNotifier.from_settings(dict(email_settings))
    try:
        if review_metadata is None:
            # Preserve compatibility with existing two-argument notifier adapters.
            sender.send_digest(pending, pricing_sources)
        else:
            sender.send_digest(pending, pricing_sources, review_metadata)
    except Exception:
        LOGGER.exception("Unable to send Bunjang deal digest")
        return 0
    for deal in pending:
        database.mark_notified(deal.listing)
    database.record_notification_delivery(len(pending), channel="email")
    return len(pending)
