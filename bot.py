"""discord.py runtime, persistent interactions, and timed game flow."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import logging
import os
import time
from typing import Any

import discord
from discord.ext import commands

from ai_players import (
    build_ai_players,
    choose_day_vote,
    choose_detective_target,
    choose_doctor_target,
    choose_mafia_target,
    discussion_line,
    is_opinion_message,
    reply_to_chat,
    validate_player_counts,
)
from engine import (
    DuplicateActionError,
    GameEngine,
    GameRuleError,
    NightResolution,
)
from ai_storyteller import generate_morning_story
from manager import GameManager
from models import GameRules, MafiaGame, Phase, Player, RoleKey
from presentation import (
    discussion_embed,
    elimination_embed,
    final_embed,
    lobby_embed,
    morning_embed,
    no_elimination_embed,
    player_label,
    role_embed,
    rules_embed,
    vote_embed,
)
from stories import build_morning_story, validate_story_library
from storage import SQLiteStore
from ui import (
    ActionView,
    LobbyView,
    VoteView,
    action_view_for,
    lobby_view_for,
    resolve_user_id,
    vote_view_for,
)

logger = logging.getLogger("mafia_master")


class DMPreflightFailure(RuntimeError):
    def __init__(self, blocked_names: list[str]) -> None:
        self.blocked_names = blocked_names
        super().__init__("Not every player can receive direct messages.")


class MafiaBot(commands.Bot):
    def __init__(
        self,
        *,
        database_path: str | None = None,
        message_content_intent: bool = True,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.message_content = message_content_intent
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.store = SQLiteStore(
            database_path or os.getenv("MAFIA_DATABASE_PATH", "data/mafia-master.sqlite3")
        )
        self.manager = GameManager(self.store)
        self.engine = GameEngine()
        self._phase_tasks: dict[str, asyncio.Task[None]] = {}
        self._restore_tasks: dict[str, asyncio.Task[None]] = {}
        self._tree_synced = False
        self._restored = False
        self._ai_chat_cooldowns: dict[str, float] = {}
        self.chat_replies_enabled = message_content_intent

    async def setup_hook(self) -> None:
        from commands import MafiaCommands, StartCommand

        await self.add_cog(MafiaCommands(self))
        await self.add_cog(StartCommand(self))
        self.tree.on_error = self.on_app_command_error

    async def on_connect(self) -> None:
        logger.info("Connected to Discord gateway")

    async def on_disconnect(self) -> None:
        logger.warning("Disconnected from Discord gateway; automatic reconnect is enabled")

    async def on_resumed(self) -> None:
        logger.info("Discord gateway session resumed")

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        if interaction.extras.get("mafia_error_handled"):
            return
        interaction.extras["mafia_error_handled"] = True
        if isinstance(error, discord.app_commands.errors.MissingPermissions):
            message = "You need the Manage Server permission to change game defaults."
        else:
            original = getattr(error, "original", error)
            logger.error(
                "Unhandled Mafia Master slash command error",
                exc_info=(
                    type(original),
                    original,
                    original.__traceback__ if original.__traceback__ else None,
                ),
            )
            message = "That command hit an error. Check /mafia status before retrying."
        try:
            await self.reply_private(interaction, message)
        except (discord.NotFound, discord.HTTPException):
            logger.warning("Could not send a slash-command error response")

    async def on_ready(self) -> None:
        if not self._tree_synced:
            try:
                await self.tree.sync()
                self._tree_synced = True
            except discord.HTTPException:
                logger.exception("Could not sync slash commands")
        if not self._restored:
            for game in self.manager.active_games():
                try:
                    await self.restore_game(game)
                except Exception:
                    logger.exception("Could not restore an active game")
                    self._schedule_restore_retry(game.game_id)
            self._restored = True
        logger.info("Mafia Master is ready")

    def _schedule_restore_retry(self, game_id: str) -> None:
        task = self._restore_tasks.get(game_id)
        if task is None or task.done():
            self._restore_tasks[game_id] = asyncio.create_task(
                self._retry_restore_game(game_id),
                name=f"mafia-restore-{game_id}",
            )

    async def _retry_restore_game(self, game_id: str) -> None:
        attempt = 0
        try:
            while not self.is_closed():
                await asyncio.sleep(min(2**attempt, 30))
                game = self.manager.get(game_id)
                if game is None or game.phase in {Phase.FINISHED, Phase.CANCELLED}:
                    return
                try:
                    await self.restore_game(game)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Retry %s could not restore game %s",
                        attempt + 1,
                        game_id,
                    )
                    attempt += 1
        finally:
            if self._restore_tasks.get(game_id) is asyncio.current_task():
                self._restore_tasks.pop(game_id, None)

    async def on_message(self, message: discord.Message) -> None:
        """Let a living human address an AI player during public discussion."""
        if (
            not self.chat_replies_enabled
            or message.guild is None
            or message.author.bot
            or not message.content.strip()
        ):
            return
        game = next(
            (
                active for active in self.manager.active_games()
                if active.guild_id == str(message.guild.id)
                and active.play_channel_id == str(message.channel.id)
                and active.phase == Phase.DAY_DISCUSSION
            ),
            None,
        )
        if game is None:
            return
        human = game.players.get(str(message.author.id))
        if human is None or human.is_ai or not human.alive:
            return
        ai_players = [player for player in game.living_players() if player.is_ai]
        if not ai_players:
            return

        content = message.content.strip()
        normalized = content.casefold()
        addressed = self.user is not None and self.user in message.mentions
        mentioned_ids = {str(user.id) for user in message.mentions}
        addressed_ai = [
            player for player in ai_players
            if player.name.casefold() in normalized or player.user_id in mentioned_ids
        ]
        if (
            not addressed
            and not addressed_ai
            and "?" not in content
            and not is_opinion_message(content)
        ):
            return
        now = time.monotonic()
        if now - self._ai_chat_cooldowns.get(game.game_id, 0.0) < 4:
            return
        actor = (
            addressed_ai[0]
            if addressed_ai
            else self.engine.rng.choice(ai_players)
        )
        reply = reply_to_chat(actor, content, self.engine.rng, game=game)
        self._ai_chat_cooldowns[game.game_id] = now
        try:
            await message.channel.send(
                f"**{discord.utils.escape_mentions(discord.utils.escape_markdown(actor.name))} · AI:** "
                f"{discord.utils.escape_mentions(discord.utils.escape_markdown(reply))}"
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not send an AI discussion reply")

    async def on_member_remove(self, member: discord.Member) -> None:
        """Remove lobby members and forfeit active-game players who leave."""
        for game in list(self.manager.active_games()):
            if game.guild_id != str(member.guild.id) or str(member.id) not in game.players:
                continue
            was_lobby = game.phase == Phase.LOBBY
            try:
                async with self.manager.edit(game.game_id) as current:
                    if current.phase == Phase.LOBBY:
                        if str(member.id) == current.host_id:
                            self.engine.cancel_game(current)
                        else:
                            self.engine.remove_player(current, str(member.id))
                    else:
                        self.engine.forfeit_player(current, str(member.id))
                    snapshot = deepcopy(current)
            except GameRuleError:
                continue

            if was_lobby:
                if snapshot.phase == Phase.CANCELLED:
                    await self._edit_lobby_after_start_failure(game.game_id)
                    await self._delete_game_threads(snapshot)
                else:
                    await self.refresh_lobby(game.game_id)
                continue

            channel = await self.fetch_game_channel(snapshot)
            if channel is not None and snapshot.phase != Phase.FINISHED:
                await channel.send(
                    f"{member.display_name} left the server and was removed from the game."
                )
            if snapshot.phase == Phase.FINISHED:
                await self.publish_final(game.game_id)
            elif snapshot.phase == Phase.DAY_VOTING:
                await self.refresh_vote_panel(game.game_id)
            elif snapshot.phase == Phase.NIGHT_MAFIA:
                current_mafia = {
                    player.user_id for player in snapshot.players_with_role(RoleKey.MAFIA)
                }
                if current_mafia and current_mafia.issubset(snapshot.mafia_votes):
                    await self.begin_special_actions(game.game_id)
            elif snapshot.phase == Phase.NIGHT_ACTIONS:
                if self.engine.night_actions_complete(snapshot):
                    await self.finish_night_if_complete(game.game_id, force=True)

    async def close(self) -> None:
        tasks = list(self._phase_tasks.values()) + list(self._restore_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.to_thread(self.manager.close)
        finally:
            await super().close()

    async def reply_private(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        ephemeral: bool = True,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)

    async def create_mixed_lobby(
        self,
        interaction: discord.Interaction,
        *,
        host_id: str,
        channel_id: str,
        total_players: int,
        human_players: int,
    ) -> None:
        if interaction.guild is None:
            await interaction.followup.send(
                "Create this game from a server text channel.",
                ephemeral=True,
            )
            return
        if str(interaction.user.id) != str(host_id):
            await interaction.followup.send(
                "Only the person who ran /start can create this lobby.",
                ephemeral=True,
            )
            return
        if str(interaction.channel_id) != str(channel_id):
            await interaction.followup.send(
                "Create the lobby in the channel where you ran /start.",
                ephemeral=True,
            )
            return
        try:
            validate_player_counts(total_players, human_players)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        channel = interaction.guild.get_channel(int(channel_id))
        if channel is None:
            with suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
                channel = await self.fetch_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "I could not access the server text channel for this lobby.",
                ephemeral=True,
            )
            return

        rules = await self.manager.rules_for_guild(str(interaction.guild.id))
        rules.min_players = total_players
        rules.max_players = total_players
        try:
            rules.role_counts_for(total_players)
            game = await self.manager.new_lobby(
                guild_id=str(interaction.guild.id),
                channel_id=str(channel.id),
                host_id=str(interaction.user.id),
                host_name=interaction.user.display_name,
                rules=rules,
                required_humans=human_players,
                exact_human_count=True,
                ai_fill_to_max=True,
            )
        except ValueError as exc:
            await interaction.followup.send(
                f"These player counts do not fit the server's current role settings: {exc}",
                ephemeral=True,
            )
            return

        try:
            message = await channel.send(
                embed=lobby_embed(game),
                view=LobbyView(self, game.game_id),
            )
            async with self.manager.edit(game.game_id) as current:
                current.lobby_message_id = str(message.id)
        except (discord.Forbidden, discord.HTTPException):
            async with self.manager.edit(game.game_id) as current:
                self.engine.cancel_game(current)
            await self.manager.retire(game.game_id)
            await interaction.followup.send(
                "I could not post the lobby. Check that I can view and send messages in this channel.",
                ephemeral=True,
            )
            return
        ai_count = total_players - human_players
        await interaction.followup.send(
            f"Lobby opened: **{human_players} human{'s' if human_players != 1 else ''}** "
            f"and **{ai_count} AI** for {total_players} total players. "
            "The host is already counted; friends can join with the lobby button.",
            ephemeral=True,
        )

    async def fetch_game_channel(self, game: MafiaGame) -> discord.abc.Messageable | None:
        channel_id = int(game.play_channel_id)
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _send_phase_message(
        self,
        channel: discord.abc.Messageable,
        *,
        operation: str,
        **kwargs: Any,
    ) -> discord.Message | None:
        """Keep a Discord REST send failure from stranding a game phase."""
        try:
            return await channel.send(**kwargs)
        except Exception:
            logger.exception("Could not %s", operation)
            return None

    async def fetch_parent_channel(self, game: MafiaGame) -> discord.TextChannel | None:
        channel = self.get_channel(int(game.parent_channel_id))
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.fetch_channel(int(game.parent_channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def resolve_member(self, guild: discord.Guild, user_id: str) -> discord.Member | None:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(user_id))
        except discord.NotFound:
            return None
        except (discord.Forbidden, discord.HTTPException):
            return None

    def schedule_phase(self, game: MafiaGame, *, retry_delay: float = 0.0) -> None:
        old = self._phase_tasks.get(game.game_id)
        if old is not None and old is not asyncio.current_task() and not old.done():
            old.cancel()
        if game.deadline is None or game.phase in {Phase.FINISHED, Phase.CANCELLED, Phase.LOBBY}:
            self._phase_tasks.pop(game.game_id, None)
            return
        task = asyncio.create_task(
            self._wait_for_phase(
                game.game_id,
                game.phase,
                game.deadline,
                retry_delay=retry_delay,
            ),
            name=f"mafia-phase-{game.game_id}-{game.phase.value}",
        )
        self._phase_tasks[game.game_id] = task

    async def _wait_for_phase(
        self,
        game_id: str,
        phase: Phase,
        deadline: float,
        *,
        retry_delay: float = 0.0,
    ) -> None:
        try:
            await asyncio.sleep(max(0.0, deadline - time.time()) + retry_delay)
            game = self.manager.get(game_id)
            if game is None or game.phase != phase or game.deadline != deadline:
                return
            await self.handle_phase_deadline(game_id, phase)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("A timed game phase failed")
            game = self.manager.get(game_id)
            if game is not None and game.phase == phase and game.deadline == deadline:
                # Retry transient storage or Discord failures instead of
                # leaving the current game permanently without a deadline.
                self.schedule_phase(game, retry_delay=5.0)
        finally:
            task = self._phase_tasks.get(game_id)
            if task is asyncio.current_task():
                self._phase_tasks.pop(game_id, None)

    async def restore_game(self, game: MafiaGame) -> None:
        """Reattach persistent component callbacks and resume phase deadlines."""
        if game.phase != Phase.LOBBY and not game.startup_complete:
            async with self.manager.edit(game.game_id) as current:
                self.engine.cancel_as_setup_failure(current)
                snapshot = deepcopy(current)
            channel = await self.fetch_game_channel(snapshot)
            if channel is not None:
                await self._send_phase_message(
                    channel,
                    operation="reporting an incomplete setup after restart",
                    content=(
                        "This game was cancelled because the bot restarted during private setup. "
                        "Create a fresh lobby so every player can receive a role."
                    ),
                )
            await self._edit_lobby_after_start_failure(game.game_id)
            await self._delete_game_threads(snapshot)
            return

        if game.phase == Phase.LOBBY:
            if game.lobby_message_id:
                self.add_view(LobbyView(self, game.game_id), message_id=int(game.lobby_message_id))
            else:
                channel = await self.fetch_parent_channel(game)
                if channel is not None:
                    message = await channel.send(
                        embed=lobby_embed(game),
                        view=LobbyView(self, game.game_id),
                    )
                    async with self.manager.edit(game.game_id) as current:
                        current.lobby_message_id = str(message.id)
        elif game.phase == Phase.DAY_VOTING:
            if game.vote_message_id:
                self.add_view(VoteView(self, game), message_id=int(game.vote_message_id))
            else:
                channel = await self.fetch_game_channel(game)
                if channel is not None:
                    message = await channel.send(
                        embed=vote_embed(game),
                        view=VoteView(self, game),
                    )
                    async with self.manager.edit(game.game_id) as current:
                        current.vote_message_id = str(message.id)
        elif game.phase in {Phase.NIGHT_MAFIA, Phase.NIGHT_ACTIONS}:
            for actor_id, message_id in list(game.action_message_ids.items()):
                view = action_view_for(self, game, actor_id)
                if view is not None:
                    self.add_view(view, message_id=int(message_id))
            await self.ensure_pending_action_panels(game.game_id, recovering=True)

        if game.phase == Phase.DAY_DISCUSSION:
            await self.run_ai_discussion(game.game_id)
        elif game.phase == Phase.DAY_VOTING:
            await self.run_ai_day_votes(game.game_id)
        elif game.phase == Phase.NIGHT_MAFIA:
            await self.run_ai_mafia_votes(game.game_id)
        elif game.phase == Phase.NIGHT_ACTIONS:
            await self.run_ai_special_actions(game.game_id)
        current = self.manager.get(game.game_id)
        if current is not None:
            self.schedule_phase(current)

    async def _verify_interaction_game(
        self,
        interaction: discord.Interaction,
        game_id: str,
        *,
        lobby: bool = False,
        vote: bool = False,
    ) -> MafiaGame | None:
        game = self.manager.get(game_id)
        if game is None:
            await self.reply_private(interaction, "This game is no longer available.")
            return None
        if interaction.guild_id is None or str(interaction.guild_id) != game.guild_id:
            await self.reply_private(interaction, "This game belongs to another server.")
            return None
        if lobby and (
            interaction.message is None
            or str(interaction.message.id) != game.lobby_message_id
        ):
            await self.reply_private(interaction, "This lobby control is no longer active.")
            return None
        if vote and (
            interaction.message is None
            or str(interaction.message.id) != game.vote_message_id
            or str(interaction.channel_id) != game.play_channel_id
        ):
            await self.reply_private(interaction, "This voting panel is no longer active.")
            return None
        return game

    async def refresh_lobby(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or not game.lobby_message_id:
            return
        channel = await self.fetch_parent_channel(game)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(game.lobby_message_id))
            await message.edit(embed=lobby_embed(game), view=LobbyView(self, game_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Could not update a lobby message")

    async def handle_lobby_membership(
        self,
        interaction: discord.Interaction,
        game_id: str,
        *,
        join: bool,
    ) -> None:
        game = await self._verify_interaction_game(interaction, game_id, lobby=True)
        if game is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.manager.edit(game_id) as current:
                if join:
                    self.engine.join_lobby(
                        current,
                        Player(str(interaction.user.id), interaction.user.display_name),
                    )
                    message = "You joined the lobby."
                else:
                    self.engine.leave_lobby(current, str(interaction.user.id))
                    message = "You left the lobby."
        except GameRuleError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self.refresh_lobby(game_id)
        await interaction.followup.send(message, ephemeral=True)

    async def handle_lobby_player_edit(
        self,
        interaction: discord.Interaction,
        game_id: str,
        raw_user_id: str,
        *,
        add: bool,
    ) -> None:
        game = await self._verify_interaction_game(interaction, game_id, lobby=True)
        if game is None:
            return
        await interaction.response.defer(ephemeral=True)
        if str(interaction.user.id) != game.host_id:
            await interaction.followup.send("Only the host can manage this lobby.", ephemeral=True)
            return
        user_id = resolve_user_id(raw_user_id)
        if user_id is None:
            await interaction.followup.send(
                "Enter a valid mention such as @player, or a Discord user ID.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.followup.send("This action must be used in the game server.", ephemeral=True)
            return
        member = await self.resolve_member(interaction.guild, user_id)
        if member is None:
            await interaction.followup.send("That user is not a member of this server.", ephemeral=True)
            return
        if member.bot:
            await interaction.followup.send("Bot accounts cannot join a Mafia game.", ephemeral=True)
            return
        try:
            async with self.manager.edit(game_id) as current:
                player = Player(str(member.id), member.display_name)
                if add:
                    self.engine.add_player(current, player)
                    result = f"Added {member.display_name} to the lobby."
                else:
                    self.engine.remove_player(current, str(member.id))
                    result = f"Removed {member.display_name} from the lobby."
        except GameRuleError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self.refresh_lobby(game_id)
        await interaction.followup.send(result, ephemeral=True)

    async def _create_private_thread(
        self,
        channel: discord.TextChannel,
        *,
        name: str,
        members: list[discord.Member],
    ) -> discord.Thread:
        thread = await channel.create_thread(
            name=name[:100],
            type=discord.ChannelType.private_thread,
            auto_archive_duration=10080,
            invitable=False,
            reason="Private Mafia Master game channel",
        )
        try:
            for member in members:
                await thread.add_user(member)
        except Exception:
            with suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                await thread.delete()
            raise
        return thread

    async def handle_start_game(
        self,
        interaction: discord.Interaction,
        game_id: str,
    ) -> None:
        game = await self._verify_interaction_game(interaction, game_id, lobby=True)
        if game is None:
            return
        if str(interaction.user.id) != game.host_id:
            await self.reply_private(interaction, "Only the host can start this game.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("This action must be used in the game server.", ephemeral=True)
            return

        parent = await self.fetch_parent_channel(game)
        if parent is None:
            await interaction.followup.send("I could not access the lobby channel.", ephemeral=True)
            return

        members: dict[str, discord.Member] = {}
        for player_id in list(game.players):
            member = await self.resolve_member(interaction.guild, player_id)
            if member is None:
                await interaction.followup.send(
                    "A joined player is no longer in this server. Remove them from the lobby before starting.",
                    ephemeral=True,
                )
                return
            if member.bot:
                await interaction.followup.send(
                    "Bot accounts cannot join a Mafia game. Remove the bot player from the lobby.",
                    ephemeral=True,
                )
                return
            members[player_id] = member

        def validate_start_roster(current: MafiaGame) -> tuple[int, int]:
            if current.phase != Phase.LOBBY:
                raise GameRuleError("This game has already started.")
            if set(current.players) != set(members):
                raise GameRuleError(
                    "The lobby changed while start-up was preparing. Please try again."
                )
            human_count = sum(not player.is_ai for player in current.players.values())
            if current.required_humans is not None:
                enough_humans = (
                    human_count == current.required_humans
                    if current.exact_human_count
                    else human_count >= current.required_humans
                )
                if not enough_humans:
                    qualifier = "exactly" if current.exact_human_count else "at least"
                    raise GameRuleError(
                        f"This lobby needs {qualifier} {current.required_humans} human players; "
                        f"{human_count} have joined."
                    )
                total_player_count = (
                    current.rules.max_players
                    if current.ai_fill_to_max
                    else len(current.players)
                )
            else:
                total_player_count = len(current.players)
            current.rules.role_counts_for(total_player_count)
            return human_count, total_player_count

        try:
            snapshot = await self.manager.snapshot(game_id)
            if snapshot is None:
                raise GameRuleError("This game is no longer available.")
            human_count, total_player_count = validate_start_roster(snapshot)
        except (GameRuleError, ValueError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        # Network calls stay outside the per-game state lock. Slow DMs must
        # not hold other lobby operations behind the lock.
        blocked: list[str] = []
        for member in members.values():
            try:
                await member.send(
                    "Mafia Master is checking private messages before the game starts. "
                    "This is only a delivery check; no role has been assigned."
                )
            except (discord.Forbidden, discord.HTTPException):
                blocked.append(member.display_name)
        if blocked:
            names = ", ".join(blocked[:10])
            await interaction.followup.send(
                f"Could not DM every player ({names}). Ask them to allow server-member DMs, then try again.",
                ephemeral=True,
            )
            return

        try:
            async with self.manager.edit(game_id) as current:
                human_count, total_player_count = validate_start_roster(current)
                for player in current.human_players():
                    player.dm_preflight_ok = True
                if current.ai_fill_to_max:
                    ai_players = build_ai_players(
                        current.game_id,
                        total_player_count,
                        human_count,
                        existing_names=[player.name for player in current.players.values()],
                        rng=self.engine.rng,
                    )
                    current.players.update(
                        {player.user_id: player for player in ai_players}
                    )
                self.engine.start_game(current)
                started_snapshot = deepcopy(current)
        except (GameRuleError, ValueError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        created_threads: list[discord.Thread] = []
        try:
            if not started_snapshot.rules.spectators_can_see:
                game_thread = await self._create_private_thread(
                    parent,
                    name=f"mafia-game-{game_id[:8]}",
                    members=list(members.values()),
                )
                created_threads.append(game_thread)
                async with self.manager.edit(game_id) as current:
                    current.play_thread_id = str(game_thread.id)
                started_snapshot = deepcopy(self.manager.get(game_id))

            human_mafia_ids = [
                player.user_id
                for player in started_snapshot.players.values()
                if player.role == RoleKey.MAFIA and not player.is_ai
            ]
            if human_mafia_ids:
                mafia_thread = await self._create_private_thread(
                    parent,
                    name=f"mafia-private-{game_id[:8]}",
                    members=[members[player_id] for player_id in human_mafia_ids],
                )
                created_threads.append(mafia_thread)
                async with self.manager.edit(game_id) as current:
                    current.mafia_thread_id = str(mafia_thread.id)
                started_snapshot = deepcopy(self.manager.get(game_id))
                await mafia_thread.send(
                    "Private Mafia coordination is open. Discuss here; each human member submits one secret night vote in their direct message."
                )
        except (discord.Forbidden, discord.HTTPException):
            for thread in created_threads:
                with suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                    await thread.delete()
            async with self.manager.edit(game_id) as current:
                if current.phase == Phase.DAY_DISCUSSION:
                    self.engine.reset_to_lobby(current)
                    current.play_thread_id = None
                    current.mafia_thread_id = None
                    for player in current.players.values():
                        player.dm_preflight_ok = False
            await interaction.followup.send(
                "I could not create the private game and Mafia threads. Give the bot permission to "
                "create private threads, send messages in threads, and add thread members, then try again.",
                ephemeral=True,
            )
            return

        try:
            for player_id, player in started_snapshot.players.items():
                if player.is_ai:
                    continue
                member = members[player_id]
                view: ActionView | None = None
                if player.role == RoleKey.MAFIA:
                    view = action_view_for(self, started_snapshot, player_id)
                message = await member.send(embed=role_embed(started_snapshot, player_id), view=view)
                if player.role == RoleKey.MAFIA:
                    async with self.manager.edit(game_id) as current:
                        current.action_message_ids[player_id] = str(message.id)
        except (discord.Forbidden, discord.HTTPException):
            async with self.manager.edit(game_id) as current:
                self.engine.cancel_as_setup_failure(current)
                failed_snapshot = deepcopy(current)
            await self._edit_lobby_after_start_failure(game_id)
            await self._delete_game_threads(failed_snapshot)
            await interaction.followup.send(
                "A private role message failed after the pre-check. I cancelled this game so nobody plays with an unknown role. "
                "Check that player's DM settings and create a fresh lobby.",
                ephemeral=True,
            )
            return

        game = self.manager.get(game_id)
        if game is None:
            await interaction.followup.send("The game state could not be loaded.", ephemeral=True)
            return
        play_channel = await self.fetch_game_channel(game)
        if play_channel is None:
            async with self.manager.edit(game_id) as current:
                self.engine.cancel_as_setup_failure(current)
                failed_snapshot = deepcopy(current)
            await self._edit_lobby_after_start_failure(game_id)
            await self._delete_game_threads(failed_snapshot)
            await interaction.followup.send("I could not access the game channel.", ephemeral=True)
            return

        opening_message = await self._send_phase_message(
            play_channel,
            operation="post the opening discussion message",
            embed=discussion_embed(game, opening=True),
        )
        if opening_message is None:
            async with self.manager.edit(game_id) as current:
                self.engine.cancel_as_setup_failure(current)
                failed_snapshot = deepcopy(current)
            await self._edit_lobby_after_start_failure(game_id)
            await self._delete_game_threads(failed_snapshot)
            await interaction.followup.send(
                "I could not post the opening game message, so I cancelled the game safely. "
                "Check the bot's channel permissions and start a new lobby.",
                ephemeral=True,
            )
            return
        async with self.manager.edit(game_id) as current:
            current.startup_complete = True
            game = deepcopy(current)
        await self._edit_lobby_after_start(game_id)
        self.schedule_phase(game)
        await interaction.followup.send(
            "Game started. Role cards were sent privately; Day 1 discussion is open.",
            ephemeral=True,
        )
        await self.run_ai_discussion(game_id)

    async def _edit_lobby_after_start_failure(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or not game.lobby_message_id:
            return
        channel = await self.fetch_parent_channel(game)
        if channel is None:
            return
        with suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
            message = await channel.fetch_message(int(game.lobby_message_id))
            embed = discord.Embed(
                title="Mafia Master · Game Cancelled",
                description="This lobby is closed. Create a new lobby to try again.",
                color=0x8D3845,
            )
            await message.edit(embed=embed, view=None)

    async def _edit_lobby_after_start(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or not game.lobby_message_id:
            return
        channel = await self.fetch_parent_channel(game)
        if channel is None:
            return
        embed = discord.Embed(
            title="Mafia Master · Game Started",
            description=(
                f"The host started a game with {len(game.players)} players. "
                + (
                    "Players-only events are taking place in the private game thread."
                    if not game.rules.spectators_can_see
                    else "Public game events are in this channel."
                )
            ),
            color=0x347A5B,
        )
        if game.play_thread_id:
            embed.add_field(name="Game thread", value=f"<#{game.play_thread_id}>", inline=False)
        with suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
            message = await channel.fetch_message(int(game.lobby_message_id))
            await message.edit(embed=embed, view=None)

    async def handle_cancel_game(
        self,
        interaction: discord.Interaction,
        game_id: str,
    ) -> None:
        game = await self._verify_interaction_game(interaction, game_id, lobby=True)
        if game is None:
            return
        if str(interaction.user.id) != game.host_id:
            await self.reply_private(interaction, "Only the host can cancel this game.")
            return
        if game.phase != Phase.LOBBY:
            await self.reply_private(interaction, "Only an open lobby can be cancelled from this control.")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.manager.edit(game_id) as current:
                self.engine.cancel_game(current)
        except GameRuleError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self._edit_lobby_after_start_failure(game_id)
        cancelled = self.manager.get(game_id)
        if cancelled is not None:
            await self._delete_game_threads(cancelled)
        await interaction.followup.send("Lobby cancelled.", ephemeral=True)

    async def handle_vote(
        self,
        interaction: discord.Interaction,
        game_id: str,
        target_id: str,
    ) -> None:
        game = await self._verify_interaction_game(interaction, game_id, vote=True)
        if game is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.manager.edit(game_id) as current:
                self.engine.submit_day_vote(current, str(interaction.user.id), target_id)
            await self.refresh_vote_panel(game_id)
            await interaction.followup.send("Your vote was recorded. You can change it before voting closes.", ephemeral=True)
            current = self.manager.get(game_id)
            if current is not None:
                living_ids = {player.user_id for player in current.living_players()}
                if current.phase == Phase.DAY_VOTING and living_ids.issubset(current.votes):
                    await self.finish_day_vote(game_id, force=False)
        except GameRuleError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def run_ai_discussion(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if (
            game is None
            or game.phase != Phase.DAY_DISCUSSION
            or game.ai_discussion_day >= game.day_number
        ):
            return
        async with self.manager.edit(game_id) as current:
            if current.phase != Phase.DAY_DISCUSSION or current.ai_discussion_day >= current.day_number:
                return
            current.ai_discussion_day = current.day_number
            players = [player for player in current.living_players() if player.is_ai]
            day_number = current.day_number
        if not players:
            return
        channel = await self.fetch_game_channel(game)
        if channel is None:
            return
        for index, player in enumerate(players):
            line = discussion_line(player, day_number, self.engine.rng)
            content = (
                f"**{discord.utils.escape_mentions(discord.utils.escape_markdown(player.name))} · AI:** "
                f"{line}"
            )
            try:
                await channel.send(content)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not send an AI discussion line")
                break
            if index + 1 < len(players):
                await asyncio.sleep(1.1)

    async def run_ai_day_votes(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or game.phase != Phase.DAY_VOTING:
            return
        actor_ids = [
            player.user_id for player in game.living_players()
            if player.is_ai
        ]
        for actor_id in actor_ids:
            current = self.manager.get(game_id)
            if current is None or current.phase != Phase.DAY_VOTING:
                return
            actor = current.players.get(actor_id)
            if actor is None or not actor.alive or actor_id in current.votes:
                continue
            target_id = choose_day_vote(current, actor, self.engine.rng)
            try:
                async with self.manager.edit(game_id) as editable:
                    self.engine.submit_day_vote(editable, actor_id, target_id)
            except (GameRuleError, DuplicateActionError):
                logger.exception("An AI player could not submit its daytime vote")
        await self.refresh_vote_panel(game_id)
        current = self.manager.get(game_id)
        if current is not None and current.phase == Phase.DAY_VOTING:
            living_ids = {player.user_id for player in current.living_players()}
            if living_ids.issubset(current.votes):
                await self.finish_day_vote(game_id, force=False)

    async def run_ai_mafia_votes(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or game.phase != Phase.NIGHT_MAFIA:
            return
        actor_ids = [
            player.user_id for player in game.players_with_role(RoleKey.MAFIA)
            if player.is_ai
        ]
        for actor_id in actor_ids:
            current = self.manager.get(game_id)
            if current is None or current.phase != Phase.NIGHT_MAFIA:
                return
            actor = current.players.get(actor_id)
            if actor is None or not actor.alive or actor_id in current.mafia_votes:
                continue
            target_id = choose_mafia_target(current, actor, self.engine.rng)
            if target_id is None:
                continue
            try:
                async with self.manager.edit(game_id) as editable:
                    self.engine.submit_mafia_vote(editable, actor_id, target_id)
            except (GameRuleError, DuplicateActionError):
                logger.exception("An AI Mafia member could not submit its target vote")
        current = self.manager.get(game_id)
        if current is not None and current.phase == Phase.NIGHT_MAFIA:
            mafia_ids = {
                player.user_id for player in current.players_with_role(RoleKey.MAFIA)
            }
            if mafia_ids.issubset(current.mafia_votes):
                await self.begin_special_actions(game_id)

    async def run_ai_special_actions(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or game.phase != Phase.NIGHT_ACTIONS:
            return
        for role, submitted_field, choose_target, submit in (
            (
                RoleKey.DOCTOR,
                "doctor_targets",
                choose_doctor_target,
                self.engine.submit_doctor_action,
            ),
            (
                RoleKey.DETECTIVE,
                "detective_targets",
                choose_detective_target,
                self.engine.submit_detective_action,
            ),
        ):
            current = self.manager.get(game_id)
            if current is None or current.phase != Phase.NIGHT_ACTIONS:
                return
            actor_ids = [
                actor.user_id
                for actor in current.players_with_role(role)
                if actor.is_ai and actor.user_id not in getattr(current, submitted_field)
            ]
            for actor_id in actor_ids:
                current = self.manager.get(game_id)
                if current is None or current.phase != Phase.NIGHT_ACTIONS:
                    return
                actor = current.players.get(actor_id)
                submitted = getattr(current, submitted_field)
                if actor is None or not actor.alive or actor_id in submitted:
                    continue
                target_id = choose_target(current, actor, self.engine.rng)
                if target_id is None:
                    continue
                try:
                    async with self.manager.edit(game_id) as editable:
                        submit(editable, actor_id, target_id)
                except (GameRuleError, DuplicateActionError):
                    logger.exception("An AI night action could not be submitted")
        current = self.manager.get(game_id)
        if current is not None and current.phase == Phase.NIGHT_ACTIONS:
            if self.engine.night_actions_complete(current):
                await self.finish_night_if_complete(game_id, force=False)

    async def refresh_vote_panel(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or not game.vote_message_id:
            return
        channel = await self.fetch_game_channel(game)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(game.vote_message_id))
            await message.edit(embed=vote_embed(game), view=VoteView(self, game))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Could not update the vote dashboard")

    async def handle_night_action(
        self,
        interaction: discord.Interaction,
        game_id: str,
        actor_id: str,
        action: str,
        target_id: str,
    ) -> None:
        if str(interaction.user.id) != str(actor_id):
            await self.reply_private(interaction, "This action panel belongs to another player.")
            return
        await interaction.response.defer(ephemeral=True)
        game = self.manager.get(game_id)
        if game is None:
            await self.reply_private(interaction, "This game is no longer available.")
            return
        try:
            async with self.manager.edit(game_id) as current:
                if action == "mafia":
                    self.engine.submit_mafia_vote(current, actor_id, target_id)
                    mafia_ids = {
                        player.user_id for player in current.players_with_role(RoleKey.MAFIA)
                    }
                    mafia_complete = mafia_ids.issubset(current.mafia_votes)
                    action_result = None
                elif action == "doctor":
                    self.engine.submit_doctor_action(current, actor_id, target_id)
                    mafia_complete = False
                    action_result = None
                elif action == "detective":
                    action_result = self.engine.submit_detective_action(
                        current,
                        actor_id,
                        target_id,
                    )
                    mafia_complete = False
                else:
                    raise GameRuleError("This action is not supported.")
            if action == "mafia" and mafia_complete:
                await self.begin_special_actions(game_id)
            elif action in {"doctor", "detective"}:
                await self.finish_night_if_complete(game_id, force=False)
            elif action_result is not None:
                # The actual result arrives privately after the night resolves.
                pass
            await interaction.followup.send(
                "Your private action was recorded. Investigation results are delivered after the night resolves.",
                ephemeral=True,
            )
        except (GameRuleError, DuplicateActionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def ensure_pending_action_panels(
        self,
        game_id: str,
        *,
        recovering: bool = False,
    ) -> None:
        game = self.manager.get(game_id)
        if game is None or game.phase not in {Phase.NIGHT_MAFIA, Phase.NIGHT_ACTIONS}:
            return

        pending: list[str] = []
        if game.phase == Phase.NIGHT_MAFIA:
            for player in game.players_with_role(RoleKey.MAFIA):
                if not player.is_ai and player.user_id not in game.mafia_votes:
                    pending.append(player.user_id)
        else:
            for player in game.players_with_role(RoleKey.DOCTOR):
                if not player.is_ai and player.user_id not in game.doctor_targets:
                    pending.append(player.user_id)
            for player in game.players_with_role(RoleKey.DETECTIVE):
                if not player.is_ai and player.user_id not in game.detective_targets:
                    pending.append(player.user_id)

        for actor_id in pending:
            view = action_view_for(self, game, actor_id)
            if view is None:
                continue
            old_message_id = game.action_message_ids.get(actor_id)
            if recovering and old_message_id:
                guild = self.get_guild(int(game.guild_id))
                if guild is None:
                    with suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
                        guild = await self.fetch_guild(int(game.guild_id))
                member = await self.resolve_member(guild, actor_id) if guild else None
                if member is not None:
                    with suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
                        dm = member.dm_channel or await member.create_dm()
                        await dm.fetch_message(int(old_message_id))
                        self.add_view(view, message_id=int(old_message_id))
                        continue
            await self.send_action_panel(game_id, actor_id, view=view)

    async def send_action_panel(
        self,
        game_id: str,
        actor_id: str,
        *,
        view: ActionView | None = None,
    ) -> None:
        game = self.manager.get(game_id)
        if game is None:
            return
        player = game.players.get(actor_id)
        if player is None or player.is_ai:
            return
        view = view or action_view_for(self, game, actor_id)
        if view is None:
            return
        guild = self.get_guild(int(game.guild_id))
        if guild is None:
            with suppress(discord.HTTPException):
                guild = await self.fetch_guild(int(game.guild_id))
        if guild is None:
            return
        member = await self.resolve_member(guild, actor_id)
        if member is None:
            return

        if player.role == RoleKey.MAFIA:
            coordination = (
                f"Coordinate with your teammates in <#{game.mafia_thread_id}>."
                if game.mafia_thread_id
                else "Your Mafia teammates are AI players; submit your private target vote below."
            )
            content = f"Night {game.round_number}: submit your one private target vote. {coordination}"
        elif player.role == RoleKey.DOCTOR:
            if game.mafia_target and game.mafia_target in game.players:
                target = game.players[game.mafia_target]
                content = (
                    f"The Mafia selected **{target.name}**. Choose a living player to protect."
                )
            else:
                content = "The Mafia did not select a target. Choose a living player to protect, or let the action deadline pass."
        else:
            content = "Choose one living player to investigate. Your result will be sent only to you."
        try:
            message = await member.send(
                content=content,
                embed=role_embed(game, actor_id),
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not deliver a private night action panel")
            return
        async with self.manager.edit(game_id) as current:
            current.action_message_ids[actor_id] = str(message.id)

    async def begin_special_actions(self, game_id: str) -> None:
        async with self.manager.edit(game_id) as game:
            if game.phase != Phase.NIGHT_MAFIA:
                return
            self.engine.resolve_mafia_votes(game)
        await self.ensure_pending_action_panels(game_id)
        await self.run_ai_special_actions(game_id)
        current = self.manager.get(game_id)
        if current is None or current.phase != Phase.NIGHT_ACTIONS:
            return
        if self.engine.night_actions_complete(current):
            await self.finish_night_if_complete(game_id, force=True)
        else:
            self.schedule_phase(current)

    async def finish_night_if_complete(self, game_id: str, *, force: bool) -> None:
        resolution: NightResolution | None = None
        async with self.manager.edit(game_id) as game:
            if game.phase != Phase.NIGHT_ACTIONS:
                return
            if not force and not self.engine.night_actions_complete(game):
                return
            resolution = self.engine.resolve_night(game)
            victim_name = (
                discord.utils.escape_mentions(
                    discord.utils.escape_markdown(
                        game.players[resolution.victim_id].name
                    )
                )
                if resolution.victim_id
                else None
            )
            story = build_morning_story(
                resolution.outcome,
                victim_name,
            )
            snapshot = deepcopy(game)
        if resolution is None:
            return
        if resolution.winner is None:
            # The next phase is durable already. Arm its deadline before any
            # Discord REST request so a failed announcement cannot strand it.
            self.schedule_phase(snapshot)
        outcome_text = (
            f"{discord.utils.escape_mentions(discord.utils.escape_markdown(snapshot.players[resolution.victim_id].name))} "
            "was eliminated overnight."
            if resolution.victim_id
            else "No player was eliminated overnight."
        )
        ai_story = None
        if snapshot.rules.ai_storytelling_enabled:
            ai_story = await generate_morning_story(
                day_number=snapshot.day_number,
                player_eliminated=resolution.victim_id is not None,
                # Names are used only for local rejection checks and never sent to OpenAI.
                player_names=tuple(player.name for player in snapshot.players.values()),
            )
        if ai_story is not None:
            story = ai_story
        async with self.manager.edit(game_id) as game:
            game.add_event(
                "night_outcome",
                f"{outcome_text} {story}" if ai_story is not None else story,
            )
            snapshot = deepcopy(game)
        await self.deliver_investigation_results(snapshot, resolution)
        channel = await self.fetch_game_channel(snapshot)
        if channel is not None:
            await self._send_phase_message(
                channel,
                operation="post the morning announcement",
                embed=morning_embed(snapshot, story, outcome_text=outcome_text),
            )
        else:
            logger.error("Could not find the game channel for morning announcement")
        if resolution.winner:
            await self.publish_final(snapshot.game_id)
        else:
            await self.run_ai_discussion(game_id)

    async def deliver_investigation_results(
        self,
        game: MafiaGame,
        resolution: NightResolution,
    ) -> None:
        if not resolution.investigation_results:
            return
        guild = self.get_guild(int(game.guild_id))
        if guild is None:
            with suppress(discord.HTTPException):
                guild = await self.fetch_guild(int(game.guild_id))
        if guild is None:
            return
        for actor_id, is_mafia in resolution.investigation_results.items():
            actor = game.players.get(actor_id)
            if actor is None or actor.is_ai:
                continue
            target_id = game.detective_targets.get(actor_id, game.detective_target)
            target_name = game.players[target_id].name if target_id in game.players else "that player"
            member = await self.resolve_member(guild, actor_id)
            if member is None:
                continue
            try:
                await member.send(
                    f"Night {game.round_number} investigation: **{target_name}** is "
                    f"**{'Mafia' if is_mafia else 'Not Mafia'}**."
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not deliver a private investigation result")

    async def start_night(self, game_id: str) -> None:
        async with self.manager.edit(game_id) as game:
            if game.phase != Phase.NIGHT_MAFIA:
                return
            snapshot = deepcopy(game)
        self.schedule_phase(snapshot)
        channel = await self.fetch_game_channel(snapshot)
        if channel is not None:
            await self._send_phase_message(
                channel,
                operation="post the night announcement",
                embed=discord.Embed(
                    title=f"Night {snapshot.round_number}",
                    description="The town is asleep. Night actions are being collected privately.",
                    color=0x20252E,
                ),
            )
        else:
            logger.error("Could not find the game channel for the night announcement")
        await self.run_ai_mafia_votes(game_id)
        await self.ensure_pending_action_panels(game_id)
        current = self.manager.get(game_id)
        if current is not None:
            self.schedule_phase(current)

    async def handle_phase_deadline(self, game_id: str, expected_phase: Phase) -> None:
        game = self.manager.get(game_id)
        if game is None or game.phase != expected_phase:
            return
        if expected_phase == Phase.DAY_DISCUSSION:
            await self.start_day_voting(game_id)
        elif expected_phase == Phase.DAY_VOTING:
            await self.finish_day_vote(game_id, force=True)
        elif expected_phase == Phase.NIGHT_MAFIA:
            await self.begin_special_actions(game_id)
        elif expected_phase == Phase.NIGHT_ACTIONS:
            await self.finish_night_if_complete(game_id, force=True)

    async def start_day_voting(self, game_id: str) -> None:
        async with self.manager.edit(game_id) as game:
            if game.phase != Phase.DAY_DISCUSSION:
                return
            self.engine.begin_voting(game)
            snapshot = deepcopy(game)
        self.schedule_phase(snapshot)
        channel = await self.fetch_game_channel(snapshot)
        message = None
        if channel is not None:
            message = await self._send_phase_message(
                channel,
                operation="post the daytime voting panel",
                embed=vote_embed(snapshot),
                view=VoteView(self, snapshot),
            )
        else:
            logger.error("Could not find the game channel for the voting panel")
        if message is not None:
            async with self.manager.edit(game_id) as game:
                if game.phase == Phase.DAY_VOTING:
                    game.vote_message_id = str(message.id)
        await self.run_ai_day_votes(game_id)
        current = self.manager.get(game_id)
        if current is not None:
            self.schedule_phase(current)

    async def finish_day_vote(self, game_id: str, *, force: bool) -> None:
        async with self.manager.edit(game_id) as game:
            if game.phase != Phase.DAY_VOTING:
                return
            living_ids = {player.user_id for player in game.living_players()}
            all_voted = living_ids.issubset(game.votes)
            if not force and not all_voted:
                return
            result = self.engine.resolve_day_vote(game)
            snapshot = deepcopy(game)
            vote_message_id = game.vote_message_id
        channel = await self.fetch_game_channel(snapshot)
        if channel is not None:
            if vote_message_id:
                with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                    old_vote = await channel.fetch_message(int(vote_message_id))
                    await old_vote.edit(view=None)
            if result.eliminated_id:
                await self._send_phase_message(
                    channel,
                    operation="post the daytime elimination result",
                    embed=elimination_embed(
                        snapshot,
                        result.eliminated_id,
                        result.revealed_role,
                    ),
                )
            else:
                await self._send_phase_message(
                    channel,
                    operation="post the daytime voting result",
                    embed=no_elimination_embed(snapshot, result.tied_ids),
                )
        else:
            logger.error("Could not find the game channel for the daytime result")

        if result.winner:
            await self.publish_final(game_id)
        else:
            current = self.manager.get(game_id)
            if current is not None:
                await self.start_night(game_id)

    async def publish_final(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or game.phase != Phase.FINISHED:
            return
        channel = await self.fetch_game_channel(game)
        if channel is not None:
            await self._send_phase_message(
                channel,
                operation="publish final game results",
                embed=final_embed(game),
            )
        else:
            logger.error("Could not find the game channel for final results")
        await self._delete_game_threads(game)
        task = self._phase_tasks.pop(game_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _delete_game_threads(self, game: MafiaGame) -> None:
        """Delete only threads created for this game, leaving its parent channel intact."""
        thread_ids = list(dict.fromkeys(
            thread_id
            for thread_id in (game.play_thread_id, game.mafia_thread_id)
            if thread_id
        ))
        deleted_ids: set[str] = set()
        for thread_id in thread_ids:
            try:
                thread = self.get_channel(int(thread_id))
                if thread is None:
                    thread = await self.fetch_channel(int(thread_id))
                if not isinstance(thread, discord.Thread):
                    logger.warning("Game thread %s was unavailable for cleanup", thread_id)
                    continue
                await thread.delete(reason="Mafia game finished or was cancelled")
                deleted_ids.add(thread_id)
            except discord.NotFound:
                deleted_ids.add(thread_id)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not delete game thread %s", thread_id)

        if deleted_ids:
            try:
                async with self.manager.edit(game.game_id) as current:
                    if current.play_thread_id in deleted_ids:
                        current.play_thread_id = None
                    if current.mafia_thread_id in deleted_ids:
                        current.mafia_thread_id = None
            except KeyError:
                # Another simultaneous finalization already retired the game.
                pass
        current = self.manager.get(game.game_id)
        if current is not None and current.phase in {Phase.FINISHED, Phase.CANCELLED}:
            self._ai_chat_cooldowns.pop(game.game_id, None)
            task = self._phase_tasks.pop(game.game_id, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            await self.manager.retire(game.game_id)

    async def restore_vote_panel(self, game_id: str) -> None:
        game = self.manager.get(game_id)
        if game is None or game.phase != Phase.DAY_VOTING:
            return
        if game.vote_message_id:
            self.add_view(VoteView(self, game), message_id=int(game.vote_message_id))
        self.schedule_phase(game)
