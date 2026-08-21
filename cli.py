#!/usr/bin/env python3
"""Baseleaf outreach pipeline — terminal only.

Subcommands:
    init-db             create the SQLite schema
    find-leads          source b2b leads from Apollo (costs credits)
    import-individuals  load opted-in individuals from CSV (consent required)
    send                draft with Claude and send (DRY RUN unless --live)
    show-drafts         print recent drafted / sent emails in full
    scan-replies        poll IMAP for replies and report them
    suggest-reply       draft a suggested reply for one lead (never auto-sent)
    suppress            add an address to the suppression list
    stats               show pipeline state
    run                 find-leads -> send -> scan-replies (for cron)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from baseleaf import db
from baseleaf.config import ConfigError, settings


def setup_logging(verbose: bool = False) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(logging.FileHandler(settings.log_dir / "outreach.log"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init_db(args: argparse.Namespace) -> int:
    conn = db.init_db()
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger') ORDER BY type, name"
    ).fetchall()]
    conn.close()
    print(f"Database ready at {settings.db_path}")
    print("Objects: " + ", ".join(tables))
    return 0


def cmd_find_leads(args: argparse.Namespace) -> int:
    from baseleaf.apollo import ApolloClient, ApolloError
    from baseleaf.leads import ingest_apollo_people, known_apollo_ids

    log = logging.getLogger("find-leads")
    try:
        settings.require("apollo_api_key")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = ApolloClient(settings.apollo_api_key)

    titles = args.titles or [
        "immigration attorney", "immigration lawyer",
        "global mobility manager", "head of people", "hr manager",
    ]
    locations = args.locations or None

    with db.session() as conn:
        skip = known_apollo_ids(conn)
        log.info("Already have %s Apollo record(s) — they will not be re-enriched", len(skip))

        if args.count_only:
            _, total = client.search(titles=titles, locations=locations, per_page=1)
            print(f"\nApollo reports {total:,} people matching those filters.")
            print("No credits spent (search returns obfuscated stubs only).")
            print(client.usage.report())
            return 0

        try:
            people = list(client.search_and_enrich(
                titles=titles, locations=locations,
                limit=args.limit, per_page=args.per_page, skip_ids=skip,
            ))
        except ApolloError as exc:
            log.error("%s", exc)
            print(client.usage.report(), file=sys.stderr)
            return 1

        stats = ingest_apollo_people(conn, people)

    print(f"\nEnriched {len(people)} person(s) from Apollo.")
    print(f"  inserted:  {stats['inserted']}")
    print(f"  duplicate: {stats['duplicate']}")
    print(f"  rejected:  {stats['rejected']}")
    print(client.usage.report())
    return 0


def cmd_import_individuals(args: argparse.Namespace) -> int:
    from baseleaf.leads import import_individual_csv

    path = Path(args.csv)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        return 2

    with db.session() as conn:
        stats = import_individual_csv(conn, path, default_consent_source=args.consent_source)

    print(f"\nImported from {path}:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if stats.get("no_consent"):
        print(
            f"\n{stats['no_consent']} row(s) skipped for missing consent_basis. "
            "CASL requires recorded opt-in before emailing an individual."
        )
    return 0


def cmd_suppress(args: argparse.Namespace) -> int:
    with db.session() as conn:
        db.suppress(conn, args.email.strip().lower(), args.reason, "manual")
    print(f"Suppressed {args.email} ({args.reason}). Sends to this address will now abort at the DB layer.")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    from baseleaf.sender import send_batch

    live = bool(args.live)

    try:
        if live:
            settings.require_sending()
        settings.require("anthropic_api_key")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if live:
        print(
            f"\nLIVE MODE — real emails will be sent from {settings.email_address}.\n"
            f"Cap: {settings.daily_send_cap}/24h, {settings.send_delay_min_seconds}-"
            f"{settings.send_delay_max_seconds}s between sends.\n"
        )
    else:
        print("\nDRY RUN — drafting and logging only. Nothing will be sent. Use --live to send.\n")

    with db.session() as conn:
        report = send_batch(
            conn, limit=args.limit, lane=args.lane, live=live,
            campaign=args.campaign, sender_name=args.sender_name,
        )

    print("\n" + report.summary(live))
    if not live and report.dry_run:
        print("\nReview the drafts with:  ./cli.py show-drafts")
    return 0


def cmd_show_drafts(args: argparse.Namespace) -> int:
    with db.session() as conn:
        rows = conn.execute(
            """SELECT email, subject, body, created_at FROM email_events
                WHERE event_type IN ('dry_run', 'sent')
                ORDER BY id DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()

    if not rows:
        print("No drafts yet. Run:  ./cli.py send   (dry run by default)")
        return 0

    for r in rows:
        print("\n" + "=" * 72)
        print(f"To:      {r['email']}")
        print(f"Subject: {r['subject']}")
        print(f"When:    {r['created_at']}")
        print("-" * 72)
        print(r["body"])
    print("\n" + "=" * 72)
    return 0


