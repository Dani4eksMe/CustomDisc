"""Entry point for running the Telegram support bot."""
from __future__ import annotations

import asyncio

from bot.main import main as bot_main


def run() -> None:
    """Run the Telegram support bot application."""
    asyncio.run(bot_main())


if __name__ == "__main__":
    run()
