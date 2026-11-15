from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Project root = the folder containing config/ and src/. Walk up from this file.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


def load_yaml(name: str, base: Path = CONFIG_DIR) -> dict:
    path = base / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_secrets() -> dict:
    """Load secrets from config/secrets.yaml (gitignored) or environment.

    Never logged, never committed. Environment variables win over the file.
    """
    secrets: dict = load_yaml("secrets.yaml")
    # Flatten one level:  groq: {api_key: X} -> GROQ_API_KEY
    flat: dict = {}
    for key, value in secrets.items():
        if isinstance(value, dict):
            for sub, subv in value.items():
                flat[f"{key}_{sub}"] = subv
        else:
            flat[key] = value
    for key in list(flat):
        env = os.environ.get(key.upper())
        if env:
            flat[key] = env
    return flat


def get_secret(name: str, secrets: dict | None = None) -> str | None:
    """Fetch a secret by provider name (case-insensitive) or env var."""
    data = secrets if secrets is not None else load_secrets()
    value = data.get(name) or data.get(name.lower())
    if value:
        return str(value)
    return os.environ.get(name.upper())


@dataclass
class Settings:
    """Runtime settings resolved from config files and environment."""

    project_root: Path = PROJECT_ROOT
    config_dir: Path = CONFIG_DIR
    data_dir: Path = DATA_DIR
    engines: dict = field(default_factory=lambda: load_yaml("engines.yaml"))
    providers: dict = field(default_factory=lambda: load_yaml("providers.yaml"))
    weights: dict = field(default_factory=lambda: load_yaml("weights.yaml"))
    personas: dict = field(default_factory=lambda: load_yaml("personas.yaml"))
    languages: dict = field(default_factory=lambda: load_yaml("languages.yaml"))
    channels: dict = field(default_factory=lambda: load_yaml("channels.yaml"))
    schedules: dict = field(default_factory=lambda: load_yaml("schedules.yaml"))
    products: dict = field(default_factory=lambda: load_yaml("products.yaml"))
    network: dict = field(default_factory=lambda: load_yaml("network.yaml"))
    db_url: str = ""

    def __post_init__(self) -> None:
        env_url = os.environ.get("BUYERINTEL_DB_URL")
        self.db_url = env_url or f"sqlite:///{self.data_dir / 'buyerintel.db'}"
        self.data_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()
