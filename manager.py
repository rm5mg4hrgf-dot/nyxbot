"""Per-game locking, in-memory registry, and durable state coordination."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import uuid
from collections.abc import AsyncIterator

from .models import GameRules, MafiaGame, Phase, Player, phase_is_active
from .storage import SQLiteStore


class GameManager:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.games: dict[str, MafiaGame] = {
            game.game_id: game for game in store.load_games(active_only=True)
        }
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    def lock_for(self, game_id: str) -> asyncio.Lock:
        return self._locks.setdefault(str(game_id), asyncio.Lock())

    @asynccontextmanager
    async def edit(self, game_id: str) -> AsyncIterator[MafiaGame]:
        """Serialize each game's state changes and persist successful mutations."""
        game_id = str(game_id)
        async with self.lock_for(game_id):
            game = self.games.get(game_id)
            if game is None:
                raise KeyError("This game could not be found.")
            original = deepcopy(game)
            persisted = False
            try:
                yield game
                # SQLite is synchronous and has a 30-second busy timeout. Keep
                # it off Discord's event loop so a locked database cannot
                # freeze every command and component callback.
                save_task = asyncio.create_task(
                    asyncio.to_thread(self.store.save_game, deepcopy(game))
                )
                try:
                    await asyncio.shield(save_task)
                except asyncio.CancelledError:
                    # Don't release the per-game lock while the worker may
                    # still be committing this snapshot.
                    await asyncio.shield(save_task)
                    persisted = True
                    raise
                persisted = True
            except BaseException:
                if not persisted:
                    self.games[game_id] = original
                raise

    async def new_lobby(
        self,
        *,
        guild_id: str,
        channel_id: str,
        host_id: str,
        host_name: str,
        rules: GameRules,
        required_humans: int | None = None,
        exact_human_count: bool = False,
        ai_fill_to_max: bool = False,
    ) -> MafiaGame:
        rules.validate()
        if required_humans is not None and not 1 <= required_humans <= rules.max_players:
            raise ValueError("Required human players must be between 1 and the total player count.")
        async with self._registry_lock:
            if any(
                game.guild_id == str(guild_id)
                and game.parent_channel_id == str(channel_id)
                and game.phase not in {Phase.FINISHED, Phase.CANCELLED}
                for game in self.games.values()
            ):
                raise ValueError("This channel already has an active Mafia game.")
            game_id = uuid.uuid4().hex
            game = MafiaGame(
                game_id=game_id,
                guild_id=str(guild_id),
                parent_channel_id=str(channel_id),
                host_id=str(host_id),
                host_name=host_name,
                rules=deepcopy(rules),
                players={str(host_id): Player(str(host_id), host_name)},
                required_humans=required_humans,
                exact_human_count=exact_human_count,
                ai_fill_to_max=ai_fill_to_max,
            )
            await asyncio.to_thread(self.store.save_game, game)
            self.games[game_id] = game
            self._locks[game_id] = asyncio.Lock()
            return game

    def get(self, game_id: str) -> MafiaGame | None:
        return self.games.get(str(game_id))

    async def snapshot(self, game_id: str) -> MafiaGame | None:
        """Return a consistent copy without holding the game lock over I/O."""
        game_id = str(game_id)
        async with self.lock_for(game_id):
            game = self.games.get(game_id)
            return deepcopy(game) if game is not None else None

    async def retire(self, game_id: str) -> bool:
        """Drop terminal games from RAM while retaining their durable record."""
        game_id = str(game_id)
        lock = self.lock_for(game_id)
        async with lock:
            game = self.games.get(game_id)
            if game is None or game.phase not in {Phase.FINISHED, Phase.CANCELLED}:
                return False
            self.games.pop(game_id, None)
            self._locks.pop(game_id, None)
            return True

    def active_in_channel(self, guild_id: str, channel_id: str) -> MafiaGame | None:
        for game in self.games.values():
            if (
                game.guild_id == str(guild_id)
                and game.parent_channel_id == str(channel_id)
                and phase_is_active(game.phase)
            ):
                return game
        return None

    def active_games(self) -> list[MafiaGame]:
        return [game for game in self.games.values() if phase_is_active(game.phase)]

    async def rules_for_guild(self, guild_id: str) -> GameRules:
        return await asyncio.to_thread(self.store.get_guild_rules, str(guild_id))

    async def save_rules(self, guild_id: str, rules: GameRules) -> None:
        await asyncio.to_thread(
            self.store.save_guild_rules,
            str(guild_id),
            deepcopy(rules),
        )

    def close(self) -> None:
        self.store.close()
