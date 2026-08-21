"""Apollo.io client for the b2b lane.

Verified against the live API on 2026-08-20:

* `mixed_people/search` is DEPRECATED for API callers and returns HTTP 422.
  The current endpoint is `mixed_people/api_search`.
* `api_search` returns *obfuscated stubs only* — `last_name_obfuscated`
  ("Ga***n"), and boolean flags (`has_email`, `has_city`) instead of values.
  It is a discovery/counting tool, not a source of contact data.
* `people/match` returns the full record (real email, linkedin_url, company
  domain, industry) and consumes one credit per person.

So sourcing is two-stage: search to discover, match to enrich. Credits are
only spent in stage two, which is why enrichment is explicitly capped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.apollo.io/api/v1"
SEARCH_ENDPOINT = f"{BASE_URL}/mixed_people/api_search"
MATCH_ENDPOINT = f"{BASE_URL}/people/match"
HEALTH_ENDPOINT = f"{BASE_URL}/auth/health"

# Apollo caps `page * per_page` at 50,000 records for search pagination.
MAX_PER_PAGE = 100


class ApolloError(RuntimeError):
    """Apollo returned an error or an unexpected payload."""


@dataclass
class RateLimit:
    """Parsed from response headers. Never assumed — always read from Apollo."""

    minute_left: int | None = None
    minute_limit: int | None = None
    hourly_left: int | None = None
    hourly_limit: int | None = None
    daily_left: int | None = None
    daily_limit: int | None = None

    @classmethod
    def from_headers(cls, headers: Any) -> "RateLimit":
        def _int(key: str) -> int | None:
            raw = headers.get(key)
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            minute_left=_int("x-minute-requests-left"),
            minute_limit=_int("x-rate-limit-minute"),
            hourly_left=_int("x-hourly-requests-left"),
            hourly_limit=_int("x-rate-limit-hourly"),
            daily_left=_int("x-24-hour-requests-left"),
            daily_limit=_int("x-rate-limit-24-hour"),
        )

    def summary(self) -> str:
        return (
            f"minute {self.minute_left}/{self.minute_limit} | "
            f"hour {self.hourly_left}/{self.hourly_limit} | "
            f"24h {self.daily_left}/{self.daily_limit}"
        )


@dataclass
class RunUsage:
    """Per-run accounting so each run reports what it actually consumed."""

    search_calls: int = 0
    match_calls: int = 0
    emails_revealed: int = 0
    last_rate_limit: RateLimit = field(default_factory=RateLimit)

    @property
    def credits_estimate(self) -> int:
        """One Apollo credit per successful people/match reveal."""
        return self.emails_revealed

    def report(self) -> str:
        return (
            f"Apollo usage this run: {self.search_calls} search call(s), "
            f"{self.match_calls} match call(s), {self.emails_revealed} email(s) revealed "
            f"(~{self.credits_estimate} credit(s)). "
            f"Remaining per Apollo headers: {self.last_rate_limit.summary()}"
        )


class ApolloClient:
    def __init__(self, api_key: str, timeout: int = 30) -> None:
        if not api_key:
            raise ApolloError("APOLLO_API_KEY is not set")
        self._key = api_key
        self.timeout = timeout
        self.usage = RunUsage()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "accept": "application/json",
                "x-api-key": api_key,
            }
        )

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._session.post(url, json=payload, timeout=self.timeout)
        self.usage.last_rate_limit = RateLimit.from_headers(resp.headers)

        if resp.status_code == 429:
            raise ApolloError(
                f"Apollo rate limit hit ({self.usage.last_rate_limit.summary()}). "
                "Reduce batch size or wait before retrying."
            )
        if resp.status_code >= 400:
            raise ApolloError(f"Apollo HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise ApolloError(f"Apollo error: {data['error']}")
        return data

    def health(self) -> bool:
        resp = self._session.get(HEALTH_ENDPOINT, timeout=self.timeout)
        return bool(resp.ok and resp.json().get("is_logged_in"))

    def search(
        self,
        *,
        titles: list[str] | None = None,
        locations: list[str] | None = None,
        industries: list[str] | None = None,
        employee_ranges: list[str] | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> tuple[list[dict[str, Any]], int]:
        """Discovery only. Returns obfuscated stubs plus the total match count."""
        payload: dict[str, Any] = {"page": page, "per_page": min(per_page, MAX_PER_PAGE)}
        if titles:
            payload["person_titles"] = titles
        if locations:
            payload["person_locations"] = locations
        if industries:
            payload["q_organization_keyword_tags"] = industries
        if employee_ranges:
            payload["organization_num_employees_ranges"] = employee_ranges

        data = self._post(SEARCH_ENDPOINT, payload)
        self.usage.search_calls += 1

        people = data.get("people") or []
        total = int(data.get("total_entries") or 0)
        log.info(
            "Apollo search page=%s returned %s stub(s) of %s total | %s",
            page, len(people), total, self.usage.last_rate_limit.summary(),
        )
        return people, total

    def match(self, apollo_id: str, *, reveal_personal_emails: bool = False) -> dict[str, Any] | None:
        """Enrich one person. COSTS A CREDIT. Returns None when nothing matched.

        `reveal_personal_emails` stays off by default: personal addresses carry
        heavier consent expectations and cost more, and the b2b lane wants work
        emails anyway.
        """
        payload: dict[str, Any] = {"id": apollo_id}
        if reveal_personal_emails:
            payload["reveal_personal_emails"] = True

        data = self._post(MATCH_ENDPOINT, payload)
        self.usage.match_calls += 1

        person = data.get("person")
        if not person:
            return None
        if person.get("email"):
            self.usage.emails_revealed += 1
        return person

    def search_and_enrich(
        self,
        *,
        titles: list[str] | None = None,
        locations: list[str] | None = None,
        industries: list[str] | None = None,
        limit: int = 10,
        per_page: int = 25,
        skip_ids: set[str] | None = None,
        reveal_personal_emails: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield fully enriched people, spending at most `limit` credits.

        Stubs whose `has_email` flag is false are skipped before enrichment, so
        credits are not burned on records Apollo already says have no email.
        """
        skip_ids = skip_ids or set()
        enriched = 0
        page = 1

        while enriched < limit:
            stubs, total = self.search(
                titles=titles, locations=locations, industries=industries,
                page=page, per_page=per_page,
            )
            if not stubs:
                break

            for stub in stubs:
                if enriched >= limit:
                    break
                apollo_id = stub.get("id")
                if not apollo_id or apollo_id in skip_ids:
                    continue
                if not stub.get("has_email"):
                    log.debug("Skipping %s: Apollo reports no email on file", apollo_id)
                    continue

                person = self.match(apollo_id, reveal_personal_emails=reveal_personal_emails)
                if person and person.get("email"):
                    enriched += 1
                    yield person

            if page * per_page >= total:
                break
            page += 1
