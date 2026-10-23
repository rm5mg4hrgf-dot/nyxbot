"""Pure Mafia rules and state transitions.

Discord callbacks call these functions while holding the per-game lock in
``GameManager``. The engine has no Discord imports, so game rules stay easy to
test and roles can be extended without changing the interface layer.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
import time
from typing import Any, Protocol

from models import (
    DEFAULT_ROLE_CATALOG,
    GameRules,
    MafiaGame,
    Phase,
    Player,
    RoleCatalog,
    RoleKey,
)


class ChoiceSource(Protocol):
    def shuffle(self, values: list[Any]) -> None: ...

    def choice(self, values: list[Any]) -> Any: ...


class GameRuleError(ValueError):
    """An action is invalid for the current game state."""


class DuplicateActionError(GameRuleError):
    """The player already submitted this phase's one-time action."""


@dataclass(frozen=True)
class NightResolution:
    outcome: str  # "killed", "saved", or "quiet"
    victim_id: str | None
    investigation_results: dict[str, bool]
    winner: str | None


@dataclass(frozen=True)
class DayResolution:
    eliminated_id: str | None
    tied_ids: tuple[str, ...]
    winner: str | None
    revealed_role: str | None
    vote_count: int


def _player(game: MafiaGame, user_id: str) -> Player:
    player = game.players.get(str(user_id))
    if player is None:
        raise GameRuleError("You are not part of this game.")
    return player


def _living_target(game: MafiaGame, target_id: str) -> Player:
    target = _player(game, target_id)
    if not target.alive:
        raise GameRuleError("That player has already been eliminated.")
    return target


def _record_private_action(game: MafiaGame, actor_id: str, action: str, target_id: str | None) -> None:
    game.private_action_history.append(
        {
            "night": game.round_number,
            "actor_id": str(actor_id),
            "action": action,
            "target_id": str(target_id) if target_id is not None else None,
        }
    )


def _faction_winner(game: MafiaGame, catalog: RoleCatalog) -> str | None:
    living = game.living_players()
    mafia_count = sum(
        1
        for player in living
        if player.role is not None and catalog.get(player.role).team == "mafia"
    )
    opposing_count = len(living) - mafia_count

    if mafia_count == 0:
        return "Town"
    if mafia_count >= opposing_count:
        return "Mafia"
    return None


def _finish(game: MafiaGame, winner: str) -> None:
    game.winner = winner
    game.phase = Phase.FINISHED
    game.deadline = None
    game.finished_at = int(time.time())
    game.add_event("game_end", f"{winner} wins the game.")


