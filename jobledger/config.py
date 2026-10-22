"""JobLedger configuration, loaded from .env and environment variables.

Nothing here assumes credentials exist: every ``has_*`` check is explicit and the
CLI reports a precise blocker instead of guessing.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- LLM (provider is configurable; nothing is assumed to be reachable) ---
    llm_provider: str = Field(default="openai")   # openai | anthropic | groq | ollama | gemini | none
    llm_model: str = Field(default="gpt-4o-mini")
    openai_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    groq_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")
    ollama_base_url: str = Field(default="http://localhost:11434")
    llm_timeout: int = Field(default=60)
    llm_max_retries: int = Field(default=2)

    # --- Mailbox (read-only IMAP) ---
    imap_host: str = Field(default="imap.gmail.com")
    imap_port: int = Field(default=993)
    imap_user: str = Field(default="")
    imap_password: str = Field(default="")
    imap_folder: str = Field(default="INBOX")
    email_filter_subjects: str = Field(default="job alert,new jobs,jobs for you,hiring,opportunity")
    email_reply_subjects: str = Field(default="your application,interview,application update")
    email_since_days: int = Field(default=30)
    email_max_messages: int = Field(default=50)

    # --- App ---
    db_path: str = Field(default="jobledger.db")
    export_dir: str = Field(default="exports")
    log_dir: str = Field(default="logs")
    log_level: str = Field(default="INFO")
    jd_max_bytes: int = Field(default=500_000)
    http_timeout: int = Field(default=15)
    max_retries: int = Field(default=3)
    followup_wait_days: int = Field(default=7)
    max_run_steps: int = Field(default=200)
    max_jd_attempts: int = Field(default=3)
    min_inline_jd_chars: int = Field(default=400)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "case_sensitive": False,
    }

    @property
    def email_filter_list(self) -> List[str]:
        return [s.strip().lower() for s in self.email_filter_subjects.split(",") if s.strip()]

    @property
    def email_reply_list(self) -> List[str]:
        return [s.strip().lower() for s in self.email_reply_subjects.split(",") if s.strip()]

    def db_abs_path(self) -> str:
        return str(Path(self.db_path).resolve())

    def llm_api_key(self) -> str:
        return {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "groq": self.groq_api_key,
            "gemini": self.gemini_api_key,
        }.get(self.llm_provider.lower(), "")

    def has_llm(self) -> bool:
        """True when a provider is configured. Reachability is NOT checked here."""
        p = self.llm_provider.lower()
        if p == "ollama":
            return bool(self.ollama_base_url)
        return bool(self.llm_api_key())

    def has_email(self) -> bool:
        return bool(self.imap_user and self.imap_password)

    def redacted_summary(self) -> dict:
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_configured": self.has_llm(),
            "imap_host": self.imap_host,
            "imap_user": self.imap_user,
            "imap_password_set": bool(self.imap_password),
            "db_path": self.db_path,
            "log_level": self.log_level,
        }


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Drop the cached settings (tests / CLI --env-file switches)."""
    global _settings
    _settings = None
