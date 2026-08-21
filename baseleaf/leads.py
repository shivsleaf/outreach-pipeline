"""Lead ingestion, normalisation and consent bookkeeping."""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .compliance import make_unsubscribe_token
from .db import log_event, utcnow

log = logging.getLogger(__name__)

B2B = "b2b"
INDIVIDUAL = "individual"


class ConsentError(ValueError):
    """Raised when an individual-lane lead arrives without a consent basis."""


def normalise_apollo_person(person: dict[str, Any]) -> dict[str, Any] | None:
    """Map an enriched Apollo record onto our lead shape."""
    email = (person.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return None
    # Apollo returns this placeholder when an address exists but is not unlocked.
    if "email_not_unlocked" in email or email.endswith("domain.com"):
        return None

    org = person.get("organization") or {}
    website = org.get("website_url") or ""
    domain = website.replace("https://", "").replace("http://", "").split("/")[0] or None
    location = ", ".join(
        p for p in [person.get("city"), person.get("state"), person.get("country")] if p
    ) or None

    return {
        "email": email,
        "lane": B2B,
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name"),
        "title": person.get("title"),
        "company": org.get("name") or person.get("organization_name"),
        "company_domain": domain,
        "linkedin_url": person.get("linkedin_url"),
        "location": location,
        "industry": org.get("industry"),
        "source": "apollo",
        "source_id": person.get("id"),
        "consent_basis": None,  # b2b cold outreach: CAN-SPAM opt-out model
        "consent_source": None,
        "consent_at": None,
        "raw_json": json.dumps(
            {k: person.get(k) for k in ("id", "title", "headline", "seniority", "email_status")}
        ),
    }


def upsert_lead(conn: sqlite3.Connection, lead: dict[str, Any]) -> tuple[int | None, str]:
    """Insert a lead, or skip it if the email already exists.

    Returns (lead_id, outcome) where outcome is 'inserted' | 'duplicate' | 'rejected'.
    """
    email = (lead.get("email") or "").strip().lower()
    lane = lead.get("lane")

    if not email or "@" not in email:
        return None, "rejected"
    if lane not in (B2B, INDIVIDUAL):
        raise ValueError(f"lane must be one of {B2B!r} / {INDIVIDUAL!r}, got {lane!r}")

    # Belt-and-braces: the DB CHECK constraint is the real guarantee, but a
    # clear Python-side error beats a raw IntegrityError for CLI users.
    if lane == INDIVIDUAL and not (lead.get("consent_basis") or "").strip():
        raise ConsentError(
            f"Refusing to store individual-lane lead {email!r} without consent_basis. "
            "CASL requires recorded opt-in consent before emailing a Canadian individual."
        )

    now = utcnow()
    try:
        cur = conn.execute(
            """INSERT INTO leads
               (email, lane, first_name, last_name, title, company, company_domain,
                linkedin_url, location, industry, source, source_id, consent_basis,
                consent_source, consent_at, unsubscribe_token, raw_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                email, lane, lead.get("first_name"), lead.get("last_name"), lead.get("title"),
                lead.get("company"), lead.get("company_domain"), lead.get("linkedin_url"),
                lead.get("location"), lead.get("industry"), lead.get("source") or "manual",
                lead.get("source_id"), lead.get("consent_basis"), lead.get("consent_source"),
                lead.get("consent_at") or (now if lead.get("consent_basis") else None),
                make_unsubscribe_token(email), lead.get("raw_json"), now, now,
            ),
        )
        return int(cur.lastrowid), "inserted"
    except sqlite3.IntegrityError as exc:
        msg = str(exc).lower()
        if "unique" in msg:
            return None, "duplicate"
        log.warning("Rejected lead %s: %s", email, exc)
        return None, "rejected"


def import_individual_csv(
    conn: sqlite3.Connection, path: Path, *, default_consent_source: str | None = None
) -> dict[str, int]:
    """Import opted-in individuals. Requires a consent_basis column per row.

    This is the ONLY supported way into the individual lane. There is
    deliberately no scraping or purchased-list path.
    """
    stats = {"inserted": 0, "duplicate": 0, "rejected": 0, "no_consent": 0}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            basis = (row.get("consent_basis") or "").strip()
            if not basis:
                stats["no_consent"] += 1
                log.warning("Skipping %s: no consent_basis recorded", row.get("email"))
                continue
            lead = {
                "email": row.get("email"),
                "lane": INDIVIDUAL,
                "first_name": row.get("first_name"),
                "last_name": row.get("last_name"),
                "location": row.get("location"),
                "source": row.get("source") or "landing_page",
                "consent_basis": basis,
                "consent_source": row.get("consent_source") or default_consent_source,
                "consent_at": row.get("consent_at"),
            }
            try:
                _, outcome = upsert_lead(conn, lead)
                stats[outcome] = stats.get(outcome, 0) + 1
            except ConsentError:
                stats["no_consent"] += 1
    return stats


def ingest_apollo_people(
    conn: sqlite3.Connection, people: Iterable[dict[str, Any]]
) -> dict[str, int]:
    stats = {"inserted": 0, "duplicate": 0, "rejected": 0}
    for person in people:
        lead = normalise_apollo_person(person)
        if not lead:
            stats["rejected"] += 1
            continue
        _, outcome = upsert_lead(conn, lead)
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


def known_apollo_ids(conn: sqlite3.Connection) -> set[str]:
    """Existing Apollo ids, so repeat runs never pay to enrich the same person."""
    rows = conn.execute(
        "SELECT source_id FROM leads WHERE source = 'apollo' AND source_id IS NOT NULL"
    ).fetchall()
    return {r["source_id"] for r in rows}
