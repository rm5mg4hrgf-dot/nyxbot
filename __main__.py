"""Run Mafia Master with ``python -m mafia_bot``."""

from __future__ import annotations

import asyncio
import logging
import os

import discord

from bot import MafiaBot
from logging_config import configure_logging
from stories import validate_story_library


async def run() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is missing. Add it to Replit Secrets before starting the bot."
        )
    validate_story_library()
    bot = MafiaBot()
    try:
        async with bot:
            await bot.start(token)
    except discord.PrivilegedIntentsRequired:
        # Keep all slash-command gameplay online if the app owner has not
        # enabled Message Content Intent in Discord yet. A restart after the
        # portal setting is enabled will turn natural chat replies on.
        logging.getLogger("mafia_master").warning(
            "Discord has not enabled Message Content Intent. Restarting without chat-reading; "
            "game commands and AI turns remain available."
        )
        fallback = MafiaBot(message_content_intent=False)
        async with fallback:
            await fallback.start(token)


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger("mafia_master").info("Mafia Master stopped by operator")
    except RuntimeError as exc:
        logging.getLogger("mafia_master").error("Mafia Master could not start: %s", exc)
        raise SystemExit(1) from exc
    except Exception:
        logging.getLogger("mafia_master").exception(
            "Mafia Master stopped after an unexpected startup or runtime failure"
        )
        raise


if __name__ == "__main__":
    main()
