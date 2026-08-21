"""SMTP sending through a single Gmail / Workspace mailbox.

This is the most fragile part of the pipeline. Real-world reports have single
accounts getting flagged well below Gmail's published 500/day limit, driven by
burst-sending behaviour rather than raw volume. So:

* a conservative rolling 24-hour cap (default 25), derived from the event log
  rather than a counter that can drift;
* a random 1-3 minute pause between individual sends, so the pipeline never
  fires in a tight loop;
* dry-run is the default. Real sending requires an explicit --live flag.

The suppression guarantee: `_send_one` inserts the 'sent' event inside an open
transaction BEFORE handing the message to SMTP. The database triggers abort
that insert for a suppressed or opted-out address, so the SMTP call is never
reached. If SMTP then fails, the transaction is rolled back and no false 'sent'
record survives.
"""

from __future__ import annotations

import logging
import random
import smtplib
import sqlite3
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .compliance import (
    footer_html,
    footer_text,
    has_required_footer,
    make_unsubscribe_token,
    unsubscribe_url,
)
from .config import settings
from .db import log_event, sends_in_last_24h, utcnow
from .personalize import Draft, PersonalizationError, draft_email

log = logging.getLogger(__name__)


class SendError(RuntimeError):
    """Sending failed for a reason the operator needs to see."""


@dataclass
class SendReport:
    attempted: int = 0
    sent: int = 0
    dry_run: int = 0
    skipped: int = 0
    failed: int = 0
    cap_reached: bool = False

    def summary(self, live: bool) -> str:
        mode = "LIVE" if live else "DRY RUN"
        return (
            f"[{mode}] attempted {self.attempted} | sent {self.sent} | "
            f"drafted-only {self.dry_run} | skipped {self.skipped} | failed {self.failed}"
            + (" | daily cap reached" if self.cap_reached else "")
        )


def build_message(lead: dict, draft: Draft) -> tuple[EmailMessage, str, str]:
    """Assemble the MIME message, including the mandatory compliance footer.

    Returns (message, message_id, plain_text_body). The plain-text body is
    returned explicitly because `EmailMessage.get_content()` raises on a
    multipart/alternative message, and the event log needs the text version.
    """
    email = lead["email"]
    token = lead.get("unsubscribe_token") or make_unsubscribe_token(email)

    msg = EmailMessage()
    msg["Subject"] = draft.subject
    msg["From"] = formataddr((settings.from_name, settings.email_address or ""))
    msg["To"] = email
    if settings.reply_to:
        msg["Reply-To"] = settings.reply_to
    message_id = make_msgid(domain=(settings.email_address or "@baseleaf.com").split("@")[-1])
    msg["Message-ID"] = message_id

    # One-click unsubscribe headers: mail providers surface these natively, which
    # measurably reduces spam complaints against the sending domain.
    msg["List-Unsubscribe"] = f"<{unsubscribe_url(email, token)}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    body_text = draft.body + footer_text(email, token)
    msg.set_content(body_text)

    body_html = (
        "<div style=\"font-family:-apple-system,Segoe UI,sans-serif;font-size:15px;"
        "line-height:1.6;color:#222\">"
        + "".join(f"<p>{para.strip()}</p>" for para in draft.body.split("\n\n") if para.strip())
        + footer_html(email, token)
        + "</div>"
    )
    msg.add_alternative(body_html, subtype="html")

    if not has_required_footer(body_text):
        raise SendError("refusing to send: compliance footer missing from message body")
    return msg, message_id, body_text


def _connect_smtp() -> smtplib.SMTP:
    settings.require_sending()
    server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    server.ehlo()
    server.starttls()
    server.ehlo()
    server.login(settings.email_address, settings.email_app_password)
    return server


