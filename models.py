"""Serializable game models and validated rules for Mafia Master."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
import time


class Phase(StrEnum):
    LOBBY = "lobby"
    DAY_DISCUSSION = "day_discussion"
    DAY_VOTING = "day_voting"
    NIGHT_MAFIA = "night_mafia"
    NIGHT_ACTIONS = "night_actions"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class RoleKey(StrEnum):
    MAFIA = "mafia"
    DOCTOR = "doctor"
    DETECTIVE = "detective"
    TOWNSPERSON = "townspeople"
    JOKER = "joker"


@dataclass(frozen=True)
class RoleDefinition:
    key: str
    name: str
    team: str
    description: str
    night_action: str | None = None


class RoleCatalog:
    """Registry keeps role metadata and role assignment open to extensions."""

    def __init__(self) -> None:
        self._roles: dict[str, RoleDefinition] = {}

    def register(self, definition: RoleDefinition) -> None:
        if not definition.key or definition.key in self._roles:
            raise ValueError(f"Role key is empty or already registered: {definition.key!r}")
        if definition.team not in {"mafia", "town", "neutral"}:
            raise ValueError("Role team must be mafia, town, or neutral.")
        self._roles[definition.key] = definition

    def get(self, key: str) -> RoleDefinition:
        try:
            return self._roles[key]
        except KeyError as exc:
            raise ValueError(f"Unknown role: {key}") from exc

    def keys(self) -> set[str]:
        return set(self._roles)


DEFAULT_ROLE_CATALOG = RoleCatalog()
DEFAULT_ROLE_CATALOG.register(
    RoleDefinition(
        RoleKey.MAFIA,
        "Mafia",
        "mafia",
        "Choose one living non-Mafia player as the night's target. Your Mafia teammates are shown privately.",
        "mafia_vote",
    )
)
DEFAULT_ROLE_CATALOG.register(
    RoleDefinition(
        RoleKey.DOCTOR,
        "Doctor",
        "town",
        "After the Mafia chooses a target, protect one living player. Protection can prevent that night's attack.",
        "protect",
    )
)
DEFAULT_ROLE_CATALOG.register(
    RoleDefinition(
        RoleKey.DETECTIVE,
        "Detective",
        "town",
        "Investigate one living player each night and privately learn whether their alignment matches the configured rules.",
        "investigate",
    )
)
DEFAULT_ROLE_CATALOG.register(
    RoleDefinition(
        RoleKey.TOWNSPERSON,
        "Townspeople",
        "town",
        "You have no night action. Use discussion and daytime votes to find the Mafia.",
    )
)
DEFAULT_ROLE_CATALOG.register(
    RoleDefinition(
        RoleKey.JOKER,
        "Joker",
        "neutral",
        "You win immediately if eliminated by a daytime vote. A night-time death does not trigger your special victory.",
    )
)


@dataclass
class GameRules:
    preset: str = "classic"
    min_players: int = 5
    max_players: int = 12
    # Counts for custom games; townspeople fill any remaining seats.
    custom_roles: dict[str, int] = field(
        default_factory=lambda: {
            RoleKey.MAFIA: 1,
            RoleKey.DOCTOR: 1,
            RoleKey.DETECTIVE: 1,
            RoleKey.JOKER: 0,
        }
    )
    doctor_self_protection: bool = True
    detective_sees_joker_as_mafia: bool = False
    reveal_roles: bool = True
    discussion_seconds: int = 90
    voting_seconds: int = 60
    night_seconds: int = 60
    vote_tie_rule: str = "no_elimination"
    mafia_tie_rule: str = "no_kill"
    anonymous_votes: bool = True
    spectators_can_see: bool = True
    ai_storytelling_enabled: bool = True

    def validate(self, catalog: RoleCatalog = DEFAULT_ROLE_CATALOG) -> None:
        if self.preset not in {"beginner", "classic", "custom"}:
            raise ValueError("Preset must be Beginner, Classic, or Custom.")
        if not 1 <= self.min_players <= 20:
            raise ValueError("Minimum human players must be between 1 and 20.")
        if not 4 <= self.max_players <= 20:
            raise ValueError("Maximum total players must be between 4 and 20.")
        if self.min_players > self.max_players:
            raise ValueError("Minimum human players cannot exceed the maximum total players.")
        if self.vote_tie_rule not in {"no_elimination", "random"}:
            raise ValueError("Daytime tie rule must be no_elimination or random.")
        if self.mafia_tie_rule not in {"no_kill", "random"}:
            raise ValueError("Mafia tie rule must be no_kill or random.")
        if not 15 <= self.discussion_seconds <= 3600:
            raise ValueError("Discussion time must be between 15 and 3600 seconds.")
        if not 15 <= self.voting_seconds <= 3600:
            raise ValueError("Voting time must be between 15 and 3600 seconds.")
        if not 15 <= self.night_seconds <= 1800:
            raise ValueError("Night action time must be between 15 and 1800 seconds.")
        if self.custom_roles.get(RoleKey.MAFIA, 0) < 1:
            raise ValueError("Custom games must include at least one Mafia member.")
        if any(key not in catalog.keys() for key in self.custom_roles):
            raise ValueError("Custom role counts contain an unregistered role.")
        if RoleKey.TOWNSPERSON in self.custom_roles:
            raise ValueError("Townspeople are automatically assigned to fill remaining seats.")
        if any(not isinstance(count, int) or count < 0 for count in self.custom_roles.values()):
            raise ValueError("Role counts must be non-negative whole numbers.")
        if sum(self.custom_roles.values()) > self.max_players:
            raise ValueError("Configured custom roles exceed the maximum player count.")
        if self.custom_roles.get(RoleKey.MAFIA, 0) >= self.max_players:
            raise ValueError("Custom roles must leave at least one non-Mafia player.")

    def role_counts_for(
        self,
        player_count: int,
        catalog: RoleCatalog = DEFAULT_ROLE_CATALOG,
    ) -> dict[str, int]:
        self.validate(catalog)
        if not 4 <= player_count <= self.max_players:
            raise ValueError(
                f"This game needs between 4 and {self.max_players} total players."
            )

        if self.preset == "beginner":
            counts = {
                RoleKey.MAFIA: 1 if player_count < 9 else 2,
                RoleKey.DOCTOR: 0,
                RoleKey.DETECTIVE: 0,
                RoleKey.JOKER: 0,
            }
        elif self.preset == "classic":
            counts = {
                RoleKey.MAFIA: 1 if player_count <= 6 else 2 if player_count <= 10 else 3,
                RoleKey.DOCTOR: 1,
                RoleKey.DETECTIVE: 1 if player_count >= 7 else 0,
                RoleKey.JOKER: 1 if player_count >= 9 else 0,
            }
        else:
            counts = dict(self.custom_roles)

        special_count = sum(counts.values())
        if counts.get(RoleKey.MAFIA, 0) < 1:
            raise ValueError("Every role setup needs at least one Mafia member.")
        if counts[RoleKey.MAFIA] >= player_count:
            raise ValueError("Role setup must leave at least one non-Mafia player.")
        if special_count > player_count:
            raise ValueError("Configured roles exceed the number of joined players.")
        counts[RoleKey.TOWNSPERSON] = player_count - special_count
        return {str(key): int(value) for key, value in counts.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "min_players": self.min_players,
            "max_players": self.max_players,
            "custom_roles": dict(self.custom_roles),
            "doctor_self_protection": self.doctor_self_protection,
            "detective_sees_joker_as_mafia": self.detective_sees_joker_as_mafia,
            "reveal_roles": self.reveal_roles,
            "discussion_seconds": self.discussion_seconds,
            "voting_seconds": self.voting_seconds,
            "night_seconds": self.night_seconds,
            "vote_tie_rule": self.vote_tie_rule,
            "mafia_tie_rule": self.mafia_tie_rule,
            "anonymous_votes": self.anonymous_votes,
            "spectators_can_see": self.spectators_can_see,
            "ai_storytelling_enabled": self.ai_storytelling_enabled,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> GameRules:
        if not raw:
            return cls()
        allowed = cls.__dataclass_fields__.keys()
        values = {key: value for key, value in raw.items() if key in allowed}
        return cls(**values)

    def summary(self) -> str:
        return (
            f"**Preset:** {self.preset.title()}\n"
            f"**Human minimum / total maximum:** {self.min_players}–{self.max_players}\n"
            f"**Timers:** discussion {self.discussion_seconds}s · "
            f"vote {self.voting_seconds}s · night {self.night_seconds}s\n"
            f"**Doctor self-protection:** {'On' if self.doctor_self_protection else 'Off'}\n"
            f"**Role reveal after vote:** {'On' if self.reveal_roles else 'Off'}\n"
            f"**Day vote display:** {'Anonymous' if self.anonymous_votes else 'Public'}\n"
            f"**Spectator access:** {'Public channel' if self.spectators_can_see else 'Players-only thread'}\n"
            f"**AI morning stories:** {'On' if self.ai_storytelling_enabled else 'Off'}\n"
            f"**Day tie:** {self.vote_tie_rule.replace('_', ' ')} · "
            f"**Mafia tie:** {self.mafia_tie_rule.replace('_', ' ')}"
        )


@dataclass
class Player:
    user_id: str
    name: str
    role: str | None = None
    alive: bool = True
    dm_preflight_ok: bool = False
    is_ai: bool = False
    personality: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role,
            "alive": self.alive,
            "dm_preflight_ok": self.dm_preflight_ok,
            "is_ai": self.is_ai,
            "personality": self.personality,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Player:
        return cls(**raw)


@dataclass
class HistoryEvent:
    timestamp: int
    day: int
    night: int
    kind: str
    public_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "day": self.day,
            "night": self.night,
            "kind": self.kind,
            "public_text": self.public_text,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HistoryEvent:
        return cls(**raw)


@dataclass
class EvidenceEvent:
    event_id: str
    timestamp: int
    day: int
    night: int
    kind: str
    summary: str
    actor_id: str | None = None
    target_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "day": self.day,
            "night": self.night,
            "kind": self.kind,
            "summary": self.summary,
            "actor_id": self.actor_id,
            "target_id": self.target_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvidenceEvent:
        return cls(**raw)


@dataclass
class MafiaGame:
    game_id: str
    guild_id: str
    parent_channel_id: str
    host_id: str
    host_name: str
    rules: GameRules
    players: dict[str, Player] = field(default_factory=dict)
    required_humans: int | None = None
    exact_human_count: bool = False
    ai_fill_to_max: bool = False
    phase: Phase = Phase.LOBBY
    created_at: int = field(default_factory=lambda: int(time.time()))
    started_at: int | None = None
    finished_at: int | None = None
    deadline: float | None = None
    round_number: int = 0
    day_number: int = 0
    winner: str | None = None
    startup_complete: bool = False
    lobby_message_id: str | None = None
    play_thread_id: str | None = None
    mafia_thread_id: str | None = None
    vote_message_id: str | None = None
    action_message_ids: dict[str, str] = field(default_factory=dict)
    votes: dict[str, str] = field(default_factory=dict)
    mafia_votes: dict[str, str] = field(default_factory=dict)
    mafia_target: str | None = None
    doctor_target: str | None = None
    detective_target: str | None = None
    doctor_targets: dict[str, str] = field(default_factory=dict)
    detective_targets: dict[str, str] = field(default_factory=dict)
    investigation_results: dict[str, bool] = field(default_factory=dict)
    private_action_history: list[dict[str, Any]] = field(default_factory=list)
    history: list[HistoryEvent] = field(default_factory=list)
    evidence_ledger: list[EvidenceEvent] = field(default_factory=list)
    last_night_outcome: str | None = None
    day_vote_count: int = 0
    ai_discussion_day: int = 0

    @property
    def play_channel_id(self) -> str:
        return self.play_thread_id or self.parent_channel_id

    def living_players(self) -> list[Player]:
        return [player for player in self.players.values() if player.alive]

    def human_players(self) -> list[Player]:
        return [player for player in self.players.values() if not player.is_ai]

    def ai_players(self) -> list[Player]:
        return [player for player in self.players.values() if player.is_ai]

    def players_with_role(self, role: str, *, alive_only: bool = True) -> list[Player]:
        return [
            player
            for player in self.players.values()
            if player.role == role and (player.alive or not alive_only)
        ]

    def record_public_evidence(
        self,
        kind: str,
        public_text: str,
        *,
        actor_id: str | None = None,
        target_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceEvent:
        """Store only public events in the evidence ledger; never private role or action details."""
        event = EvidenceEvent(
            event_id=f"{self.game_id}:{len(self.evidence_ledger) + 1}",
            timestamp=int(time.time()),
            day=self.day_number,
            night=self.round_number,
            kind=kind,
            summary=public_text,
            actor_id=actor_id,
            target_id=target_id,
            metadata=metadata or {},
        )
        self.evidence_ledger.append(event)
        return event

    def add_event(self, kind: str, public_text: str) -> None:
        self.history.append(
            HistoryEvent(
                timestamp=int(time.time()),
                day=self.day_number,
                night=self.round_number,
                kind=kind,
                public_text=public_text,
            )
        )
        self.record_public_evidence(kind, public_text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "guild_id": self.guild_id,
            "parent_channel_id": self.parent_channel_id,
            "host_id": self.host_id,
            "host_name": self.host_name,
            "rules": self.rules.to_dict(),
            "players": {key: player.to_dict() for key, player in self.players.items()},
            "required_humans": self.required_humans,
            "exact_human_count": self.exact_human_count,
            "ai_fill_to_max": self.ai_fill_to_max,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "deadline": self.deadline,
            "round_number": self.round_number,
            "day_number": self.day_number,
            "winner": self.winner,
            "startup_complete": self.startup_complete,
            "lobby_message_id": self.lobby_message_id,
            "play_thread_id": self.play_thread_id,
            "mafia_thread_id": self.mafia_thread_id,
            "vote_message_id": self.vote_message_id,
            "action_message_ids": dict(self.action_message_ids),
            "votes": dict(self.votes),
            "mafia_votes": dict(self.mafia_votes),
            "mafia_target": self.mafia_target,
            "doctor_target": self.doctor_target,
            "detective_target": self.detective_target,
            "doctor_targets": dict(self.doctor_targets),
            "detective_targets": dict(self.detective_targets),
            "investigation_results": dict(self.investigation_results),
            "private_action_history": list(self.private_action_history),
            "history": [event.to_dict() for event in self.history],
            "evidence_ledger": [event.to_dict() for event in self.evidence_ledger],
            "last_night_outcome": self.last_night_outcome,
            "day_vote_count": self.day_vote_count,
            "ai_discussion_day": self.ai_discussion_day,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MafiaGame:
        values = dict(raw)
        values["rules"] = GameRules.from_dict(values.get("rules"))
        values["players"] = {
            key: Player.from_dict(player) for key, player in values.get("players", {}).items()
        }
        values["phase"] = Phase(values.get("phase", Phase.LOBBY.value))
        values["history"] = [
            HistoryEvent.from_dict(event) for event in values.get("history", [])
        ]
        values["evidence_ledger"] = [
            EvidenceEvent.from_dict(event) for event in values.get("evidence_ledger", [])
        ]
        values.setdefault("doctor_targets", {})
        values.setdefault("detective_targets", {})
        # Older saved night states have scalar targets and private action
        # records; rebuild the per-actor maps so they resume without duplicates.
        current_night_actions = [
            action for action in values.get("private_action_history", [])
            if action.get("night") == values.get("round_number")
        ]
        if not values["doctor_targets"]:
            values["doctor_targets"] = {
                action["actor_id"]: action["target_id"]
                for action in current_night_actions
                if action.get("action") == "protect" and action.get("target_id")
            }
        if not values["detective_targets"]:
            values["detective_targets"] = {
                action["actor_id"]: action["target_id"]
                for action in current_night_actions
                if action.get("action") == "investigate" and action.get("target_id")
            }
        return cls(**values)


def phase_is_active(phase: Phase) -> bool:
    return phase not in {Phase.FINISHED, Phase.CANCELLED}
