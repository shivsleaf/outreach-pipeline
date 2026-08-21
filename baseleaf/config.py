"""Configuration loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secrets are never logged or printed."""

    # storage
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH") or PROJECT_ROOT / "outreach.db"))
    log_dir: Path = field(default_factory=lambda: Path(_env("LOG_DIR") or PROJECT_ROOT / "logs"))

    # apollo
    apollo_api_key: str | None = field(default_factory=lambda: _env("APOLLO_API_KEY"))

    # anthropic
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL") or "claude-haiku-4-5")
    anthropic_model_strong: str = field(
        default_factory=lambda: _env("ANTHROPIC_MODEL_STRONG") or "claude-sonnet-5"
    )

    # smtp / imap (single Gmail or Workspace account, App Password auth)
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST") or "smtp.gmail.com")
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 587))
    imap_host: str = field(default_factory=lambda: _env("IMAP_HOST") or "imap.gmail.com")
    imap_port: int = field(default_factory=lambda: _env_int("IMAP_PORT", 993))
    email_address: str | None = field(default_factory=lambda: _env("EMAIL_ADDRESS"))
    email_app_password: str | None = field(default_factory=lambda: _env("EMAIL_APP_PASSWORD"))
    from_name: str = field(default_factory=lambda: _env("FROM_NAME") or "Baseleaf")
    reply_to: str | None = field(default_factory=lambda: _env("REPLY_TO"))

    # sending guardrails
    daily_send_cap: int = field(default_factory=lambda: _env_int("DAILY_SEND_CAP", 25))
    send_delay_min_seconds: int = field(default_factory=lambda: _env_int("SEND_DELAY_MIN_SECONDS", 60))
    send_delay_max_seconds: int = field(default_factory=lambda: _env_int("SEND_DELAY_MAX_SECONDS", 180))

    # compliance (CAN-SPAM: physical address + working unsubscribe are mandatory)
    company_name: str = field(default_factory=lambda: _env("COMPANY_NAME") or "Baseleaf")
    physical_address: str | None = field(default_factory=lambda: _env("PHYSICAL_ADDRESS"))
    unsubscribe_base_url: str | None = field(default_factory=lambda: _env("UNSUBSCRIBE_BASE_URL"))
    unsubscribe_secret: str | None = field(default_factory=lambda: _env("UNSUBSCRIBE_SECRET"))
    free_tool_url: str = field(
        default_factory=lambda: _env("FREE_TOOL_URL") or "https://baseleaf.com/eligibility-check"
    )

    # notifications
    slack_webhook_url: str | None = field(default_factory=lambda: _env("SLACK_WEBHOOK_URL"))

    def require(self, *names: str) -> None:
        """Fail fast when a command needs config that is not present."""
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            env_names = [n.upper() for n in missing]
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(env_names)
                + ". Add them to .env (see .env.example)."
            )

    def require_sending(self) -> None:
        """Everything needed before a single real email may leave the box."""
        self.require(
            "email_address",
            "email_app_password",
            "physical_address",
            "unsubscribe_base_url",
            "unsubscribe_secret",
        )


settings = Settings()
