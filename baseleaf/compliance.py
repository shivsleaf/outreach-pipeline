"""Compliance helpers: unsubscribe tokens and the mandatory email footer.

CAN-SPAM requires a working unsubscribe mechanism and a physical mailing
address on every commercial email, in both lanes. CASL additionally requires
opt-in consent *before* sending to a Canadian individual, which is enforced in
the data model (see db.py), not here.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
from urllib.parse import quote

from .config import settings


def make_unsubscribe_token(email: str, secret: str | None = None) -> str:
    """Deterministic, non-guessable token so an unsubscribe link stays valid."""
    key = (secret or settings.unsubscribe_secret or "").encode()
    if not key:
        # No secret configured: fall back to a random token. Still works as an
        # opaque identifier, but cannot be re-derived, so it must be stored.
        return secrets.token_urlsafe(24)
    digest = hmac.new(key, email.strip().lower().encode(), sha256).hexdigest()
    return digest[:32]


def verify_unsubscribe_token(email: str, token: str, secret: str | None = None) -> bool:
    return hmac.compare_digest(make_unsubscribe_token(email, secret), token)


def unsubscribe_url(email: str, token: str) -> str:
    base = (settings.unsubscribe_base_url or "https://baseleaf.com/unsubscribe").rstrip("/")
    return f"{base}?e={quote(email)}&t={quote(token)}"


def footer_text(email: str, token: str) -> str:
    return (
        "\n\n---\n"
        f"{settings.company_name}\n"
        f"{settings.physical_address or '[PHYSICAL ADDRESS NOT CONFIGURED]'}\n\n"
        f"Don't want these emails? Unsubscribe here: {unsubscribe_url(email, token)}\n\n"
        f"{settings.company_name} provides self-service immigration information tools. "
        "We are not a law firm and nothing in this email is legal advice."
    )


def footer_html(email: str, token: str) -> str:
    url = unsubscribe_url(email, token)
    addr = settings.physical_address or "[PHYSICAL ADDRESS NOT CONFIGURED]"
    return (
        '<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">'
        '<div style="font-size:12px;color:#666;line-height:1.5">'
        f"<div>{settings.company_name}</div>"
        f"<div>{addr}</div>"
        f'<div style="margin-top:8px">Don\'t want these emails? '
        f'<a href="{url}">Unsubscribe</a></div>'
        f'<div style="margin-top:8px">{settings.company_name} provides self-service immigration '
        "information tools. We are not a law firm and nothing in this email is legal advice.</div>"
        "</div>"
    )


def has_required_footer(body: str) -> bool:
    """Last-line assertion used by the sender before a message goes out."""
    return "Unsubscribe" in body or "unsubscribe" in body