def cmd_scan_replies(args: argparse.Namespace) -> int:
    from baseleaf.notify import post_slack, write_report
    from baseleaf.replies import ReplyScanError, scan_replies

    try:
        settings.require("email_address", "email_app_password")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with db.session() as conn:
        try:
            replies = scan_replies(conn, mailbox=args.mailbox, reset_uid=args.reset_uid)
        except ReplyScanError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if not replies:
        print("No new replies.")
        return 0

    lines = [r.format() for r in replies]
    path = write_report(lines)
    print(f"\n{len(replies)} new repl(y/ies):\n")
    for r in replies:
        print(f"  from {r.sender}")
        print(f"  subj {r.subject}")
        print(f"       {r.snippet[:150]}")
        print()
    print(f"Appended to {path}")

    if settings.slack_webhook_url:
        text = f"*{len(replies)} new Baseleaf repl(y/ies)*\n" + "\n".join(
            f"• `{r.sender}` — {r.subject}" for r in replies
        )
        print("Slack notified." if post_slack(text) else "Slack notification failed (see log).")
    return 0


def cmd_suggest_reply(args: argparse.Namespace) -> int:
    """Draft a SUGGESTED reply for a human to send. Never sends anything."""
    from baseleaf.personalize import PersonalizationError, suggest_reply

    try:
        settings.require("anthropic_api_key")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with db.session() as conn:
        lead = conn.execute(
            "SELECT * FROM leads WHERE email = ?", (args.email.strip().lower(),)
        ).fetchone()
        if not lead:
            print(f"error: no lead with email {args.email}", file=sys.stderr)
            return 2
        row = conn.execute(
            """SELECT body FROM email_events
                WHERE email = ? AND event_type = 'reply'
                ORDER BY id DESC LIMIT 1""",
            (args.email.strip().lower(),),
        ).fetchone()

    thread = args.text or (row["body"] if row else "")
    if not thread:
        print("error: no reply recorded for that lead. Pass --text to supply it.", file=sys.stderr)
        return 2

    try:
        draft = suggest_reply(dict(lead), thread)
    except PersonalizationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("\n" + "=" * 72)
    print(f"SUGGESTED reply to {lead['email']} — NOT SENT")
    print("=" * 72)
    print(draft)
    print("=" * 72)
    print("\nRead it, edit it, and send it yourself from your mail client.")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with db.session() as conn:
        lanes = conn.execute(
            "SELECT lane, status, COUNT(*) n FROM leads GROUP BY lane, status ORDER BY lane, status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        suppressed = conn.execute("SELECT COUNT(*) n FROM suppression").fetchone()["n"]
        events = conn.execute(
            "SELECT event_type, COUNT(*) n FROM email_events GROUP BY event_type ORDER BY event_type"
        ).fetchall()
        sent24 = db.sends_in_last_24h(conn)

    print(f"\nDatabase: {settings.db_path}")
    print(f"Leads: {total} | Suppressed: {suppressed}")
    if lanes:
        print("\n  lane          status        count")
        print("  " + "-" * 34)
        for r in lanes:
            print(f"  {r['lane']:<13} {r['status']:<13} {r['n']}")
    if events:
        print("\n  event         count")
        print("  " + "-" * 20)
        for r in events:
            print(f"  {r['event_type']:<13} {r['n']}")
    print(f"\nSent in last 24h: {sent24} / {settings.daily_send_cap} cap")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """All-in-one entrypoint for cron. Dry-run unless --live is passed."""
    log = logging.getLogger("run")
    steps = 0

    if settings.apollo_api_key and args.find_limit > 0:
        log.info("--- step 1: find-leads ---")
        find_args = argparse.Namespace(
            titles=None, locations=args.locations, limit=args.find_limit,
            per_page=25, count_only=False,
        )
        try:
            cmd_find_leads(find_args)
        except Exception as exc:
            log.error("find-leads failed: %s", exc)
        steps += 1
    else:
        log.info("Skipping find-leads (no Apollo key, or --find-limit 0)")

    if settings.anthropic_api_key:
        log.info("--- step 2: send ---")
        send_args = argparse.Namespace(
            limit=args.send_limit, lane=args.lane, live=args.live,
            campaign=args.campaign, sender_name=None,
        )
        try:
            cmd_send(send_args)
        except Exception as exc:
            log.error("send failed: %s", exc)
        steps += 1
    else:
        log.info("Skipping send (no ANTHROPIC_API_KEY)")

    if settings.email_address and settings.email_app_password:
        log.info("--- step 3: scan-replies ---")
        scan_args = argparse.Namespace(mailbox="INBOX", reset_uid=False)
        try:
            cmd_scan_replies(scan_args)
        except Exception as exc:
            log.error("scan-replies failed: %s", exc)
        steps += 1
    else:
        log.info("Skipping scan-replies (no mailbox credentials)")

    log.info("run complete (%s step(s))", steps)
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="baseleaf", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the SQLite schema").set_defaults(func=cmd_init_db)

    f = sub.add_parser("find-leads", help="source b2b leads from Apollo (costs credits)")
    f.add_argument("--titles", nargs="+", help="job titles to target")
    f.add_argument("--locations", nargs="+", help='e.g. "United States" "Canada"')
    f.add_argument("--limit", type=int, default=5,
                   help="max people to ENRICH — this equals credits spent (default: 5)")
    f.add_argument("--per-page", type=int, default=25, help="search page size (default: 25)")
    f.add_argument("--count-only", action="store_true",
                   help="report how many people match, spend no credits")
    f.set_defaults(func=cmd_find_leads)

    i = sub.add_parser("import-individuals", help="load opted-in individuals from CSV")
    i.add_argument("csv", help="CSV with columns: email,first_name,last_name,consent_basis,...")
    i.add_argument("--consent-source", help="default consent_source for rows lacking one")
    i.set_defaults(func=cmd_import_individuals)

    s = sub.add_parser("suppress", help="add an address to the suppression list")
    s.add_argument("email")
    s.add_argument("--reason", default="manual request")
    s.set_defaults(func=cmd_suppress)

    sd = sub.add_parser("send", help="draft and send emails (DRY RUN unless --live)")
    sd.add_argument("--limit", type=int, default=10, help="max emails this run (default: 10)")
    sd.add_argument("--lane", choices=["b2b", "individual"], help="restrict to one lane")
    sd.add_argument("--campaign", default="default", help="campaign label for the event log")
    sd.add_argument("--sender-name", help="name to sign off as")
    sd.add_argument("--live", action="store_true",
                    help="ACTUALLY SEND. Without this flag nothing leaves the mailbox.")
    sd.set_defaults(func=cmd_send)

    sh = sub.add_parser("show-drafts", help="print recent drafted / sent emails in full")
    sh.add_argument("--limit", type=int, default=5)
    sh.set_defaults(func=cmd_show_drafts)

    sr = sub.add_parser("scan-replies", help="poll IMAP for replies and report them")
    sr.add_argument("--mailbox", default="INBOX")
    sr.add_argument("--reset-uid", action="store_true",
                    help="rescan from the beginning of the mailbox")
    sr.set_defaults(func=cmd_scan_replies)

    sg = sub.add_parser("suggest-reply",
                        help="draft a suggested reply for one lead (never sends it)")
    sg.add_argument("email")
    sg.add_argument("--text", help="their message, if it is not already in the database")
    sg.set_defaults(func=cmd_suggest_reply)

    rn = sub.add_parser("run", help="find-leads -> send -> scan-replies (for cron)")
    rn.add_argument("--find-limit", type=int, default=5,
                    help="Apollo enrichments per run = credits spent (default: 5)")
    rn.add_argument("--send-limit", type=int, default=10)
    rn.add_argument("--lane", choices=["b2b", "individual"])
    rn.add_argument("--locations", nargs="+")
    rn.add_argument("--campaign", default="cron")
    rn.add_argument("--live", action="store_true", help="ACTUALLY SEND")
    rn.set_defaults(func=cmd_run)

    sub.add_parser("stats", help="show pipeline state").set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
