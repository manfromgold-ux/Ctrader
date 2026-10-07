"""Runtime configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, optional quotes. Never overrides real env vars."""
    if not path.is_file():
        return
    # utf-8-sig drops the byte-order mark Windows Notepad may add; splitlines() handles CRLF.
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value else default


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


def _env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


# Sites that are off-limits: login walls, personal data, or platforms that aggressively litigate scraping.
DEFAULT_BLOCKED_DOMAINS = [
    "linkedin.com", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
    "threads.net", "snapchat.com", "pinterest.com", "reddit.com", "google.com", "youtube.com",
    "amazon.com", "ebay.com", "craigslist.org", "glassdoor.com", "indeed.com", "zillow.com",
    "ticketmaster.com", "whatsapp.com", "telegram.org", "discord.com", "onlyfans.com",
]


@dataclass
class Config:
    data_dir: Path
    apify_token: str
    openrouter_key: str

    free_models: list[str] = field(default_factory=list)
    paid_model: str = "auto"
    max_paid_price_per_mtok: float = 3.0
    llm_monthly_budget_usd: float = 15.0

    price_per_1000_results: float = 2.0
    max_new_actors_per_week: int = 5
    max_live_actors: int = 60
    apify_monthly_cap_usd: float = 25.0
    min_age_days_before_prune: int = 60
    clone_threshold_users30: int = 10
    retire_after_maintenance_days: int = 14

    heal_interval_hours: float = 6.0
    scout_interval_hours: float = 24.0
    spawn_interval_hours: float = 24.0
    prune_interval_hours: float = 168.0
    report_interval_hours: float = 168.0

    max_build_attempts: int = 3
    max_heal_attempts: int = 3
    html_prompt_chars: int = 60_000

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    report_email_to: str = ""

    blocked_domains: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_DOMAINS))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reef.db"

    @property
    def pause_file(self) -> Path:
        return self.data_dir / "PAUSE"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Config":
        load_dotenv(env_file or Path(_env("REEF_ENV_FILE", ".env")))
        data_dir = Path(_env("REEF_DATA_DIR", "data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        free = _env_list("REEF_FREE_MODELS", "auto")
        return cls(
            data_dir=data_dir,
            apify_token=_env("APIFY_TOKEN"),
            openrouter_key=_env("OPENROUTER_API_KEY"),
            free_models=[] if free == ["auto"] else free,
            paid_model=_env("REEF_PAID_MODEL", "auto"),
            max_paid_price_per_mtok=_env_float("REEF_MAX_PAID_PRICE_PER_MTOK", 3.0),
            llm_monthly_budget_usd=_env_float("REEF_LLM_MONTHLY_BUDGET_USD", 15.0),
            price_per_1000_results=_env_float("REEF_PRICE_PER_1000_RESULTS", 2.0),
            max_new_actors_per_week=_env_int("REEF_MAX_NEW_ACTORS_PER_WEEK", 5),
            max_live_actors=_env_int("REEF_MAX_LIVE_ACTORS", 60),
            apify_monthly_cap_usd=_env_float("REEF_APIFY_MONTHLY_CAP_USD", 25.0),
            min_age_days_before_prune=_env_int("REEF_MIN_AGE_DAYS_BEFORE_PRUNE", 60),
            clone_threshold_users30=_env_int("REEF_CLONE_THRESHOLD_USERS30", 10),
            retire_after_maintenance_days=_env_int("REEF_RETIRE_AFTER_MAINTENANCE_DAYS", 14),
            heal_interval_hours=_env_float("REEF_HEAL_INTERVAL_HOURS", 6.0),
            scout_interval_hours=_env_float("REEF_SCOUT_INTERVAL_HOURS", 24.0),
            spawn_interval_hours=_env_float("REEF_SPAWN_INTERVAL_HOURS", 24.0),
            prune_interval_hours=_env_float("REEF_PRUNE_INTERVAL_HOURS", 168.0),
            report_interval_hours=_env_float("REEF_REPORT_INTERVAL_HOURS", 168.0),
            max_build_attempts=_env_int("REEF_MAX_BUILD_ATTEMPTS", 3),
            max_heal_attempts=_env_int("REEF_MAX_HEAL_ATTEMPTS", 3),
            html_prompt_chars=_env_int("REEF_HTML_PROMPT_CHARS", 60_000),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_user=_env("SMTP_USER"),
            smtp_password=_env("SMTP_PASSWORD"),
            report_email_to=_env("REPORT_EMAIL_TO"),
            blocked_domains=DEFAULT_BLOCKED_DOMAINS + _env_list("REEF_EXTRA_BLOCKED_DOMAINS"),
        )

    def missing(self) -> list[str]:
        """Names of required settings that are not configured."""
        required = {
            "APIFY_TOKEN": self.apify_token,
            "OPENROUTER_API_KEY": self.openrouter_key,
        }
        return [name for name, value in required.items() if not value]
