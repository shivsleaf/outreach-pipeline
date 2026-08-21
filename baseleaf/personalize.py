"""Claude-backed email drafting with hard compliance guardrails.

Model choice: the cheap tier (`claude-haiku-4-5`) drafts three sentences from a
handful of structured fields, which does not need a frontier model. It also
supports structured outputs, so the subject/body split is schema-guaranteed
rather than parsed out of prose.

Two layers of protection, because a system prompt is guidance and not a
guarantee:

1. The system prompt forbids legal-outcome promises, attorney impersonation,
   and manufactured urgency.
2. `screen_draft()` re-checks the generated text against banned phrasing after
   the fact. A draft that fails is rejected rather than sent.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You write short, plain cold outreach emails for Baseleaf, a company that builds \
self-service immigration information tools (such as a free green-card \
eligibility checker).

The goal of every email is to get the reader to try the free tool. It is not to \
give advice, and not to close a sale in the email.

ABSOLUTE RULES — a draft that breaks any of these is unusable:
- Never promise or imply a specific legal outcome, approval, approval odds, or \
percentage chance of success.
- Never promise or imply a specific timeline ("green card in 6 months", \
"processed within X weeks"). Government processing times are not ours to promise.
- Never state or imply that the sender is a lawyer, attorney, or licensed legal \
representative, and never imply Baseleaf is a law firm. Baseleaf is not a law firm \
and does not give legal advice.
- Never use false urgency ("act now", "deadline tonight", "last chance", \
"limited spots"), and never write a subject line that misrepresents the content \
(no fake "Re:" or "Fwd:", no fake reply threads).
- Never claim an existing relationship, prior conversation, or referral that was \
not supplied to you in the lead context.
- Never invent facts about the recipient or their company. Use only the supplied \
context. If context is thin, write something short and general instead of guessing.

STYLE:
- 60-110 words in the body. Three short paragraphs at most.
- Plain sentences. No marketing throat-clearing, no exclamation marks, no emoji.
- One clear call to action: try the free tool at the supplied URL.
- Subject line: under 60 characters, lowercase or sentence case, descriptive and \
honest. No clickbait.
- Sign off as the sender name supplied. Do not invent a job title.
- Do not write a signature block, legal disclaimer, physical address, or \
unsubscribe line. Those are appended automatically afterwards.\
"""

# Post-generation screen. Deliberately blunt: a false positive costs one retry,
# a false negative sends a non-compliant claim to a real inbox.
BANNED_PATTERNS: list[tuple[str, str]] = [
    (r"\bguarantee\w*\b", "promises a guaranteed outcome"),
    (r"\bapprov(?:al|ed)\s+(?:is\s+)?(?:guaranteed|assured|certain)\b", "promises approval"),
    (r"\b\d{1,3}\s?%\s*(?:approval|success|chance|odds)", "quotes approval odds"),
    (r"\b(?:we|i)\s+(?:are|am)\s+(?:a\s+)?(?:licensed\s+)?(?:attorney|lawyer|law firm)\b",
     "claims to be an attorney or law firm"),
    (r"\bour\s+(?:attorneys|lawyers)\b", "implies in-house legal counsel"),
    (r"\blegal advice\b", "offers legal advice"),
    (r"\bact now\b|\blast chance\b|\bdeadline\s+(?:tonight|today)\b|\blimited spots\b",
     "uses manufactured urgency"),
    (r"\bwithin\s+\d+\s+(?:days|weeks|months)\b.*\b(?:green card|approval|visa)\b",
     "promises a processing timeline"),
    (r"\bgreen card in\s+\d+\b", "promises a processing timeline"),
]

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "Honest subject line, under 60 characters."},
        "body": {"type": "string", "description": "Email body, 60-110 words, no signature block."},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}


class PersonalizationError(RuntimeError):
    """Drafting failed or produced non-compliant copy."""


