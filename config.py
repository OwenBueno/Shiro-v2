"""
Configuration helpers and environment loading.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


@dataclass(frozen=True)
class Settings:
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    COMMAND_PREFIX: str = os.getenv("COMMAND_PREFIX", "!")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG").upper()

    # Lavalink
    LAVALINK_URL: str = os.getenv("LAVALINK_URL", "http://localhost:2333")
    LAVALINK_PASSWORD: str = os.getenv("LAVALINK_PASSWORD", "1995")


settings = Settings()
