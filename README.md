# Baseleaf outreach pipeline

Terminal-only lead sourcing and cold outreach for Baseleaf. Python 3.11+, SQLite,
four dependencies. Every outreach email points at a free tool (the green-card
eligibility checker) which is the conversion path into the paid product.

```
find-leads ──▶ leads (SQLite) ──▶ draft (Claude) ──▶ send (SMTP) ──▶ scan-replies (IMAP)
  Apollo         dedup + consent      guardrails       cap + jitter      report only
```

## The two lanes

Apollo sells **professional/company data** — titles, work emails, LinkedIn. It has
no "person is currently applying for a green card" filter, because that is consumer
intent data Apollo does not carry. The two lanes are therefore fed differently and
must not be merged:

| Lane | Source | Legal basis | How leads get in |
|---|---|---|---|
| `b2b` | Apollo | CAN-SPAM opt-out model | `find-leads` |
| `individual` | Baseleaf landing pages only | **CASL opt-in, consent recorded before sending** | `import-individuals` (requires `consent_basis`) |

Ontario/Canada is CASL territory: consent must exist **before** a commercial email
reaches an individual, unlike the US opt-out model. This is enforced by a CHECK
constraint on the `leads` table — an individual-lane row without a `consent_basis`
raises `IntegrityError`. There is deliberately no code path that puts a scraped or
purchased address into the individual lane.

## Compliance enforcement is structural

Two rules are enforced by the database, not by convention:

1. **Consent** — `CHECK (lane = 'b2b' OR consent_basis IS NOT NULL)` on `leads`.
2. **Suppression** — `BEFORE INSERT` triggers on `email_events` abort any `sent`
   event for a suppressed or opted-out address. The sender writes that event
   *inside an open transaction, before* handing the message to SMTP, so a
   suppressed address cannot be mailed even if calling code forgets to check. If
   SMTP then fails, the transaction rolls back and no false `sent` row survives.

Bypassing either means dropping a database object, not forgetting an `if`.

Every email also carries a CAN-SPAM footer (physical address + working unsubscribe)
plus `List-Unsubscribe` headers, and Claude's system prompt forbids legal-outcome
promises, approval odds, timelines, attorney impersonation, and false urgency. A
regex screen re-checks each draft afterwards and **rejects** anything that slips
through — a prompt is guidance, not a guarantee.

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill it in — .env is gitignored
./cli.py init-db
```

Gmail/Workspace needs an **App Password** (Google Account → Security → 2-Step
Verification → App passwords), never your account password. 2FA must be on.

Required before any live send: `PHYSICAL_ADDRESS`, `UNSUBSCRIBE_BASE_URL`,
`UNSUBSCRIBE_SECRET` (any long random string), `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`.
`send` refuses to run live without them.

## Testing walkthrough — in this order

**1. Schema and guardrails (no credentials, no network)**

```bash
./cli.py init-db
./cli.py stats
```

**2. Apollo, without spending credits**

```bash
./cli.py find-leads --count-only --titles "immigration attorney" --locations "United States"
```

Reports how many people match and consumes zero credits. Then spend exactly two:

```bash
./cli.py find-leads --titles "immigration attorney" --limit 2
./cli.py stats
```

`--limit` is the number of *enrichments*, and enrichment is what costs credits —
one per lead. Every run prints its real consumption and Apollo's own remaining-quota
headers. Repeat runs skip Apollo IDs already in the database, so you never pay twice
for the same person.

**3. Drafting and sending — dry run (nothing leaves the mailbox)**

```bash
./cli.py send --limit 2          # dry run is the default
./cli.py show-drafts
```

Read the drafts in full. Check the footer, the unsubscribe link, the tone, and that
no draft promises an outcome or a timeline. Iterate on `SYSTEM_PROMPT` in
`baseleaf/personalize.py` until you would sign your name to the output.

**4. Send to yourself first**

```bash
./cli.py import-individuals my-own-address.csv    # consent_basis: "internal test"
./cli.py send --lane individual --limit 1 --live
```

Confirm in your own inbox: rendering, footer, unsubscribe link, and that it lands in
Primary rather than Promotions or Spam.

**5. Replies**

```bash
./cli.py scan-replies
```

Tracks progress by IMAP UID and fetches with `BODY.PEEK[]`, so nothing is ever marked
read — triage your inbox normally alongside it. It only reports. To get a *suggested*
reply you read, edit, and send yourself:

```bash
./cli.py suggest-reply someone@firm.com
```

Nothing in this codebase sends a reply automatically.

**6. Cron**

Add repo secrets (`APOLLO_API_KEY`, `ANTHROPIC_API_KEY`, `EMAIL_ADDRESS`,
`EMAIL_APP_PASSWORD`, `PHYSICAL_ADDRESS`, `UNSUBSCRIBE_SECRET`, optionally
`SLACK_WEBHOOK_URL`) and repo variables (`UNSUBSCRIBE_BASE_URL`, `FREE_TOOL_URL`,
`FROM_NAME`, `COMPANY_NAME`, `DAILY_SEND_CAP`). Trigger it manually with **live
unchecked** first and read the logs artifact. Only then let the 6-hour schedule send
for real — the workflow stays dry-run unless `live` is explicitly true.

## Deliverability

The rate limits are the point, not a formality. Gmail publishes 500/day, but single
accounts get flagged well below that from burst-sending behaviour. Defaults:

- `DAILY_SEND_CAP=25` per rolling 24 hours, derived from the event log rather than a
  counter that can drift out of sync
- a random 60-180s pause between individual sends

Warm a new sending domain for 2-3 weeks with real conversational mail before cold
outreach. Keep bounces under ~3%. When volume outgrows one mailbox, the upgrade is a
pool of mailboxes across several domains (or an ESP like Instantly/Smartlead) — at
which point `sender.py` becomes a pool selector rather than a single SMTP login.

## Commands

| Command | Purpose |
|---|---|
| `init-db` | Create the schema |
| `find-leads` | Source b2b leads from Apollo (`--count-only` spends nothing) |
| `import-individuals` | Load opted-in individuals from CSV (consent required) |
| `send` | Draft and send — **dry run unless `--live`** |
| `show-drafts` | Print recent drafts in full |
| `scan-replies` | Poll IMAP, report replies (never auto-replies) |
| `suggest-reply` | Draft a suggested reply for you to send manually |
| `suppress` | Add an address to the suppression list |
| `stats` | Pipeline state |
| `run` | find-leads → send → scan-replies, for cron |

## Notes

- The Apollo key shared during development should be rotated; it lives in `.env`,
  which is gitignored from the first commit.
- `outreach.db` holds lead PII. It is gitignored, and the workflow keeps it in the
  Actions cache rather than the repository. Caches can be evicted after ~7 days of
  no reads, or under the 10GB repo limit — if a run reports zero known leads, the
  cache was dropped and dedup restarts. For anything long-lived, move the database
  to durable storage (a private S3/R2 bucket, or Turso/libSQL).
- `individual`-lane consent records are the evidence for a CASL complaint. Keep the
  signup timestamp and source in `consent_basis` / `consent_source` / `consent_at`.
