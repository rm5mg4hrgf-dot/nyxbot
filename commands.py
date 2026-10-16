"""Slash commands for lobby creation, rules, and game status."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ai_players import reply_to_chat
from bot import MafiaBot
from models import GameRules, Phase, RoleKey
from presentation import lobby_embed, player_label, rules_embed
from ui import LobbyView, StartSetupView

logger = logging.getLogger("mafia_master.commands")


def _current_game(bot: MafiaBot, interaction: discord.Interaction):
    if interaction.guild_id is None or interaction.channel_id is None:
        return None
    for game in bot.manager.active_games():
        if game.guild_id == str(interaction.guild_id) and str(interaction.channel_id) in {
            game.parent_channel_id,
            game.play_channel_id,
        }:
            return game
    return None


class MafiaCommands(commands.GroupCog, group_name="mafia", group_description="Host and play Mafia"):
    def __init__(self, bot: MafiaBot) -> None:
        self.bot = bot

    @app_commands.command(name="create", description="Create a Mafia game lobby in this channel")
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="Beginner", value="beginner"),
            app_commands.Choice(name="Classic", value="classic"),
            app_commands.Choice(name="Custom", value="custom"),
        ]
    )
    @app_commands.describe(
        preset="Role preset for this game; omitting it uses this server's saved preset.",
        min_players="Minimum human players, including the host. Remaining seats are AI.",
        max_players="Total players (4 or more); unfilled seats are added as AI.",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        preset: app_commands.Choice[str] | None = None,
        min_players: app_commands.Range[int, 1, 20] | None = None,
        max_players: app_commands.Range[int, 4, 20] | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await self.bot.reply_private(
                interaction,
                "Create the lobby in a server text channel, not a DM or thread.",
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rules = await self.bot.manager.rules_for_guild(str(interaction.guild.id))
        if preset is not None:
            rules.preset = preset.value
        if min_players is not None:
            rules.min_players = min_players
        if max_players is not None:
            rules.max_players = max_players
        try:
            rules.validate()
            rules.role_counts_for(rules.max_players)
            game = await self.bot.manager.new_lobby(
                guild_id=str(interaction.guild.id),
                channel_id=str(interaction.channel.id),
                host_id=str(interaction.user.id),
                host_name=interaction.user.display_name,
                rules=rules,
                required_humans=rules.min_players,
                ai_fill_to_max=True,
            )
        except ValueError as exc:
            await self.bot.reply_private(interaction, str(exc))
            return
        view = LobbyView(self.bot, game.game_id)
        try:
            message = await interaction.channel.send(embed=lobby_embed(game), view=view)
            async with self.bot.manager.edit(game.game_id) as current:
                current.lobby_message_id = str(message.id)
        except (discord.Forbidden, discord.HTTPException):
            async with self.bot.manager.edit(game.game_id) as current:
                self.bot.engine.cancel_game(current)
            await self.bot.manager.retire(game.game_id)
            await self.bot.reply_private(
                interaction,
                "I could not post the lobby. Check that I can view and send messages in this channel.",
            )
            return
        await self.bot.reply_private(interaction, "Your lobby is open.", ephemeral=True)

    @app_commands.command(name="configure", description="Set the default rules for future lobbies")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="Beginner", value="beginner"),
            app_commands.Choice(name="Classic", value="classic"),
            app_commands.Choice(name="Custom", value="custom"),
        ],
        vote_tie_rule=[
            app_commands.Choice(name="Nobody is eliminated", value="no_elimination"),
            app_commands.Choice(name="Choose randomly among the tied players", value="random"),
        ],
        mafia_tie_rule=[
            app_commands.Choice(name="No night target", value="no_kill"),
            app_commands.Choice(name="Choose randomly among tied targets", value="random"),
        ],
    )
    @app_commands.describe(
        preset="Beginner, Classic, or Custom role distribution; any supplied role count switches to Custom.",
        min_players="Minimum number of human players for future games.",
        max_players="Maximum total players for future games.",
        mafia_count="Mafia members in Custom games.",
        doctor_count="Doctors in Custom games; 0 disables the Doctor.",
        detective_count="Detectives in Custom games; 0 disables the Detective.",
        joker_count="Jokers in Custom games; 0 disables the Joker.",
        discussion_seconds="Day discussion duration, 15–3600 seconds.",
        voting_seconds="Day vote duration, 15–3600 seconds.",
        night_seconds="Each night-action stage duration, 15–1800 seconds.",
        doctor_self_protection="Allow the Doctor to protect themself.",
        detective_sees_joker_as_mafia="Make the Joker appear Mafia to Detective investigations.",
        reveal_roles="Reveal a player's role after daytime elimination.",
        anonymous_votes="Show only the tally, not who cast each daytime vote.",
        spectators_can_see="Use the channel for public events; off creates a players-only game thread.",
        ai_storytelling="Generate mood-only morning stories with OpenAI; off uses built-in stories.",
        vote_tie_rule="How to handle a tied daytime vote.",
        mafia_tie_rule="How Mafia target-vote ties are resolved.",
    )
    async def configure(
        self,
        interaction: discord.Interaction,
        preset: app_commands.Choice[str] | None = None,
        min_players: app_commands.Range[int, 1, 20] | None = None,
        max_players: app_commands.Range[int, 4, 20] | None = None,
        mafia_count: app_commands.Range[int, 1, 8] | None = None,
        doctor_count: app_commands.Range[int, 0, 5] | None = None,
        detective_count: app_commands.Range[int, 0, 5] | None = None,
        joker_count: app_commands.Range[int, 0, 5] | None = None,
        discussion_seconds: app_commands.Range[int, 15, 3600] | None = None,
        voting_seconds: app_commands.Range[int, 15, 3600] | None = None,
        night_seconds: app_commands.Range[int, 15, 1800] | None = None,
        doctor_self_protection: bool | None = None,
        detective_sees_joker_as_mafia: bool | None = None,
        reveal_roles: bool | None = None,
        anonymous_votes: bool | None = None,
        spectators_can_see: bool | None = None,
        ai_storytelling: bool | None = None,
        vote_tie_rule: app_commands.Choice[str] | None = None,
        mafia_tie_rule: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await self.bot.reply_private(interaction, "Server rules can only be changed in a server.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rules = await self.bot.manager.rules_for_guild(str(interaction.guild_id))
        if preset is not None:
            rules.preset = preset.value
        if min_players is not None:
            rules.min_players = min_players
        if max_players is not None:
            rules.max_players = max_players
        role_values = {
            RoleKey.MAFIA: mafia_count,
            RoleKey.DOCTOR: doctor_count,
            RoleKey.DETECTIVE: detective_count,
            RoleKey.JOKER: joker_count,
        }
        role_changed = False
        for role, count in role_values.items():
            if count is not None:
                rules.custom_roles[role] = count
                role_changed = True
        if role_changed:
            rules.preset = "custom"
        if discussion_seconds is not None:
            rules.discussion_seconds = discussion_seconds
        if voting_seconds is not None:
            rules.voting_seconds = voting_seconds
        if night_seconds is not None:
            rules.night_seconds = night_seconds
        if doctor_self_protection is not None:
            rules.doctor_self_protection = doctor_self_protection
        if detective_sees_joker_as_mafia is not None:
            rules.detective_sees_joker_as_mafia = detective_sees_joker_as_mafia
        if reveal_roles is not None:
            rules.reveal_roles = reveal_roles
        if anonymous_votes is not None:
            rules.anonymous_votes = anonymous_votes
        if spectators_can_see is not None:
            rules.spectators_can_see = spectators_can_see
        if ai_storytelling is not None:
            rules.ai_storytelling_enabled = ai_storytelling
        if vote_tie_rule is not None:
            rules.vote_tie_rule = vote_tie_rule.value
        if mafia_tie_rule is not None:
            rules.mafia_tie_rule = mafia_tie_rule.value

        try:
            rules.validate()
            await self.bot.manager.save_rules(str(interaction.guild_id), rules)
        except ValueError as exc:
            await self.bot.reply_private(interaction, str(exc))
            return
        await interaction.followup.send(
            embed=rules_embed(rules),
            ephemeral=True,
        )

    @app_commands.command(name="rules", description="Show the rules that will be used for a game")
    async def rules(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        game = _current_game(self.bot, interaction)
        if game is not None:
            rules = game.rules
            note = "These are the rules for the active game."
        elif interaction.guild_id is not None:
            rules = await self.bot.manager.rules_for_guild(str(interaction.guild_id))
            note = "These are the server defaults for future games."
        else:
            await self.bot.reply_private(interaction, "Rules are available inside a server.")
            return
        embed = rules_embed(rules)
        embed.set_footer(text=note)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="Show the public status of this channel's game")
    async def status(self, interaction: discord.Interaction) -> None:
        game = _current_game(self.bot, interaction)
        if game is None:
            await self.bot.reply_private(interaction, "There is no active Mafia game in this channel.")
            return
        phase_label = game.phase.value.replace("_", " ").title()
        living = [player_label(player) for player in game.living_players()]
        embed = discord.Embed(
            title="Mafia Master · Game Status",
            description=f"**Phase:** {phase_label}\n**Host:** <@{game.host_id}>",
            color=0x416A8C,
        )
    
        if game.deadline is not None:
            embed.add_field(name="Phase ends", value=f"<t:{int(game.deadline)}:R>", inline=True)
        embed.add_field(
            name=f"Living players ({len(living)})",
            value="\n".join(living) or "None",
            inline=False,
        )
        embed.set_footer(text=f"Game ID · {game.game_id[:8]} · Private roles are not shown.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="cancel",
        description="Cancel the current Mafia lobby or active game",
    )
    async def cancel(self, interaction: discord.Interaction) -> None:
        game = _current_game(self.bot, interaction)

        if game is None:
            await self.bot.reply_private(
                interaction,
                "There is no active Mafia game in this channel.",
            )
            return

        await self.bot.handle_cancel_any_game(
            interaction, game.game_id
        )

    @app_commands.command(
        name="opinion",
        description="Share a Mafia theory and get an AI player's reaction",
    )
    @app_commands.describe(opinion="Your theory, suspicion, defense, or clue (up to 800 characters).")
    async def opinion(self, interaction: discord.Interaction, opinion: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        opinion = opinion.strip()
        if not opinion or len(opinion) > 800:
            await interaction.followup.send(
                "Write a game opinion between 1 and 800 characters.",
                ephemeral=True,
            )
            return
        game = _current_game(self.bot, interaction)
        if game is None or game.phase != Phase.DAY_DISCUSSION:
            await interaction.followup.send(
                "AI opinions are available in the active game's discussion phase.",
                ephemeral=True,
            )
            return
        human = game.players.get(str(interaction.user.id))
        if human is None or human.is_ai or not human.alive:
            await interaction.followup.send(
                "Only living human players can ask an AI to react during discussion.",
                ephemeral=True,
            )
            return
        ai_players = [player for player in game.living_players() if player.is_ai]
        if not ai_players:
            await interaction.followup.send(
                "This game has no living AI players to respond.",
                ephemeral=True,
            )
            return
        normalized = opinion.casefold()
        actor = next(
            (player for player in ai_players if player.name.casefold() in normalized),
            self.bot.engine.rng.choice(ai_players),
        )
        reply = reply_to_chat(actor, opinion, self.bot.engine.rng, game=game)
        channel = await self.bot.fetch_game_channel(game)
        if channel is None:
            await interaction.followup.send(
                "I could not reach this game's discussion channel.",
                ephemeral=True,
            )
            return
        try:
            await channel.send(
                f"**{discord.utils.escape_mentions(discord.utils.escape_markdown(actor.name))} · AI:** "
                f"{discord.utils.escape_mentions(discord.utils.escape_markdown(reply))}"
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not post the AI reply. Check the bot's permission to send messages in the game channel.",
                ephemeral=True,
            )
            return
        await interaction.followup.send("Your opinion was shared with the AI players.", ephemeral=True)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        interaction.extras["mafia_error_handled"] = True
        if isinstance(error, app_commands.errors.MissingPermissions):
            message = "You need the Manage Server permission to change game defaults."
        else:
            original = getattr(error, "original", error)
            logger.error(
                "A Mafia Master slash command failed",
                exc_info=(
                    type(original),
                    original,
                    original.__traceback__,
                ),
            )
            message = "The command could not be completed. Check the bot's channel permissions and try again."
        await self.bot.reply_private(interaction, message)


class StartCommand(commands.Cog):
    """Root /start command for configurable mixed human/AI games."""

    def __init__(self, bot: MafiaBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="start",
        description="Set player counts and open a mixed human/AI Mafia lobby",
    )
    async def start(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await self.bot.reply_private(
                interaction,
                "Run /start in a server text channel, not a DM or thread.",
            )
            return
        existing = self.bot.manager.active_in_channel(
            str(interaction.guild.id),
            str(interaction.channel.id),
        )
        if existing is not None:
            await self.bot.reply_private(
                interaction,
                "This channel already has an active Mafia game. Finish or cancel it before opening another.",
            )
            return
        await interaction.response.send_message(
            "Choose the total number of players for your game.",
            view=StartSetupView(
                self.bot,
                host_id=str(interaction.user.id),
                channel_id=str(interaction.channel.id),
            ),
            ephemeral=True,
        )
