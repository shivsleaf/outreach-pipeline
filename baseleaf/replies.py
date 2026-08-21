"""IMAP reply scanning.

Two deliberate constraints:

* Progress is tracked by IMAP UID in `kv_state`, never by the \\Seen flag, and
  messages are fetched with BODY.PEEK[] so reading them does not mark them read.
  You can keep triaging the inbox by hand and this script will not interfere.
* This module only ever REPORTS. Nothing here sends a reply. `suggest-reply`
  drafts text for a human to read, edit, and send themselves.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import sqlite3
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr

from .config import settings
from .db import get_state, log_event, set_state, utcnow

log = logging.getLogger(__name__)

LAST_UID_KEY = "imap_last_uid"
MAX_FETCH_PER_RUN = 200


class ReplyScanError(RuntimeError):
    """IMAP scanning failed."""


@dataclass
class Reply:
    uid: int
    sender: str
    subject: str
    snippet: str
    lead_id: int | None
    lead_email: str

    def format(self) -> str:
        return f"REPLY from {self.sender} | {self.subject!r} | {self.snippet[:160]}"


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _extract_snippet(message: email.message.Message, limit: int = 300) -> str:
    """Plain-text snippet, with quoted history stripped where possible."""
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition") or ""
            ):
                try:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    continue
                break
    else:
        try:
            body = message.get_payload(decode=True).decode(
                message.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            body = ""

    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", stripped):
            break
        if stripped.startswith("-----Original Message-----"):
            break
        lines.append(stripped)
    return " ".join(l for l in lines if l)[:limit]


def _contacted_lookup(conn: sqlite3.Connection) -> dict[str, int]:
    """Map contacted addresses -> lead id. Only these count as replies."""
    rows = conn.execute(
        """SELECT DISTINCT l.email, l.id
             FROM leads l
             JOIN email_events e ON e.email = l.email
            WHERE e.event_type = 'sent'"""
    ).fetchall()
    return {r["email"].lower(): r["id"] for r in rows}


def scan_replies(
    conn: sqlite3.Connection, *, mailbox: str = "INBOX", reset_uid: bool = False
) -> list[Reply]:
    settings.require("email_address", "email_app_password")
    contacted = _contacted_lookup(conn)
    if not contacted:
        log.info("No leads have been emailed yet — nothing to match replies against.")
        return []

    try:
        client = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        client.login(settings.email_address, settings.email_app_password)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise ReplyScanError(f"IMAP login failed: {exc}") from exc

    replies: list[Reply] = []
    try:
        # readonly=True is belt-and-braces on top of BODY.PEEK[]: the session
        # cannot alter flags at all.
        status, _ = client.select(mailbox, readonly=True)
        if status != "OK":
            raise ReplyScanError(f"Could not select mailbox {mailbox!r}")

        last_uid = 0 if reset_uid else int(get_state(conn, LAST_UID_KEY, "0") or 0)
        status, data = client.uid("search", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            raise ReplyScanError("IMAP UID search failed")

        uids = [int(u) for u in (data[0] or b"").split()]
        # `UID n:*` always returns at least the highest UID, even when it is
        # below n — filter so an already-seen message is not re-reported.
        uids = [u for u in uids if u > last_uid][:MAX_FETCH_PER_RUN]
        if not uids:
            log.info("No new messages since UID %s.", last_uid)
            return []

        log.info("Fetching %s new message(s) since UID %s", len(uids), last_uid)
        highest = last_uid

        for uid in uids:
            highest = max(highest, uid)
            status, payload = client.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue

            message = email.message_from_bytes(payload[0][1])
            sender_addr = parseaddr(message.get("From", ""))[1].lower()
            lead_id = contacted.get(sender_addr)
            if lead_id is None:
                continue  # not one of our contacted leads

            reply = Reply(
                uid=uid,
                sender=sender_addr,
                subject=_decode(message.get("Subject")),
                snippet=_extract_snippet(message),
                lead_id=lead_id,
                lead_email=sender_addr,
            )
            replies.append(reply)

            log_event(
                conn, lead_id=lead_id, email=sender_addr, event_type="reply",
                subject=reply.subject, body=reply.snippet, message_id=message.get("Message-ID"),
            )
            conn.execute(
                "UPDATE leads SET status = 'replied', updated_at = ? WHERE id = ?",
                (utcnow(), lead_id),
            )

            # An unsubscribe request in a reply is honoured immediately.
            if re.search(r"\b(unsubscribe|remove me|stop emailing|take me off)\b",
                         f"{reply.subject} {reply.snippet}", re.IGNORECASE):
                from .db import suppress
                suppress(conn, sender_addr, "requested via reply", "imap")
                log.info("Suppressed %s — unsubscribe requested in their reply", sender_addr)

        set_state(conn, LAST_UID_KEY, str(highest))
        conn.commit()
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass

    return replies
