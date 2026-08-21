"""Reporting sinks: a log file, and optionally a Slack webhook."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from .config import settings
from .db import utcnow

log = logging.getLogger(__name__)


def write_report(lines: list[str], filename: str = "replies.log") -> Path:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    path = settings.log_dir / filename
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(f"{utcnow()}  {line}\n")
    return path


def post_slack(text: str, webhook_url: str | None = None) -> bool:
    """Post to a Slack incoming webhook. Returns False on failure, never raises."""
    url = webhook_url or settings.slack_webhook_url
    if not url:
        return False
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Slack notification failed: %s", exc)
        return False