@dataclass
class Draft:
    subject: str
    body: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


def screen_draft(subject: str, body: str) -> list[str]:
    """Return a list of guardrail violations. Empty list means the draft passed."""
    text = f"{subject}\n{body}"
    violations = []
    for pattern, reason in BANNED_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            violations.append(reason)
    if re.match(r"^\s*(re|fwd)\s*:", subject, flags=re.IGNORECASE):
        violations.append("fake reply/forward subject line")
    return violations


def _lead_context(lead: dict[str, Any]) -> str:
    fields = [
        ("Name", lead.get("first_name")),
        ("Title", lead.get("title")),
        ("Company", lead.get("company")),
        ("Industry", lead.get("industry")),
        ("Location", lead.get("location")),
        ("Lane", lead.get("lane")),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def draft_email(lead: dict[str, Any], *, sender_name: str | None = None, model: str | None = None) -> Draft:
    """Draft one personalised email. Raises PersonalizationError on a failed screen."""
    import anthropic  # imported lazily so `find-leads` never needs the SDK

    if not settings.anthropic_api_key:
        raise PersonalizationError("ANTHROPIC_API_KEY is not set")

    model = model or settings.anthropic_model
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    lane = lead.get("lane")
    if lane == "b2b":
        angle = (
            "This is a business contact (an immigration attorney, HR/global mobility lead, "
            "recruiter, or relocation specialist). Frame the free eligibility checker as "
            "something that saves their team time on intake screening or that they can pass "
            "to the people they advise. Do not write as if they personally need a green card."
        )
    else:
        angle = (
            "This person opted in through a Baseleaf page and asked to hear from us. "
            "Frame the free eligibility checker as the next step for their own situation. "
            "Reference that they signed up, but do not invent details about their case."
        )

    user_prompt = (
        f"{angle}\n\n"
        f"Lead context:\n{_lead_context(lead)}\n\n"
        f"Free tool URL: {settings.free_tool_url}\n"
        f"Sender name: {sender_name or settings.from_name}\n\n"
        "Write the subject line and body."
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": DRAFT_SCHEMA}},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIStatusError as exc:
        raise PersonalizationError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise PersonalizationError(f"Could not reach the Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise PersonalizationError("Claude declined to draft this email")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PersonalizationError(f"Could not parse the drafted email: {exc}") from exc

    subject = (payload.get("subject") or "").strip()
    body = (payload.get("body") or "").strip()
    if not subject or not body:
        raise PersonalizationError("Claude returned an empty subject or body")

    violations = screen_draft(subject, body)
    if violations:
        raise PersonalizationError(
            "Draft rejected by compliance screen: " + "; ".join(violations)
        )

    return Draft(
        subject=subject,
        body=body,
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def suggest_reply(lead: dict[str, Any], thread_text: str, *, model: str | None = None) -> str:
    """Draft a SUGGESTED reply for a human to read, edit, and send manually.

    Never wired to the sender. `scan-replies` only reports; nothing in this
    codebase sends a reply automatically.
    """
    import anthropic

    if not settings.anthropic_api_key:
        raise PersonalizationError("ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=model or settings.anthropic_model,
        max_tokens=1024,
        system=SYSTEM_PROMPT + (
            "\n\nYou are now drafting a SUGGESTED reply to a real person who wrote back. "
            "A human will read and edit it before anything is sent. Answer what they "
            "actually asked. If the question needs a licensed attorney or depends on "
            "case-specific facts, say plainly that Baseleaf cannot advise on it and "
            "suggest they speak to an immigration attorney."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Lead context:\n{_lead_context(lead)}\n\n"
                f"Their message:\n{thread_text[:4000]}\n\n"
                "Draft a short suggested reply."
            ),
        }],
    )
    if response.stop_reason == "refusal":
        raise PersonalizationError("Claude declined to draft a reply")
    return next((b.text for b in response.content if b.type == "text"), "").strip()
