"""Built-in, non-graphic morning stories with no paid service requirement."""

from __future__ import annotations

import random
from typing import Protocol


class ChoiceSource(Protocol):
    def choice(self, values: list[str]) -> str: ...


STORY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "killed": (
        "Morning plot twist: {name} is eliminated overnight. The town's theories just got louder.",
        "The result is official: {name} is out of the game. The mystery is still wide open.",
        "One less voice at roll call: {name} is eliminated. Bring receipts, not rumors.",
        "Today’s headline is a rough one: {name} is out. Every vote from here has extra weight.",
        "The game just took a sharp turn: {name} is eliminated. Nobody gets to call it solved yet.",
        "No cheap jump scare, just the confirmed result: {name} is out overnight.",
        "The town's group chat has one less player: {name} is eliminated. The theories keep coming.",
        "Status update: {name} is out of the game. The vibe is tense, and the clues still matter.",
        "A plot twist nobody asked for: {name} is eliminated. Keep the theories bold and respectful.",
        "The morning roll call is short by one: {name} is out. Who has actual evidence?",
        "The result changed the whole board: {name} is eliminated. The next vote hits different.",
        "The town has a confirmed exit: {name} is out overnight. The mystery has not cleared.",
        "Today's game recap starts with one fact: {name} is eliminated. The rest is still theory.",
        "The town woke up to a real plot twist: {name} is out. No one has the full picture yet.",
        "One confirmed elimination, a dozen new theories: {name} is out of the game.",
    ),
    "saved": (
        "No one was eliminated overnight. Somehow, that makes the town's vibe even more suspicious.",
        "Zero exits this round. The mystery is still undefeated, and the group chat is not calm.",
        "Everyone is still in the game. Theories are multiplying faster than the clues.",
        "The night ended without an elimination. Take the rare calm moment; the questions remain.",
        "No elimination to report. Plot twist: now everyone has another day to explain their theory.",
        "The player count held steady overnight. The suspense absolutely did not.",
        "No one left the game overnight. That is the confirmed result; everything else is a theory.",
        "The town got a no-elimination morning. The vibe check says: keep asking questions.",
        "Everyone is still on the board. No one should mistake a quiet result for a solved mystery.",
        "The overnight result is a clean zero eliminations. The debate is very much not over.",
        "Nobody was eliminated overnight. The group chat has officially entered its detective era.",
        "The player list is unchanged this morning. The theories, however, are doing the most.",
        "No elimination overnight. Take a breath, compare notes, and keep the receipts coming.",
        "All players remain in the game. The suspense got a bonus round.",
        "A no-elimination morning: calm on the scoreboard, chaotic in the theories.",
    ),
    "quiet": (
        "The overnight result is zero eliminations. The silence is giving season-finale energy.",
        "No one is out this morning. The mystery stays open, and the theories stay loud.",
        "Quiet result, loud group chat: nobody was eliminated overnight.",
        "The board has the same number of players. The suspense is still doing the most.",
        "No elimination overnight. The town has one more day to trade vibes for evidence.",
        "Today's update: everyone remains in the game. Nobody has cracked the mystery yet.",
        "A zero-elimination morning. The calm is suspiciously good at raising more questions.",
        "No one left the game overnight. The theories have officially unlocked another round.",
        "The night gave the town no new elimination. The debate is still very much alive.",
        "Same player count, fresh theories. No elimination was confirmed overnight.",
        "The scoreboard is unchanged this morning. The plot, unfortunately, is not solved.",
        "No one was eliminated overnight. Everyone gets another turn to bring actual receipts.",
        "An uneventful result, an extremely eventful group chat: zero eliminations overnight.",
        "The town wakes with everyone still in the game and no shortage of questions.",
        "No elimination to report. The mystery remains open for another day.",
    ),
}


def build_morning_story(
    outcome: str,
    player_name: str | None = None,
    rng: ChoiceSource | None = None,
) -> str:
    """Select a story matching the actual night result.

    Saved stories intentionally omit the protected player's identity; public
    narration must not reveal the Mafia target or the Doctor's action.
    """
    source = rng or random.SystemRandom()
    if outcome not in STORY_TEMPLATES:
        raise ValueError(f"Unknown morning outcome: {outcome}")
    template = source.choice(list(STORY_TEMPLATES[outcome]))
    if outcome == "killed":
        if not player_name:
            raise ValueError("A player's name is required for a death story.")
        return template.format(name=player_name)
    return template


def validate_story_library() -> None:
    if sum(len(templates) for templates in STORY_TEMPLATES.values()) < 30:
        raise RuntimeError("The built-in story library must contain at least 30 templates.")
    if any(len(set(templates)) != len(templates) for templates in STORY_TEMPLATES.values()):
        raise RuntimeError("Morning story templates must be varied and unique.")
