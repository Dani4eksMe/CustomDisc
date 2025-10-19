"""Configuration helpers for the support bot."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


@dataclass(frozen=True)
class BotConfig:
    token: str
    owner_username: str = "dn4ikx"
    default_ticket_wage: int = 10

    @staticmethod
    def from_env() -> "BotConfig":
        token = os.getenv("BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "BOT_TOKEN is not configured. Set it in the environment or .env file."
            )
        owner_username = os.getenv("BOT_OWNER", "dn4ikx")
        default_wage_str = os.getenv("DEFAULT_TICKET_WAGE", "10")
        try:
            default_wage = int(default_wage_str)
        except ValueError as exc:
            raise RuntimeError(
                "DEFAULT_TICKET_WAGE must be an integer"
            ) from exc
        return BotConfig(token=token, owner_username=owner_username, default_ticket_wage=default_wage)


CONFIG = BotConfig.from_env()
