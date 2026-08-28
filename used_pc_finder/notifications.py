"""Email notifications for newly detected deals."""

from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any, Protocol

from .database import ListingDatabase
from .models import Deal

LOGGER = logging.getLogger(__name__)
DEFAULT_PASSWORD_ENVIRONMENT_VARIABLE = "KARROT_SMTP_PASSWORD"


class DealNotifier(Protocol):
    def send_deal(self, deal: Deal) -> None: ...


def _deal_fields(deal: Deal) -> tuple[tuple[str, str], ...]:
    listing = deal.listing
    return (
        ("Product", deal.normalized_name),
        ("Listing price", f"{listing.price:,} KRW"),
        ("Reference market price", f"{deal.reference_price:,} KRW"),
        ("Discount", f"{deal.discount_percent:.1f}%"),
        ("Location", listing.location or "Not specified"),
        ("Source type", listing.source_type),
    )


def build_deal_email(deal: Deal, sender: str, recipient: str) -> EmailMessage:
    """Build a multipart message with text and HTML alternatives for one deal."""
    listing = deal.listing
    fields = _deal_fields(deal)
    text_lines = ["Karrot computer-parts deal found", ""]
    text_lines.extend(f"{label}: {value}" for label, value in fields)
    text_lines.extend(("", f"Original Karrot listing: {listing.url}"))
    html_rows = "".join(
        "<tr><th align=\"left\" style=\"padding:4px 12px 4px 0\">"
        f"{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in fields
    )
    listing_url = html.escape(listing.url, quote=True)
    html_body = (
        "<html><body><h2>Karrot computer-parts deal found</h2>"
        f"<table>{html_rows}</table>"
        f"<p><a href=\"{listing_url}\">Open original Karrot listing</a></p>"
        "</body></html>"
    )

    message = EmailMessage()
    message["Subject"] = f"Karrot deal: {deal.normalized_name} ({deal.discount_percent:.1f}% off)"
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
                settings.get(
                    "password_environment_variable",
                    DEFAULT_PASSWORD_ENVIRONMENT_VARIABLE,
                )
            ),
            use_starttls=bool(settings.get("use_starttls", True)),
        )

    def send_deal(self, deal: Deal) -> None:
        password = os.environ.get(self.password_environment_variable)
        if not password:
            raise ValueError(
                "SMTP password is not set in environment variable "
                f"{self.password_environment_variable}"
            )
        message = build_deal_email(deal, self.sender_address, self.recipient_address)
        with self.smtp_factory(self.smtp_host, self.smtp_port, timeout=20) as client:
            client.ehlo()
            if self.use_starttls:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            client.login(self.sender_address, password)
            client.send_message(message)


def send_unnotified_deals(
    deals: list[Deal],
    database: ListingDatabase,
    email_settings: dict[str, Any],
    notifier: DealNotifier | None = None,
) -> int:
    """Send deals once, recording success only after SMTP accepts the message."""
    if not email_settings.get("enabled", False):
        return 0
    sender = notifier or EmailNotifier.from_settings(email_settings)
    sent = 0
    for deal in deals:
        if database.was_notified(deal.listing):
            continue
        try:
            sender.send_deal(deal)
            database.mark_notified(deal.listing)
            sent += 1
        except Exception:
            LOGGER.exception("Unable to send email notification for %s", deal.listing.url)
    return sent
