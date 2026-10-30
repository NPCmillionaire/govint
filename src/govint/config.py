"""Settings: data paths and API keys, read from env vars or ~/.config/govint/config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MIRROR = "https://github.com/NPCmillionaire/govint-data/releases/download"


def _config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "govint" / "config.toml"


def _data_dir() -> Path:
    if env := os.environ.get("GOVINT_HOME"):
        return Path(env)
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "govint"


@dataclass
class Settings:
    contact: str | None  # email for the SEC User-Agent (SEC rejects requests without one)
    data_gov_key: str  # api.data.gov key, used by FEC
    sam_key: str | None  # SAM.gov key; SAM is skipped without it
    data_dir: Path
    mirror: str = DEFAULT_MIRROR  # GitHub releases base for prebuilt datasets
    ntfy: str | None = None  # ntfy topic (or full URL) for watch alerts
    lda_key: str | None = None  # lda.gov API key: 120 req/min instead of 15
    courtlistener_token: str | None = None  # CourtListener API token (free account)

    @property
    def user_agent(self) -> str:
        # SEC wants exactly "name email"; extra text gets a 403.
        if self.contact:
            return f"govint/0.1 {self.contact}"
        return "govint/0.1 (+https://github.com/NPCmillionaire/govint)"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "cache.sqlite"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "datasets.sqlite"


def load() -> Settings:
    file: dict = {}
    path = _config_file()
    if path.exists():
        file = tomllib.loads(path.read_text())

    def pick(env: str, key: str) -> str | None:
        return os.environ.get(env) or file.get(key) or None

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        contact=pick("GOVINT_CONTACT", "contact"),
        data_gov_key=pick("GOVINT_DATA_GOV_KEY", "data_gov_key") or "DEMO_KEY",
        sam_key=pick("GOVINT_SAM_KEY", "sam_key"),
        data_dir=data_dir,
        mirror=pick("GOVINT_MIRROR", "mirror") or DEFAULT_MIRROR,
        ntfy=pick("GOVINT_NTFY", "ntfy"),
        lda_key=pick("GOVINT_LDA_KEY", "lda_key"),
        courtlistener_token=pick("GOVINT_COURTLISTENER_TOKEN", "courtlistener_token"),
    )


def config_path() -> Path:
    return _config_file()
