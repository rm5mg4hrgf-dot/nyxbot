"""Persistent Discord buttons, selects, and lobby modals."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord

from models import MafiaGame, Phase, RoleKey

if TYPE_CHECKING:
    from .bot import MafiaBot

logger = logging.getLogger("mafia_master.ui")


async def _send_component_error(
    interaction: discord.Interaction,
    error: Exception,
) -> None:
    logger.error(
        "Discord component interaction failed",
        exc_info=(type(error), error, error.__traceback__),
    )
    try:
        message = "That action hit an error. Check the game status and try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.warning("Could not acknowledge a failed Discord component interaction")


class SafeView(discord.ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await _send_component_error(interaction, error)


class LobbyView(SafeView):
    def __init__(self, bot: MafiaBot, game_id: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.game_id = game_id
        self._button("Join", discord.ButtonStyle.success, "join", self.join)
        self._button("Leave", discord.ButtonStyle.secondary, "leave", self.leave)
        self._button("Add Player", discord.ButtonStyle.primary, "add", self.add_player)
        self._button("Remove Player", discord.ButtonStyle.secondary, "remove", self.remove_player)
        self._button("Start Game", discord.ButtonStyle.success, "start", self.start_game)
        self._button("Cancel Game", discord.ButtonStyle.danger, "cancel", self.cancel_game)

    def _button(self, label, style, action, callback) -> None:
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"mafia:lobby:{self.game_id}:{action}",
        )
        button.callback = callback
        self.add_item(button)

    async def join(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_lobby_membership(interaction, self.game_id, join=True)

    async def leave(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_lobby_membership(interaction, self.game_id, join=False)

    async def add_player(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PlayerEditModal(self.bot, self.game_id, add=True))

    async def remove_player(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PlayerEditModal(self.bot, self.game_id, add=False))

    async def start_game(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_start_game(interaction, self.game_id)

    async def cancel_game(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_cancel_game(interaction, self.game_id)


class StartSetupView(SafeView):
    """Private first step of the /start player-count setup."""

    def __init__(
        self,
        bot: MafiaBot,
        *,
        host_id: str,
        channel_id: str,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.host_id = str(host_id)
        self.channel_id = str(channel_id)
        select = discord.ui.Select(
            placeholder="Choose total players (4–12)",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=f"{count} total players", value=str(count))
                for count in range(4, 13)
            ],
            custom_id=f"mafia:start:total:{self.host_id}:{self.channel_id}",
        )
        select.callback = self.choose_total
        self.add_item(select)

    async def choose_total(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.host_id:
            await interaction.response.send_message(
                "Only the person who ran /start can choose the player counts.",
                ephemeral=True,
            )
            return
        if str(interaction.channel_id) != self.channel_id:
            await interaction.response.send_message(
                "Choose the player count in the channel where you ran /start.",
                ephemeral=True,
            )
            return
        data = interaction.data
        values = data.get("values", []) if data else []
        if not values:
            await interaction.response.send_message("Choose a total player count.", ephemeral=True)
            return
        total_players = int(values[0])
        await interaction.response.edit_message(
            content=(
                f"Total players: **{total_players}**. You count as one human player. "
                "How many humans should join?"
            ),
            view=HumanCountView(
                self.bot,
                host_id=self.host_id,
                channel_id=self.channel_id,
                total_players=total_players,
            ),
        )


class HumanCountView(SafeView):
    """Private second step for selecting the number of human seats."""

    def __init__(
        self,
        bot: MafiaBot,
        *,
        host_id: str,
        channel_id: str,
        total_players: int,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.host_id = str(host_id)
        self.channel_id = str(channel_id)
        self.total_players = total_players
        select = discord.ui.Select(
            placeholder="Choose human players (including you)",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"{count} human player{'s' if count != 1 else ''}",
                    value=str(count),
                )
                for count in range(1, total_players + 1)
            ],
            custom_id=f"mafia:start:humans:{self.host_id}:{self.channel_id}",
        )
        select.callback = self.choose_humans
        self.add_item(select)

    async def choose_humans(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.host_id:
            await interaction.response.send_message(
                "Only the person who ran /start can choose the player counts.",
                ephemeral=True,
            )
            return
        if str(interaction.channel_id) != self.channel_id:
            await interaction.response.send_message(
                "Choose the player count in the channel where you ran /start.",
                ephemeral=True,
            )
            return
        data = interaction.data
        values = data.get("values", []) if data else []
        if not values:
            await interaction.response.send_message("Choose a human player count.", ephemeral=True)
            return
        human_players = int(values[0])
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.create_mixed_lobby(
            interaction,
            host_id=self.host_id,
            channel_id=self.channel_id,
            total_players=self.total_players,
            human_players=human_players,
        )


class PlayerEditModal(discord.ui.Modal):
    def __init__(self, bot: MafiaBot, game_id: str, *, add: bool) -> None:
        super().__init__(
            title="Add a player" if add else "Remove a player",
            custom_id=f"mafia:player:{game_id}:{'add' if add else 'remove'}",
            timeout=300,
        )
        self.bot = bot
        self.game_id = game_id
        self.add_player = add
        self.player_input = discord.ui.TextInput(
            label="Discord mention or user ID",
            placeholder="Mention the player, or paste their Discord user ID",
            min_length=2,
            max_length=40,
            required=True,
        )
        self.add_item(self.player_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.handle_lobby_player_edit(
            interaction,
            self.game_id,
            self.player_input.value,
            add=self.add_player,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await _send_component_error(interaction, error)


class VoteView(SafeView):
    def __init__(self, bot: MafiaBot, game: MafiaGame) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.game_id = game.game_id
        options = [
            discord.SelectOption(
                label=(f"{player.name} (AI)" if player.is_ai else player.name)[:100],
                value=player.user_id,
                description="Living player",
            )
            for player in game.living_players()
        ]
        select = discord.ui.Select(
            placeholder="Cast or change your vote",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"mafia:vote:{game.game_id}",
        )
        select.callback = self.cast_vote
        self.add_item(select)

    async def cast_vote(self, interaction: discord.Interaction) -> None:
        select = interaction.data
        target_id = select["values"][0] if select and "values" in select else ""
        await self.bot.handle_vote(interaction, self.game_id, target_id)


class ActionView(SafeView):
    def __init__(self, bot: MafiaBot, game: MafiaGame, actor_id: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.game_id = game.game_id
        self.actor_id = str(actor_id)
        actor = game.players[self.actor_id]
        if actor.role == RoleKey.MAFIA:
            self.action = "mafia"
            targets = [
                player
                for player in game.living_players()
                if player.role != RoleKey.MAFIA
            ]
            placeholder = "Choose the Mafia's night target"
        elif actor.role == RoleKey.DOCTOR:
            self.action = "doctor"
            targets = [
                player
                for player in game.living_players()
                if player.user_id != self.actor_id or game.rules.doctor_self_protection
            ]
            placeholder = "Choose a player to protect"
        elif actor.role == RoleKey.DETECTIVE:
            self.action = "detective"
            targets = [player for player in game.living_players() if player.user_id != self.actor_id]
            placeholder = "Choose a player to investigate"
        else:
            raise ValueError("This role does not have an interactive night action.")

        options = [
            discord.SelectOption(
                label=(f"{player.name} (AI)" if player.is_ai else player.name)[:100],
                value=player.user_id,
                description="Living player",
            )
            for player in targets
        ]
        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"mafia:action:{game.game_id}:{self.actor_id}:{self.action}",
        )
        select.callback = self.submit
        self.add_item(select)

    async def submit(self, interaction: discord.Interaction) -> None:
        select = interaction.data
        target_id = select["values"][0] if select and "values" in select else ""
        await self.bot.handle_night_action(
            interaction,
            self.game_id,
            self.actor_id,
            self.action,
            target_id,
        )


def resolve_user_id(value: str) -> str | None:
    value = value.strip()
    mention = re.fullmatch(r"<@!?(\d{15,22})>", value)
    if mention:
        return mention.group(1)
    if re.fullmatch(r"\d{15,22}", value):
        return value
    return None


def lobby_view_for(bot: MafiaBot, game: MafiaGame) -> LobbyView | None:
    return LobbyView(bot, game.game_id) if game.phase == Phase.LOBBY else None


def vote_view_for(bot: MafiaBot, game: MafiaGame) -> VoteView | None:
    return VoteView(bot, game) if game.phase == Phase.DAY_VOTING else None


def action_view_for(bot: MafiaBot, game: MafiaGame, actor_id: str) -> ActionView | None:
    actor = game.players.get(str(actor_id))
    if actor is None or not actor.alive:
        return None
    if game.phase == Phase.NIGHT_MAFIA and actor.role == RoleKey.MAFIA:
        return ActionView(bot, game, actor.user_id)
    if game.phase == Phase.NIGHT_ACTIONS and actor.role in {
        RoleKey.DOCTOR,
        RoleKey.DETECTIVE,
    }:
        return ActionView(bot, game, actor.user_id)
    return None
