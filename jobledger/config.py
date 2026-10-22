"""
JobLedger configuration loaded from .env and environment variables.
"""
from __future__ import annotations
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # LLM
    llm_provider: str = Field(default="openai")
    llm_model: str = Field(default="gpt-4o-mini")
    openai_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    groq_api_key: str = Field(default="")
    ollama_base_url: str = Field(default="http://localhost:11434")
    gemini_api_key: str = Field(default="")

    # Email IMAP
    imap_host: str = Field(default="imap.gmail.com")
    imap_port: int = Field(default=993)
    imap_user: str = Field(default="")
    imap_password: str = Field(default="")
    imap_folder: str = Field(default="INBOX")
    email_filter_subjects: str = Field(
        default="job alert,new jobs,jobs for you,hiring,opportunity"
    )

    # App
    db_path: str = Field(default="jobledger.db")
    export_dir: str = Field(default="exports")
    log_dir: str = Field(default="logs")
    log_level: str = Field(default="INFO")
    jd_max_bytes: int = Field(default=500_000)
    http_timeout: int = Field(default=15)
    max_retries: int = Field(default=3)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "case_sensitive": False,
    }

    @property
    def email_filter_list(self) -> List[str]:
        return [s.strip().lower() for s in self.email_filter_subjects.split(",") if s.strip()]

    def db_abs_path(self) -> str:
        return str(Path(self.db_path).resolve())

    def has_llm(self) -> bool:
        p = self.llm_provider.lower()
        if p == "openai":
            return bool(self.openai_api_key)
        if p == "anthropic":
            return bool(self.anthropic_api_key)
        if p == "groq":
            return bool(self.groq_api_key)
        if p == "ollama":
            return True
        if p == "gemini":
            return bool(self.gemini_api_key)
        return False

    def has_email(self) -> bool:
        return bool(self.imap_user and self.imap_password)

    def redacted_summary(self) -> dict:
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "has_llm_key": self.has_llm(),
            "imap_host": self.imap_host,
            "imap_user": self.imap_user,
            "has_email_password": bool(self.imap_password),
            "db_path": self.db_path,
            "log_level": self.log_level,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
