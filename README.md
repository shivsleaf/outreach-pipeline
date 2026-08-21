# Baseleaf outreach pipeline

Terminal-only lead sourcing and cold outreach for Baseleaf. Python 3.11+, SQLite,
three dependencies, no UI. Every email points at a free tool (the green-card
eligibility checker), which is the conversion path into the paid product.

```
find-leads ──▶ SQLite ──▶ draft (Claude) ──▶ send (SMTP) ──▶ scan-replies (IMAP) ──▶ notify
  Apollo       dedup +     per-lead prompt    cap + jitter     UID cursor,           log file
  2-stage      consent     + guardrails       dry-run default  never marks read      + Slack
```

---

## Current status

| Piece | State |
|---|---|
| SQLite schema, dedup, consent + suppression enforcement | **Verified** — 8/8 tests |
| Apollo sourcing | **Verified against the live API** — 2 real leads in the DB, 4 credits spent |
| Claude prompt assembly from SQLite | **Verified** — exact payload inspected |
| Compliance screen on drafts | **Verified** — 7/7 violation classes caught |
| Send machinery: cap, jitter, footer, rollback | **Verified** — 7/7 tests, fake SMTP |
| Notify: Slack webhook + log file | **Verified** — 6/6 tests, local webhook server |
| Live Claude API call | **Untested** — needs `ANTHROPIC_API_KEY` |
| Live SMTP / IMAP | **Untested** — needs a Gmail App Password |

The two untested pieces are untested because this machine has no Claude key and no
mailbox credentials — not because they're unfinished. Both fail with a clear config
error rather than a crash. The SDK check confirms `output_config` is accepted by the
installed `anthropic` 1.0.0, so the drafting call is built against the right API.

---

## The two lanes — do not merge them

Apollo sells **professional/company data** — titles, work emails, LinkedIn. It has no
"person is currently applying for a green card" filter, because that's consumer intent
data Apollo doesn't carry.

| Lane | Source | Legal basis | Entry point |
|---|---|---|---|
| `b2b` | Apollo | CAN-SPAM opt-out model | `find-leads` |
| `individual` | Baseleaf landing pages **only** | **CASL opt-in, recorded before sending** | `import-individuals` |

You're in Ontario. CASL requires consent to exist **before** a commercial email reaches
an individual — unlike the US opt-out model. Enforced by a CHECK constraint: an
individual-lane row without `consent_basis` raises `IntegrityError`. There is
deliberately no code path putting a scraped or purchased address into that lane.

## Compliance is structural, not conventional

Two rules live in the database, not in application code:

1. **Consent** — `CHECK (lane = 'b2b' OR consent_basis IS NOT NULL)` on `leads`.
2. **Suppression** — `BEFORE INSERT` triggers on `email_events` abort any `sent` event
   for a suppressed or opted-out address. The sender writes that event *inside an open
   transaction, before* handing the message to SMTP — so a suppressed address cannot be
   mailed even if calling code forgets to check. If SMTP then fails, the transaction
   rolls back and no false `sent` row survives.

Bypassing either means dropping a database object, not forgetting an `if`.

Every email also carries a CAN-SPAM footer (physical address + working unsubscribe) and
`List-Unsubscribe` headers. Claude's system prompt forbids outcome promises, approval
odds, timelines, attorney impersonation, and false urgency — and a regex screen
re-checks every draft afterwards and **rejects** failures. A prompt is guidance; the
screen is the guarantee.

## How Claude gets told what to say to whom

Per lead, `personalize.py` assembles two parts and enforces the response shape:

- **System prompt** (identical every call) — the absolute rules and style constraints.
- **User message** (built per lead from the SQLite row) — a lane-specific angle plus
  `first_name`, `title`, `company`, `industry`, `location`. Nothing else. The prompt
  explicitly forbids inventing facts beyond this context.
- **Response** — `output_config` with a JSON schema, so `subject` and `body` come back
  schema-guaranteed rather than parsed out of prose.

Cheap tier (`claude-haiku-4-5`) by design: this is three sentences from six structured
fields. `ANTHROPIC_MODEL_STRONG` is there if drafting quality ever justifies it.

---

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill it in — .env is gitignored
./cli.py init-db
```

Gmail/Workspace needs an **App Password** (Google Account → Security → 2-Step
Verification → App passwords), never your account password. 2FA must be on first.

Required before any live send — `send --live` refuses without them:
`EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `PHYSICAL_ADDRESS`, `UNSUBSCRIBE_BASE_URL`,
`UNSUBSCRIBE_SECRET` (any long random string).

---

## NEXT STEPS — in this order

### 1. Add your Claude key, then prove drafting works

```bash
# put ANTHROPIC_API_KEY=sk-ant-... in .env, then:
./cli.py send --limit 1        # dry run — the default, sends nothing
./cli.py show-drafts
```

This is the first genuinely untested step. You're checking three things: the API call
succeeds, the draft reads like something you'd sign your name to, and the footer is
present. Iterate on `SYSTEM_PROMPT` in `baseleaf/personalize.py` until it does.

If a draft gets rejected by the compliance screen, that's the screen working — the log
names which rule fired.

### 2. Fill in the compliance fields