def _send_one(
    conn: sqlite3.Connection,
    server: smtplib.SMTP | None,
    lead: dict,
    draft: Draft,
    *,
    live: bool,
    campaign: str,
) -> str:
    """Send (or dry-run) a single email. Returns 'sent' | 'dry_run'."""
    msg, message_id, body_text = build_message(lead, draft)

    if not live or server is None:
        log_event(
            conn, lead_id=lead["id"], email=lead["email"], event_type="dry_run",
            campaign=campaign, subject=draft.subject, body=body_text,
        )
        conn.execute(
            "UPDATE leads SET status = 'drafted', updated_at = ? WHERE id = ?",
            (utcnow(), lead["id"]),
        )
        conn.commit()
        return "dry_run"

    # The event insert is the enforcement point. If the recipient is suppressed
    # or opted out, a trigger raises here and SMTP is never touched.
    log_event(
        conn, lead_id=lead["id"], email=lead["email"], event_type="sent",
        campaign=campaign, subject=draft.subject, body=body_text,
        message_id=message_id,
    )
    try:
        server.send_message(msg)
    except Exception:
        conn.rollback()  # no false 'sent' record survives a failed delivery
        raise

    conn.execute(
        "UPDATE leads SET status = 'sent', updated_at = ? WHERE id = ?",
        (utcnow(), lead["id"]),
    )
    conn.commit()
    return "sent"


def send_batch(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    lane: str | None = None,
    live: bool = False,
    campaign: str = "default",
    sender_name: str | None = None,
) -> SendReport:
    """Draft and send up to `limit` emails, honouring the rolling 24h cap.

    `live=False` (the default) drafts and logs without sending anything.
    """
    report = SendReport()

    already = sends_in_last_24h(conn)
    remaining = max(settings.daily_send_cap - already, 0)
    if live and remaining == 0:
        report.cap_reached = True
        log.warning(
            "Daily cap reached: %s sent in the last 24h (cap %s). Nothing will be sent.",
            already, settings.daily_send_cap,
        )
        return report
    if live:
        limit = min(limit, remaining)
        log.info("%s of %s daily sends remaining; this run will send at most %s",
                 remaining, settings.daily_send_cap, limit)

    query = "SELECT * FROM sendable_leads"
    params: list = []
    if lane:
        query += " WHERE lane = ?"
        params.append(lane)
    query += " ORDER BY created_at LIMIT ?"
    params.append(limit)
    leads = [dict(r) for r in conn.execute(query, params).fetchall()]

    if not leads:
        log.info("No sendable leads matched.")
        return report

    server = _connect_smtp() if live else None
    try:
        for index, lead in enumerate(leads):
            report.attempted += 1
            try:
                draft = draft_email(lead, sender_name=sender_name)
            except PersonalizationError as exc:
                report.skipped += 1
                log.warning("Skipping %s: %s", lead["email"], exc)
                log_event(
                    conn, lead_id=lead["id"], email=lead["email"], event_type="skipped",
                    campaign=campaign, meta_json=f'{{"reason": "{exc}"}}',
                )
                conn.commit()
                continue

            try:
                outcome = _send_one(
                    conn, server, lead, draft, live=live, campaign=campaign,
                )
            except sqlite3.IntegrityError as exc:
                # A trigger blocked it — suppressed or opted out.
                report.skipped += 1
                conn.rollback()
                log.warning("Blocked by database guard for %s: %s", lead["email"], exc)
                continue
            except Exception as exc:
                report.failed += 1
                log.error("Send failed for %s: %s", lead["email"], exc)
                log_event(
                    conn, lead_id=lead["id"], email=lead["email"], event_type="error",
                    campaign=campaign, meta_json=f'{{"error": "{exc}"}}',
                )
                conn.execute(
                    "UPDATE leads SET status = 'failed', updated_at = ? WHERE id = ?",
                    (utcnow(), lead["id"]),
                )
                conn.commit()
                continue

            if outcome == "sent":
                report.sent += 1
                log.info("Sent to %s — %r", lead["email"], draft.subject)
            else:
                report.dry_run += 1
                log.info("[dry run] would send to %s — %r", lead["email"], draft.subject)

            # Pace real sends. No delay after the final message, and none at all
            # in dry run, so testing stays fast.
            if live and index < len(leads) - 1:
                delay = random.randint(
                    settings.send_delay_min_seconds, settings.send_delay_max_seconds
                )
                log.info("Waiting %ss before the next send", delay)
                time.sleep(delay)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass

    return report
