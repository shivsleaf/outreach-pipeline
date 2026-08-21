"""SQLite storage layer.

Two compliance rules are enforced by the database itself, not by convention:

1. CASL consent: a CHECK constraint on `leads` rejects any individual-lane row
   that has no recorded `consent_basis`. Ontario/Canada requires opt-in consent
   *before* a commercial email is sent to an individual.

2. Suppression: a BEFORE INSERT trigger on `email_events` aborts any 'sent'
   event whose recipient is suppressed or has opted out. The sender records the
   event in the same transaction *before* handing the message to SMTP, so a
   suppressed address cannot be mailed even if calling code forgets to check.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import settings

LANES = ("b2b", "individual")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    lane            TEXT NOT NULL CHECK (lane IN ('b2b', 'individual')),
    first_name      TEXT,
    last_name       TEXT,
    title           TEXT,
    company         TEXT,
    company_domain  TEXT,
    linkedin_url    TEXT,
    location        TEXT,
    industry        TEXT,
    source          TEXT NOT NULL,
    source_id       TEXT,
    consent_basis   TEXT,
    consent_source  TEXT,
    consent_at      TEXT,
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new','drafted','sent','replied','bounced','suppressed','failed')),
    opted_out       INTEGER NOT NULL DEFAULT 0 CHECK (opted_out IN (0, 1)),
    unsubscribe_token TEXT UNIQUE,
    raw_json        TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    -- CASL: individual-lane leads are illegal to store for outreach without consent.
    CHECK (lane = 'b2b' OR (consent_basis IS NOT NULL AND trim(consent_basis) <> ''))
);

CREATE INDEX IF NOT EXISTS idx_leads_lane_status ON leads(lane, status);
CREATE INDEX IF NOT EXISTS idx_leads_opted_out ON leads(opted_out);

CREATE TABLE IF NOT EXISTS email_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    email       TEXT NOT NULL COLLATE NOCASE,
    event_type  TEXT NOT NULL
                CHECK (event_type IN ('drafted','dry_run','sent','reply','bounce','error','skipped')),
    campaign    TEXT,
    subject     TEXT,
    body        TEXT,
    message_id  TEXT,
    meta_json   TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_type_time ON email_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_events_email ON email_events(email);

CREATE TABLE IF NOT EXISTS suppression (
    email       TEXT PRIMARY KEY COLLATE NOCASE,
    reason      TEXT NOT NULL,
    source      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

-- Hard stop: a suppressed or opted-out address can never be recorded as sent,
-- and because the event is written before SMTP hands off, never actually mailed.
CREATE TRIGGER IF NOT EXISTS trg_block_suppressed_send
BEFORE INSERT ON email_events
FOR EACH ROW
WHEN NEW.event_type = 'sent'
     AND EXISTS (SELECT 1 FROM suppression s WHERE s.email = NEW.email)
BEGIN
    SELECT RAISE(ABORT, 'blocked: recipient is on the suppression list');
END;

CREATE TRIGGER IF NOT EXISTS trg_block_opted_out_send
BEFORE INSERT ON email_events
FOR EACH ROW
WHEN NEW.event_type = 'sent'
     AND EXISTS (SELECT 1 FROM leads l WHERE l.email = NEW.email AND l.opted_out = 1)
BEGIN
    SELECT RAISE(ABORT, 'blocked: lead has opted out');
END;

-- Opting out anywhere immediately propagates to the lead record.
CREATE TRIGGER IF NOT EXISTS trg_suppression_marks_lead
AFTER INSERT ON suppression
FOR EACH ROW
BEGIN
    UPDATE leads
       SET opted_out = 1,
           status = 'suppressed',
           updated_at = NEW.created_at
     WHERE email = NEW.email;
END;

-- Leads eligible to receive mail. Every send path selects from here.
CREATE VIEW IF NOT EXISTS sendable_leads AS
SELECT l.*
  FROM leads l
 WHERE l.opted_out = 0
   AND l.status IN ('new', 'drafted')
   AND NOT EXISTS (SELECT 1 FROM suppression s WHERE s.email = l.email);
"""


def utcnow() -> str:
    """UTC timestamp in SQLite's own text format.

    Must stay byte-comparable with `datetime('now')` so range queries such as
    the rolling 24-hour send cap work; an ISO string with a '+00:00' suffix
    would sort incorrectly against SQLite's space-separated output.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = init_db(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, utcnow()),
    )


def log_event(
    conn: sqlite3.Connection,
    *,
    email: str,
    event_type: str,
    lead_id: int | None = None,
    campaign: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    message_id: str | None = None,
    meta_json: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO email_events
           (lead_id, email, event_type, campaign, subject, body, message_id, meta_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lead_id, email, event_type, campaign, subject, body, message_id, meta_json, utcnow()),
    )
    return int(cur.lastrowid)


def suppress(conn: sqlite3.Connection, email: str, reason: str, source: str | None = None) -> None:
    conn.execute(
        """INSERT INTO suppression (email, reason, source, created_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(email) DO NOTHING""",
        (email, reason, source, utcnow()),
    )


def sends_in_last_24h(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM email_events
            WHERE event_type = 'sent'
              AND created_at >= datetime('now', '-24 hours')"""
    ).fetchone()
    return int(row["n"])
