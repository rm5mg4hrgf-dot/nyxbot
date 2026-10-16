"""SQLite persistence for games and per-server rule presets."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from models import GameRules, MafiaGame, Phase, phase_is_active


class SQLiteStore:
    """Small synchronous SQLite store; each short operation is serialized."""

    def __init__(self, database_path: str | Path = "data/mafia-master.sqlite3") -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
            timeout=30,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock, closing(self._connection.cursor()) as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS games (
                    game_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    parent_channel_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS games_guild_phase
                ON games (guild_id, phase)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id TEXT PRIMARY KEY,
                    rules_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.commit()

    def save_game(self, game: MafiaGame) -> None:
        state = json.dumps(game.to_dict(), separators=(",", ":"), ensure_ascii=False)
        with self._lock, closing(self._connection.cursor()) as cursor:
            cursor.execute(
                """
                INSERT INTO games (game_id, guild_id, parent_channel_id, phase, state_json, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(game_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    parent_channel_id = excluded.parent_channel_id,
                    phase = excluded.phase,
                    state_json = excluded.state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    game.game_id,
                    game.guild_id,
                    game.parent_channel_id,
                    game.phase.value,
                    state,
                ),
            )
            self._connection.commit()

    def load_games(self, *, active_only: bool = False) -> list[MafiaGame]:
        with self._lock, closing(self._connection.cursor()) as cursor:
            if active_only:
                phases = tuple(phase.value for phase in Phase if phase_is_active(phase))
                placeholders = ",".join("?" for _ in phases)
                rows = cursor.execute(
                    f"SELECT state_json FROM games WHERE phase IN ({placeholders})",
                    phases,
                ).fetchall()
            else:
                rows = cursor.execute("SELECT state_json FROM games").fetchall()
        return [MafiaGame.from_dict(json.loads(row["state_json"])) for row in rows]

    def get_game(self, game_id: str) -> MafiaGame | None:
        with self._lock, closing(self._connection.cursor()) as cursor:
            row = cursor.execute(
                "SELECT state_json FROM games WHERE game_id = ?",
                (str(game_id),),
            ).fetchone()
        return MafiaGame.from_dict(json.loads(row["state_json"])) if row else None

    def get_guild_rules(self, guild_id: str) -> GameRules:
        with self._lock, closing(self._connection.cursor()) as cursor:
            row = cursor.execute(
                "SELECT rules_json FROM guild_settings WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        if row is None:
            return GameRules()
        return GameRules.from_dict(json.loads(row["rules_json"]))

    def save_guild_rules(self, guild_id: str, rules: GameRules) -> None:
        rules.validate()
        encoded = json.dumps(rules.to_dict(), separators=(",", ":"))
        with self._lock, closing(self._connection.cursor()) as cursor:
            cursor.execute(
                """
                INSERT INTO guild_settings (guild_id, rules_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id) DO UPDATE SET
                    rules_json = excluded.rules_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (str(guild_id), encoded),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