class GameEngine:
    """Deterministic game rules; provide a seeded RNG in tests."""

    def __init__(
        self,
        catalog: RoleCatalog = DEFAULT_ROLE_CATALOG,
        rng: ChoiceSource | None = None,
    ) -> None:
        self.catalog = catalog
        self.rng = rng or random.SystemRandom()

    def start_game(self, game: MafiaGame) -> list[Player]:
        if game.phase != Phase.LOBBY:
            raise GameRuleError("This lobby has already started.")
        game.rules.validate(self.catalog)
        if game.required_humans is not None:
            human_count = sum(not player.is_ai for player in game.players.values())
            count_ok = (
                human_count == game.required_humans
                if game.exact_human_count
                else human_count >= game.required_humans
            )
            if not count_ok:
                comparison = "exactly" if game.exact_human_count else "at least"
                raise GameRuleError(
                    f"This lobby needs {comparison} {game.required_humans} human players; "
                    f"{human_count} have joined."
                )
            if game.ai_fill_to_max and len(game.players) != game.rules.max_players:
                raise GameRuleError(
                    f"This game needs {game.rules.max_players} total players, including AI seats."
                )
        counts = game.rules.role_counts_for(len(game.players), self.catalog)
        roles: list[str] = []
        for role_key, count in counts.items():
            self.catalog.get(role_key)
            roles.extend([role_key] * count)
        if len(roles) != len(game.players):
            raise GameRuleError("The role list does not match the number of joined players.")

        self.rng.shuffle(roles)
        for player, role in zip(game.players.values(), roles, strict=True):
            player.role = role
            player.alive = True

        now = time.time()
        game.phase = Phase.DAY_DISCUSSION
        game.started_at = int(now)
        game.startup_complete = False
        game.day_number = 1
        game.round_number = 0
        game.deadline = now + game.rules.discussion_seconds
        game.votes.clear()
        game.mafia_votes.clear()
        game.doctor_targets.clear()
        game.detective_targets.clear()
        game.investigation_results.clear()
        game.add_event("game_start", "The game began. Day 1 discussion is open.")
        return list(game.players.values())

    def reset_to_lobby(self, game: MafiaGame) -> None:
        """Undo role assignment if Discord setup fails before roles are delivered."""
        if game.phase not in {Phase.DAY_DISCUSSION, Phase.LOBBY}:
            raise GameRuleError("A game cannot be reset after the first phase.")
        for player in game.players.values():
            player.role = None
            player.alive = True
        for player_id in [
            player_id for player_id, player in game.players.items() if player.is_ai
        ]:
            del game.players[player_id]
        game.phase = Phase.LOBBY
        game.started_at = None
        game.startup_complete = False
        game.deadline = None
        game.day_number = 0
        game.round_number = 0
        game.ai_discussion_day = 0
        game.history.clear()

    def join_lobby(self, game: MafiaGame, player: Player) -> None:
        if game.phase != Phase.LOBBY:
            raise GameRuleError("Players can only join before the game starts.")
        if player.user_id in game.players:
            raise GameRuleError("You have already joined.")
        if game.required_humans is not None and game.exact_human_count:
            human_count = sum(not current.is_ai for current in game.players.values())
            if human_count >= game.required_humans:
                raise GameRuleError("This lobby already has all of its human seats filled.")
        if len(game.players) >= game.rules.max_players:
            raise GameRuleError("This lobby is full.")
        game.players[player.user_id] = player

    def leave_lobby(self, game: MafiaGame, user_id: str) -> None:
        if game.phase != Phase.LOBBY:
            raise GameRuleError("Players can only leave before the game starts.")
        if str(user_id) == game.host_id:
            raise GameRuleError("The host cannot leave. Cancel the lobby or transfer hosting first.")
        if str(user_id) not in game.players:
            raise GameRuleError("You are not in this lobby.")
        del game.players[str(user_id)]

    def add_player(self, game: MafiaGame, player: Player) -> None:
        if game.phase != Phase.LOBBY:
            raise GameRuleError("Players can only be added before the game starts.")
        if player.user_id in game.players:
            raise GameRuleError("That player is already in the lobby.")
        if game.required_humans is not None and game.exact_human_count:
            human_count = sum(not current.is_ai for current in game.players.values())
            if human_count >= game.required_humans:
                raise GameRuleError("This lobby already has all of its human seats filled.")
        if len(game.players) >= game.rules.max_players:
            raise GameRuleError("This lobby is full.")
        game.players[player.user_id] = player

    def remove_player(self, game: MafiaGame, user_id: str) -> None:
        if game.phase != Phase.LOBBY:
            raise GameRuleError("Players can only be removed before the game starts.")
        if str(user_id) == game.host_id:
            raise GameRuleError("The host cannot be removed from their own lobby.")
        if str(user_id) not in game.players:
            raise GameRuleError("That player is not in this lobby.")
        del game.players[str(user_id)]

    def cancel_game(self, game: MafiaGame) -> None:
        if game.phase in {Phase.FINISHED, Phase.CANCELLED}:
            raise GameRuleError("This game has already ended.")
        game.phase = Phase.CANCELLED
        game.deadline = None

    def forfeit_player(self, game: MafiaGame, user_id: str) -> str | None:
        """Mark a server-leaver out without granting the Joker a daytime win."""
        player = game.players.get(str(user_id))
        if player is None or not player.alive:
            return None
        if game.phase in {Phase.LOBBY, Phase.FINISHED, Phase.CANCELLED}:
            raise GameRuleError("A player can only forfeit after the game starts.")
        player.alive = False
        game.add_event(
            "player_forfeit",
            f"{player.name} left the server and was removed from the game.",
        )
        winner = _faction_winner(game, self.catalog)
        if winner:
            _finish(game, winner)
        return winner

    def begin_voting(self, game: MafiaGame) -> None:
        if game.phase != Phase.DAY_DISCUSSION:
            raise GameRuleError("Daytime discussion is not active.")
        game.phase = Phase.DAY_VOTING
        game.votes.clear()
        game.deadline = time.time() + game.rules.voting_seconds

    def submit_day_vote(self, game: MafiaGame, voter_id: str, target_id: str) -> None:
        if game.phase != Phase.DAY_VOTING:
            raise GameRuleError("Daytime voting is not active.")
        voter = _player(game, voter_id)
        if not voter.alive:
            raise GameRuleError("Eliminated players cannot vote.")
        target = _living_target(game, target_id)
        if target.user_id != str(target_id):
            raise GameRuleError("That vote target is invalid.")
        # Day votes intentionally replace a player's prior vote until resolution.
        previous_target_id = game.votes.get(voter.user_id)
        game.votes[voter.user_id] = target.user_id


        # Record individual votes only when votes are public.
        # Anonymous ballots must not reveal who voted for whom.
        if not game.rules.anonymous_votes:
            if previous_target_id != target.user_id:
                if previous_target_id is None:
                    kind = "day_vote_cast"
                    summary = (
                        f"Day {game.day_number}: "
                        f"{voter.name} voted for {target.name}."
                    )
                else:
                    previous_target = game.players.get(previous_target_id)
                    previous_name = (
                        previous_target.name
                        if previous_target is not None
                        else "another player"
                    )
                    kind = "day_vote_changed"
                    summary = (
                        f"Day {game.day_number}: {voter.name} "
                        f"changed their vote from {previous_name} "
                        f"to {target.name}."
                    )

                game.record_public_evidence(
                    kind=kind,
                    public_text=summary,
                    actor_id=voter.user_id,
                    target_id=target.user_id,
                    metadata={
                        "previous_target_id": previous_target_id,
                    },
                )
                
        game.record_public_evidence(
            kind=kind,
            public_text=summary,
            actor_id=voter.user_id,
            target_id=target.user_id,
            metadata={
                "previous_target_id": previous_target_id,
            },
        )

    def resolve_day_vote(self, game: MafiaGame) -> DayResolution:
        if game.phase != Phase.DAY_VOTING:
            raise GameRuleError("Daytime voting is not active.")
        counts: dict[str, int] = {}
        for voter_id, target_id in game.votes.items():
            voter = game.players.get(voter_id)
            target = game.players.get(target_id)
            if voter and voter.alive and target and target.alive:
                counts[target_id] = counts.get(target_id, 0) + 1

        vote_count = sum(counts.values())
        if not counts:
            game.add_event("day_vote", f"Day {game.day_number}: no votes were cast; no one was eliminated.")
            return self._finish_vote_without_elimination(game, vote_count)

        high_score = max(counts.values())
        tied = tuple(sorted(target for target, count in counts.items() if count == high_score))
        if len(tied) > 1 and game.rules.vote_tie_rule == "no_elimination":
            game.add_event(
                "day_vote",
                f"Day {game.day_number}: the vote was tied; no one was eliminated.",
            )
            return self._finish_vote_without_elimination(game, vote_count, tied)

        target_id = self.rng.choice(list(tied))
        eliminated = game.players[target_id]
        eliminated.alive = False
        game.day_vote_count += 1
        role = eliminated.role
        role_name = self.catalog.get(role).name if role else "unknown"
        public_result = f"Day {game.day_number}: {eliminated.name} was eliminated."
        if game.rules.reveal_roles:
            public_result += f" Their role was {role_name}."
        game.add_event("day_elimination", public_result)

        # The Joker's special victory is checked before faction parity.
        if role == RoleKey.JOKER:
            _finish(game, "Joker")
        else:
            winner = _faction_winner(game, self.catalog)
            if winner:
                _finish(game, winner)
            else:
                self.begin_night(game)
        return DayResolution(
            eliminated_id=target_id,
            tied_ids=tied if len(tied) > 1 else (),
            winner=game.winner,
            revealed_role=role if game.rules.reveal_roles else None,
            vote_count=vote_count,
        )

    def _finish_vote_without_elimination(
        self,
        game: MafiaGame,
        vote_count: int,
        tied: tuple[str, ...] = (),
    ) -> DayResolution:
        winner = _faction_winner(game, self.catalog)
        if winner:
            _finish(game, winner)
        else:
            self.begin_night(game)
        return DayResolution(None, tied, game.winner, None, vote_count)

    def begin_night(self, game: MafiaGame) -> None:
        if game.phase != Phase.DAY_VOTING:
            raise GameRuleError("Night can only follow daytime voting.")
        game.phase = Phase.NIGHT_MAFIA
        game.round_number += 1
        game.deadline = time.time() + game.rules.night_seconds
        game.mafia_votes.clear()
        game.votes.clear()
        game.mafia_target = None
        game.doctor_target = None
        game.detective_target = None
        game.doctor_targets.clear()
        game.detective_targets.clear()
        game.investigation_results.clear()
        game.action_message_ids.clear()

    def submit_mafia_vote(self, game: MafiaGame, actor_id: str, target_id: str) -> None:
        if game.phase != Phase.NIGHT_MAFIA:
            raise GameRuleError("Mafia target selection is not active.")
        actor = _player(game, actor_id)
        if not actor.alive or actor.role != RoleKey.MAFIA:
            raise GameRuleError("Only living Mafia members can choose a night target.")
        target = _living_target(game, target_id)
        if target.role == RoleKey.MAFIA:
            raise GameRuleError("Mafia cannot target a Mafia teammate.")
        if actor.user_id in game.mafia_votes:
            raise DuplicateActionError("Your Mafia vote has already been submitted.")
        game.mafia_votes[actor.user_id] = target.user_id
        _record_private_action(game, actor.user_id, "mafia_vote", target.user_id)

    def resolve_mafia_votes(self, game: MafiaGame) -> str | None:
        if game.phase != Phase.NIGHT_MAFIA:
            raise GameRuleError("Mafia target selection is not active.")
        eligible = {player.user_id for player in game.players_with_role(RoleKey.MAFIA)}
        counts: dict[str, int] = {}
        for actor_id, target_id in game.mafia_votes.items():
            target = game.players.get(target_id)
            if actor_id in eligible and target and target.alive and target.role != RoleKey.MAFIA:
                counts[target_id] = counts.get(target_id, 0) + 1

        target_id: str | None = None
        if counts:
            high_score = max(counts.values())
            leaders = [target for target, count in counts.items() if count == high_score]
            if len(leaders) == 1:
                target_id = leaders[0]
            elif game.rules.mafia_tie_rule == "random":
                target_id = self.rng.choice(leaders)

        game.mafia_target = target_id
        game.phase = Phase.NIGHT_ACTIONS
        game.deadline = time.time() + game.rules.night_seconds
        return target_id

    def expected_night_actions(self, game: MafiaGame) -> set[str]:
        actors: set[str] = set()
        for role, action_field in (
            (RoleKey.DOCTOR, "doctor_target"),
            (RoleKey.DETECTIVE, "detective_target"),
        ):
            if game.players_with_role(role):
                actors.add(str(role))
        return actors

    def submit_doctor_action(self, game: MafiaGame, actor_id: str, target_id: str) -> None:
        if game.phase != Phase.NIGHT_ACTIONS:
            raise GameRuleError("Doctor actions are not active.")
        actor = _player(game, actor_id)
        if not actor.alive or actor.role != RoleKey.DOCTOR:
            raise GameRuleError("Only a living Doctor can protect a player.")
        if actor.user_id in game.doctor_targets:
            raise DuplicateActionError("Your protection action has already been submitted.")
        target = _living_target(game, target_id)
        if target.user_id == actor.user_id and not game.rules.doctor_self_protection:
            raise GameRuleError("Doctor self-protection is disabled for this game.")
        game.doctor_targets[actor.user_id] = target.user_id
        if game.doctor_target is None:
            game.doctor_target = target.user_id
        _record_private_action(game, actor.user_id, "protect", target.user_id)

    def submit_detective_action(self, game: MafiaGame, actor_id: str, target_id: str) -> bool:
        if game.phase != Phase.NIGHT_ACTIONS:
            raise GameRuleError("Detective actions are not active.")
        actor = _player(game, actor_id)
        if not actor.alive or actor.role != RoleKey.DETECTIVE:
            raise GameRuleError("Only a living Detective can investigate.")
        if actor.user_id in game.detective_targets:
            raise DuplicateActionError("Your investigation has already been submitted.")
        target = _living_target(game, target_id)
        if target.user_id == actor.user_id:
            raise GameRuleError("You cannot investigate yourself.")
        game.detective_targets[actor.user_id] = target.user_id
        if game.detective_target is None:
            game.detective_target = target.user_id
        target_is_mafia = target.role == RoleKey.MAFIA
        if target.role == RoleKey.JOKER and game.rules.detective_sees_joker_as_mafia:
            target_is_mafia = True
        game.investigation_results[actor.user_id] = target_is_mafia
        _record_private_action(game, actor.user_id, "investigate", target.user_id)
        return target_is_mafia

    def night_actions_complete(self, game: MafiaGame) -> bool:
        if game.phase != Phase.NIGHT_ACTIONS:
            return False
        for role, submitted in (
            (RoleKey.DOCTOR, game.doctor_targets),
            (RoleKey.DETECTIVE, game.detective_targets),
        ):
            alive = game.players_with_role(role)
            if any(player.user_id not in submitted for player in alive):
                return False
        return True

    def resolve_night(self, game: MafiaGame) -> NightResolution:
        if game.phase != Phase.NIGHT_ACTIONS:
            raise GameRuleError("Night actions are not active.")

        investigation_results = dict(game.investigation_results)
        victim_id: str | None = None
        if game.mafia_target and game.mafia_target in game.players:
            target = game.players[game.mafia_target]
            protected_targets = set(game.doctor_targets.values())
            if game.doctor_target is not None:
                protected_targets.add(game.doctor_target)
            if target.alive and target.user_id not in protected_targets:
                target.alive = False
                victim_id = target.user_id

        if victim_id is not None:
            outcome = "killed"
        elif (
            game.mafia_target in game.players
            and game.players[game.mafia_target].alive
            and (
                game.mafia_target in game.doctor_targets.values()
                or game.doctor_target == game.mafia_target
            )
        ):
            outcome = "saved"
        else:
            outcome = "quiet"
        game.last_night_outcome = outcome

        winner = _faction_winner(game, self.catalog)
        if winner:
            _finish(game, winner)
        else:
            game.phase = Phase.DAY_DISCUSSION
            game.day_number += 1
            game.deadline = time.time() + game.rules.discussion_seconds
        return NightResolution(outcome, victim_id, investigation_results, game.winner)

    def cancel_as_setup_failure(self, game: MafiaGame) -> None:
        """End a started game if private delivery fails after Discord setup begins."""
        if game.phase in {Phase.FINISHED, Phase.CANCELLED}:
            return
        game.phase = Phase.CANCELLED
        game.deadline = None
        game.add_event("game_cancelled", "The game was cancelled because private setup failed.")
