"""Optional OpenAI atmosphere lines for verified Mafia morning results."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import aiohttp

logger = logging.getLogger("mafia_master.ai_storyteller")

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-5-nano"
REQUEST_TIMEOUT_SECONDS = 8
MAX_STORY_CHARACTERS = 240

_UNSAFE_OR_UNVERIFIED = re.compile(
    r"\b(?:"
    r"mafia|doctor|detective|joker|sheriff|medic|healer|bodyguard|investigator|"
    r"townspeople|townsperson|villager|role|faction|"
    r"investigat\w*|protect\w*|save\w*|attack\w*|target\w*|vote\w*|"
    r"kill\w*|dead|death|die|died|dying|eliminat\w*|victim|"
    r"perish\w*|deceas\w*|murder\w*|slain|passed away|pass away|"
    r"gone|missing|disappear\w*|vanish\w*|never return\w*|last breath|"
    r"someone|somebody|anyone|nobody|no one|person|people|player|stranger|figure|witness|"
    r"blood|corpse|remains|body|stab\w*|shoot\w*|wound\w*|"
    r"suicid\w*|torture\w*|decapitat\w*|gore|rape|porn|nude|sexual|"
    r"fuck\w*|shit\w*|bitch\w*|asshole|cunt|bastard"
    r")\b",
    re.IGNORECASE,
)


def build_story_request(day_number: int, player_eliminated: bool) -> dict[str, Any]:
    """Build a request from public, already-resolved facts only."""
    public_result = (
        "One player was eliminated overnight."
        if player_eliminated
        else "No player was eliminated overnight."
    )
    return {
        "model": OPENAI_MODEL,
        "max_completion_tokens": 96,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Write one original, suspenseful, teen-friendly, non-graphic "
                    "atmosphere line for a Discord game's morning announcement. "
                    "Use a contemporary teen group-chat voice with light, natural "
                    "phrases like 'plot twist' or 'vibe'; do not force slang. "
                    "The verified result is displayed separately and is authoritative. "
                    "Write mood only: do not mention people, names, roles, teams, "
                    "actions, deaths, votes, investigations, protection, or causes. "
                    "Do not claim that any event happened. Use one short sentence, "
                    "with no profanity, threats, or graphic content."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Day {day_number}. Verified public result: {public_result} "
                    "Return only the atmosphere line."
                ),
            },
        ],
    }


def _normalized_words(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip().casefold()


def validate_ai_story(text: str, player_names: tuple[str, ...] = ()) -> str | None:
    """Reject AI text that could claim a result, reveal a role, or identify a player."""
    candidate = " ".join(text.split()).strip(" \t\r\n\"'`")
    if not candidate or len(candidate) > MAX_STORY_CHARACTERS:
        return None
    if "\n" in text or _UNSAFE_OR_UNVERIFIED.search(candidate):
        return None
    if re.search(r"<@|@everyone|@here|https?://", candidate, re.IGNORECASE):
        return None
    if len(re.findall(r"[.!?](?:\s|$)", candidate)) > 1:
        return None

    normalized_candidate = f" {_normalized_words(candidate)} "
    for name in player_names:
        normalized_name = _normalized_words(name)
        if normalized_name and f" {normalized_name} " in normalized_candidate:
            return None
    return candidate


def _response_text(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return " ".join(parts) or None
    return None


async def generate_morning_story(
    day_number: int,
    player_eliminated: bool,
    player_names: tuple[str, ...] = (),
) -> str | None:
    """Return a validated atmosphere line, or None so the caller can use its fallback."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT_SECONDS,
        connect=3,
    )
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                OPENAI_CHAT_COMPLETIONS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=build_story_request(day_number, player_eliminated),
            ) as response:
                if response.status != 200:
                    logger.warning(
                        "AI storyteller returned HTTP %s; using a built-in story.",
                        response.status,
                    )
                    return None
                payload = await response.json()
        raw_story = _response_text(payload)
        if raw_story is None:
            logger.warning("AI storyteller returned no text; using a built-in story.")
            return None
        story = validate_ai_story(raw_story, player_names)
        if story is None:
            logger.warning("AI storyteller text failed safety checks; using a built-in story.")
        return story
    except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, TypeError) as exc:
        logger.warning(
            "AI storyteller request failed (%s); using a built-in story.",
            type(exc).__name__,
        )
        return None
    except Exception as exc:
        # Keep story generation non-critical, without logging request headers or secrets.
        logger.warning(
            "AI storyteller encountered an unexpected error (%s); using a built-in story.",
            type(exc).__name__,
        )
        return None