`PHYSICAL_ADDRESS` must be a real mailing address you control — it's a CAN-SPAM
requirement, not decoration. `UNSUBSCRIBE_BASE_URL` must point at a page that actually
records the opt-out. **The pipeline generates working unsubscribe links but does not
host the endpoint** — that page is yours to build. Until it exists, unsubscribes only
work via the reply path (step 4).

### 3. Send to yourself before anyone else

```bash
# a CSV with your own address and consent_basis "internal test"
./cli.py import-individuals my-own-address.csv
./cli.py send --lane individual --limit 1 --live
```

Check in your own inbox: rendering, footer, unsubscribe link, and whether it lands in
Primary rather than Promotions or Spam.

### 4. Replies

```bash
./cli.py scan-replies
```

Tracks progress by IMAP UID and fetches with `BODY.PEEK[]`, so nothing is ever marked
read — keep triaging your inbox normally alongside it. Reports to `logs/replies.log`
and to Slack if `SLACK_WEBHOOK_URL` is set. Auto-suppresses anyone whose reply asks to
be removed. It never sends anything. For a *suggested* reply you read, edit, and send
yourself:

```bash
./cli.py suggest-reply someone@firm.com
```

### 5. Only then, the cron

Add repo **secrets** (`APOLLO_API_KEY`, `ANTHROPIC_API_KEY`, `EMAIL_ADDRESS`,
`EMAIL_APP_PASSWORD`, `PHYSICAL_ADDRESS`, `UNSUBSCRIBE_SECRET`, optionally
`SLACK_WEBHOOK_URL`) and repo **variables** (`UNSUBSCRIBE_BASE_URL`, `FREE_TOOL_URL`,
`FROM_NAME`, `COMPANY_NAME`, `DAILY_SEND_CAP`).

Trigger it manually with **live unchecked** first, read the logs artifact, and only then
let the 6-hour schedule send for real. The workflow stays dry-run unless `live` is
explicitly true.

### 6. Rotate the Apollo key

The key used during development was shared in a chat transcript. Rotate it when the
trial ends.

---

## Deliverability — the limits are the point

Gmail publishes 500/day, but single accounts get flagged well below that from
burst-sending behaviour. Defaults:

- `DAILY_SEND_CAP=25` per rolling 24 hours, derived from the event log rather than a
  counter that can drift
- random 60–180s pause between sends

Warm a new sending domain for 2–3 weeks with real conversational mail before cold
outreach. Keep bounces under ~3%. When volume outgrows one mailbox, the upgrade is a
pool of mailboxes across several domains (or an ESP like Instantly/Smartlead), at which
point `sender.py` becomes a pool selector rather than a single SMTP login.

---

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

## Apollo notes

Verified live on 2026-08-20:

- `mixed_people/search` is **deprecated for API callers** and returns HTTP 422. The
  current endpoint is `mixed_people/api_search`.
- `api_search` returns **obfuscated stubs only** — `last_name_obfuscated: "Ga***n"`,
  and boolean flags (`has_email`) instead of values. It's a discovery tool.
- `people/match` returns the real record and **costs one credit per person**.

So `--limit` caps *enrichments*, which is what costs credits. Every run prints its real
consumption plus Apollo's own remaining-quota headers. Repeat runs skip Apollo IDs
already stored, so you never pay twice for the same person. Verified quota on the trial:
50/min, 200/hour, 600/24h.

## Files

```
cli.py                     entrypoint, 10 subcommands
baseleaf/config.py         .env loading, fail-fast validation
baseleaf/db.py             schema, CHECK constraint, 3 triggers, sendable_leads view
baseleaf/apollo.py         two-stage search → enrich, credit accounting
baseleaf/leads.py          upsert, dedup, consent gate, CSV import
baseleaf/personalize.py    Claude drafting, guardrail prompt, compliance screen
baseleaf/compliance.py     HMAC unsubscribe tokens, CAN-SPAM footer
baseleaf/sender.py         SMTP, rolling cap, jitter, dry-run default
baseleaf/replies.py        IMAP UID cursor, BODY.PEEK, report-only
baseleaf/notify.py         log file + Slack webhook
.github/workflows/         6-hourly cron, DB persisted via rolling cache
```

## Warnings

- **Commit the `.py` files.** An earlier commit staged only `README.md`, `.gitignore`,
  `outreach.db`, `logs/`, and `__pycache__/*.pyc` — no source — and a later "remove
  generated files" commit deleted the working tree copies. Git could not restore them
  because they had never been committed. Run `git add -A && git status` and confirm the
  10 `.py` files are staged before committing.
- `outreach.db` holds lead PII, and `.env` holds live API keys. Both are gitignored.
  Never commit either; note the current `outreach.db` **is** tracked from that earlier
  commit — `git rm --cached outreach.db` to untrack it.
- The Actions cache holding `outreach.db` can be evicted after ~7 days idle, which
  resets dedup and the rate-limit window. For anything long-lived, move the database to
  durable storage (a private S3/R2 bucket, or Turso/libSQL).
- `individual`-lane consent records are your evidence in a CASL complaint. Keep the
  signup timestamp and source in `consent_basis` / `consent_source` / `consent_at`.
