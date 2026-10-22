
"""Shadowmind: evidence-based reasoning for Nyx Mafia AI.

This module uses public evidence only. It never reads hidden player
roles, private Mafia votes, private night actions, or investigation
results to calculate ordinary public suspicions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


MAX_SUSPICION = 10.0
MAX_REASONS = 8


@dataclass
class PlayerBelief:
    suspicion: float = 0.0
    reasons: list[str] = field(default_factory=list)
    evidence_count: int = 0

    def add(self, amount: float, reason: str) -> None:
        self.suspicion = max(
            0.0, min(MAX_SUSPICION, self.suspicion + amount)
        )
        self.evidence_count += 1

        if reason not in self.reasons:
            self.reasons.append(reason)

        self.reasons[:] = self.reasons[-MAX_REASONS:]


@dataclass
class ShadowmindMemory:
    """Separate public-evidence memory for one AI."""

    beliefs: dict[str, PlayerBelief] = field(default_factory=dict)
    processed_event_ids: set[str] = field(default_factory=set)

    def belief_for(self, player_id: str) -> PlayerBelief:
        return self.beliefs.setdefault(player_id, PlayerBelief())


# Each AI has a different memory for each game.
# This is in-memory state, not permanent storage across restarts.
_MEMORIES: dict[tuple[str, str], ShadowmindMemory] = {}


def memory_for(game: Any, observer_id: str) -> ShadowmindMemory:
    game_id = str(getattr(game, "game_id", "unknown-game"))
    key = (game_id, str(observer_id))
    return _MEMORIES.setdefault(key, ShadowmindMemory())


def forget_game(game_id: str) -> None:
    """Release all Shadowmind memories for a completed game."""

    game_id = str(game_id)

    for key in list(_MEMORIES):
        if key[0] == game_id:
            del _MEMORIES[key]


def _players(game: Any) -> dict[str, Any]:
    players = getattr(game, "players", {})
    return players if isinstance(players, dict) else {}


def _event_value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def update_beliefs(
    game: Any,
    observer_id: str,
) -> ShadowmindMemory:
    """Update an AI's beliefs using its game's public evidence ledger."""

    observer_id = str(observer_id)
    players = _players(game)
    memory = memory_for(game, observer_id)

    if observer_id not in players:
        return memory

    for event in getattr(game, "evidence_ledger", []):
        event_id = str(_event_value(event, "event_id", ""))

        if not event_id or event_id in memory.processed_event_ids:
            continue

        memory.processed_event_ids.add(event_id)

        kind = str(
            _event_value(event, "kind", "")
        ).casefold()

        actor_id = _event_value(event, "actor_id")
        target_id = _event_value(event, "target_id")

        actor_id = str(actor_id) if actor_id is not None else None
        target_id = str(target_id) if target_id is not None else None

        # Only evaluate recorded public behavior by other players.
        if actor_id is None or actor_id == observer_id:
            continue

        actor = players.get(actor_id)

        if actor is None:
            continue

        # A vote change is a weak clue, never proof of guilt.
        if "day_vote_changed" in kind:
            memory.belief_for(actor_id).add(
                0.35,
                "Changed a public daytime vote.",
            )

        # A recorded public accusation can make the accused worth
        # examining, but the accusation itself may be wrong.
        if target_id and target_id != actor_id:
            if any(
                marker in kind
                for marker in (
                    "accusation",
                    "public_claim",
                    "public_statement",
                )
            ):
                target = players.get(target_id)

                if target is not None:
                    memory.belief_for(target_id).add(
                        0.2,
                        "Was named in a public claim.",
                    )

    return memory


def suspicion_score(
    game: Any,
    observer_id: str,
    target_id: str,
) -> float:
    """Return a bounded suspicion score from public evidence."""

    if str(observer_id) == str(target_id):
        return 0.0

    memory = update_beliefs(game, observer_id)
    return memory.belief_for(str(target_id)).suspicion


def rank_suspects(
    game: Any,
    observer_id: str,
) -> list[str]:
    """Rank living alternatives using this AI's own beliefs."""

    candidates = [
        player
        for player in game.living_players()
        if player.user_id != observer_id
    ]

    memory = update_beliefs(game, observer_id)

    candidates.sort(
        key=lambda player: (
            memory.belief_for(player.user_id).suspicion,
            memory.belief_for(player.user_id).evidence_count,
            player.user_id,
        ),
        reverse=True,
    )

    return [player.user_id for player in candidates]


def choose_suspect(
    game: Any,
    observer: Any,
    candidates: list[Any],
    rng: Any,
) -> str | None:
    """Choose a candidate using public evidence and personality.

    The caller must apply the game's role-specific rules first.
    """

    if not candidates:
        return None

    memory = update_beliefs(game, observer.user_id)
    profile = getattr(observer, "personality", None)

    scored = [
        (
            memory.belief_for(player.user_id).suspicion,
            memory.belief_for(player.user_id).evidence_count,
            player,
        )
        for player in candidates
    ]

    highest = max(score for score, _, _ in scored)

    if highest <= 0:
        # No meaningful evidence: preserve variety rather than
        # pretending the AI has discovered something.
        return rng.choice(candidates).user_id

    leaders = [
        player
        for score, _, player in scored
        if score == highest
    ]

    # Some personalities are deliberately more exploratory.
    if profile in {"random", "easygoing"} and rng.random() < 0.25:
        return rng.choice(candidates).user_id

    return rng.choice(leaders).user_id


def explain_suspicion(
    game: Any,
    observer_id: str,
    target_id: str,
) -> str:
    """Produce a short explanation based on recorded evidence."""

    memory = update_beliefs(game, observer_id)
    belief = memory.belief_for(str(target_id))

    if not belief.reasons:
        return "I don't have enough recorded public evidence yet."

    reasons = "; ".join(belief.reasons[-3:])
    return (
        f"My suspicion score is {belief.suspicion:.1f}/10. "
        f"Public clues: {reasons}"
    )
