"""Sends the "you have a new message" notification email.

Used by POST /notify-message, called by the internal Joint-Use-Permits
widget right after a city reply is written directly to the Messages
table -- this is the contractor-notification half only; the other
direction (a contractor's message notifying city staff) is handled by
Joint-Use-External's own PHP backend, not this service. Failing to send
never blocks the message itself from being saved: email is a courtesy,
not the record of truth (the Messages table is), so a bad SMTP relay or
an unset config shouldn't make replying itself unusable.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from . import config

logger = logging.getLogger(__name__)


def _single_line(value: str) -> str:
    """Collapses embedded newlines so a value can go straight into a
    header -- see send_message_notification's Subject line."""

    return " ".join(str(value).splitlines()).strip()


def send_message_notification(
    to_address: str,
    sender_label: str,
    body: str,
    permit_number: str | None,
) -> None:
    """Best-effort notification -- logs and returns on any failure,
    including SMTP_HOST being unset, rather than raising."""

    if not to_address:
        logger.warning("No recipient address to notify for a message from %s -- skipping email.", sender_label)
        return

    if not config.SMTP_HOST:
        logger.warning(
            "SMTP_HOST is not configured -- would have emailed %s about a message from %s.",
            to_address,
            sender_label,
        )
        return

    # permit_number/sender_label ultimately trace back to a request body
    # (POST /notify-message's payload) -- strip CR/LF before they reach a
    # header value so nobody can fold in extra headers via a crafted value.
    safe_permit_number = _single_line(permit_number) if permit_number else None
    subject = f"New message on permit {safe_permit_number}" if safe_permit_number else "New message on your request"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_FROM_ADDRESS
    message["To"] = to_address
    message.set_content(f"{sender_label} wrote:\n\n{body}")

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
            if config.SMTP_USE_TLS:
                smtp.starttls()
            if config.SMTP_USERNAME:
                smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(message)
        logger.info("Sent message notification to %s.", to_address)
    except Exception:
        # Never let an email failure surface as a failure to send the
        # message itself -- the caller already saved it before this runs.
        logger.exception("Failed to send message notification to %s.", to_address)
