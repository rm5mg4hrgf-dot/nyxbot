"""Discord embeds and public-safe game summaries."""

from __future__ import annotations

import discord

from models import DEFAULT_ROLE_CATALOG, MafiaGame, Phase, RoleKey


INK = 0x20252E
GOLD = 0xD6A84F
GREEN = 0x347A5B
RED = 0x8D3845
BLUE = 0x416A8C

# Funny reaction GIFs shown on public game embeds.
SHOCKED_GIF = "https://media.tenor.com/FlqWC5OpKf8AAAAd/surprised-cat.gif"
LAUGHING_GIF = "https://media.tenor.com/QQNBxAeAvy4AAAAd/laughing-reaction.gif"
CONFUSED_GIF = "https://media.tenor.com/IklKDJCAZHgAAAAd/confused-cat.gif"
POPCORN_GIF = "https://media.tenor.com/AAk-e9RN9sEAAAAd/enjoying-the-show-popcorn.gif"
FACEPALM_GIF = "https://media.tenor.com/y6-0O8TczlMAAAAd/funny-reaction.gif"
DANCE_GIF = "https://media.tenor.com/DVnUx37BK-0AAAAd/cat-dancing.gif"


def _attach_gif(embed: discord.Embed, gif_url: str) -> discord.Embed:
    """Set a reaction GIF as the embed's main image."""
    embed.set_image(url=gif_url)
    return embed


def player_label(player) -> str:
    if player.is_ai:
        name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(player.name)
        )
        return f"{name} (AI)"
    return f"<@{player.user_id}>"


def _mentions(players: list, *, alive_only: bool = False) -> str:
    selected = [player for player in players if player.alive or not alive_only]
    return "\n".join(f"• {player_label(player)}" for player in selected) or "None yet"


def lobby_embed(game: MafiaGame) -> discord.Embed:
    embed = discord.Embed(
        title="Mafia Master · Open Lobby",
        description=(
            "Join the table before the host starts. Roles are assigned privately, "
            "and every human player must be able to receive direct messages."
        ),
        color=GOLD,
    )
    if game.required_humans is None:
        player_count = (
            f"**{len(game.players)} / {game.rules.max_players}** joined\n"
            f"Minimum to start: {game.rules.min_players}"
        )
    elif game.ai_fill_to_max and not game.exact_human_count:
        humans_joined = len(game.human_players())
        ai_count = game.rules.max_players - humans_joined
        player_count = (
            f"**{humans_joined} humans joined** · minimum {game.required_humans}\n"
            f"{game.rules.max_players} total · {ai_count} AI will fill open seats when started"
        )
    else:
        ai_count = game.rules.max_players - game.required_humans
        player_count = (
            f"**{len(game.human_players())} / {game.required_humans}** humans joined\n"
            f"{game.rules.max_players} total · {ai_count} AI added when the host starts"
        )
    embed.add_field(name="Players", value=player_count, inline=True)
    embed.add_field(name="Host", value=f"<@{game.host_id}>", inline=True)
    embed.add_field(
        name="Rules",
        value=f"{game.rules.preset.title()} preset · "
        f"{'Public' if game.rules.spectators_can_see else 'Players-only'} events",
        inline=True,
    )
    lobby_players = (
        game.human_players() if game.required_humans is not None else list(game.players.values())
    )
    embed.add_field(name=f"Joined ({len(lobby_players)})", value=_mentions(lobby_players), inline=False)
    embed.set_footer(text=f"Game ID · {game.game_id[:8]}")
    return embed


def rules_embed(rules) -> discord.Embed:
    return discord.Embed(
        title="Mafia Master · Server Rules",
        description=rules.summary(),
        color=BLUE,
    )


def discussion_embed(game: MafiaGame, *, opening: bool = False) -> discord.Embed:
    alive = [player for player in game.players.values() if player.alive]
    title = "Day 1 · Discussion" if opening else f"Day {game.day_number} · Morning"
    description = (
        "The game is underway. Discuss what you know; do not post private role messages."
        if opening
        else "The town gathers again. Discuss the night, then vote when the timer ends."
    )
    embed = discord.Embed(title=title, description=description, color=GREEN)
    embed.add_field(name="Living players", value=_mentions(alive), inline=False)
    embed.add_field(
        name="Discussion closes",
        value=f"<t:{int(game.deadline or 0)}:R>",
        inline=True,
    )
    embed.set_footer(text=f"Day {game.day_number}")
    return _attach_gif(embed, DANCE_GIF if opening else CONFUSED_GIF)


def vote_embed(game: MafiaGame) -> discord.Embed:
    alive = [player for player in game.players.values() if player.alive]
    valid_votes = {
        voter_id: target_id
        for voter_id, target_id in game.votes.items()
        if voter_id in game.players
        and game.players[voter_id].alive
        and target_id in game.players
        and game.players[target_id].alive
    }
    embed = discord.Embed(
        title=f"Day {game.day_number} · Town Vote",
        description=(
            "Choose a living player. You can change your vote until the timer ends."
            if game.rules.anonymous_votes
            else "Votes are public. Choose a living player; you can change your vote until the timer ends."
        ),
        color=BLUE,
    )
    embed.add_field(
        name="Ballots",
        value=f"{len(valid_votes)} of {len(alive)} living players have voted.",
        inline=True,
    )
    embed.add_field(name="Voting closes", value=f"<t:{int(game.deadline or 0)}:R>", inline=True)

    if game.rules.anonymous_votes:
        counts: dict[str, int] = {}
        for target in valid_votes.values():
            counts[target] = counts.get(target, 0) + 1
        details = "\n".join(
            f"• {player_label(game.players[target_id])} — {count} vote{'s' if count != 1 else ''}"
            for target_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        )
        embed.add_field(name="Anonymous tally", value=details or "No votes yet.", inline=False)
    else:
        details = "\n".join(
            f"• {player_label(game.players[voter_id])} → {player_label(game.players[target_id])}"
            for voter_id, target_id in sorted(valid_votes.items())
        )
        embed.add_field(name="Submitted votes", value=details or "No votes yet.", inline=False)
    embed.set_footer(text=f"Game ID · {game.game_id[:8]}")
    return _attach_gif(embed, POPCORN_GIF)


def morning_embed(
    game: MafiaGame,
    story: str,
    *,
    outcome_text: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Day {game.day_number} · Morning",
        description=outcome_text or story,
        color=GREEN,
    )
    if outcome_text is not None:
        embed.add_field(name="Morning story", value=story, inline=False)
    embed.add_field(
        name="Living players",
        value=_mentions([player for player in game.players.values() if player.alive]),
        inline=False,
    )
    embed.add_field(
        name="Discussion closes",
        value=f"<t:{int(game.deadline or 0)}:R>",
        inline=True,
    )
    return _attach_gif(embed, SHOCKED_GIF if outcome_text else CONFUSED_GIF)


def elimination_embed(game: MafiaGame, eliminated_id: str, revealed_role: str | None) -> discord.Embed:
    player = game.players[eliminated_id]
    description = f"{player_label(player)} was eliminated by the town vote."
    if revealed_role:
        description += f"\nTheir role was **{DEFAULT_ROLE_CATALOG.get(revealed_role).name}**."
    embed = discord.Embed(
        title=f"Day {game.day_number} · Vote Result",
        description=description,
        color=RED,
    )
    embed.set_footer(text=f"{player.name} is out of the game.")
    return _attach_gif(embed, FACEPALM_GIF)


def no_elimination_embed(game: MafiaGame, tied_ids: tuple[str, ...]) -> discord.Embed:
    if tied_ids:
        details = "The vote ended in a tie"
        if game.rules.vote_tie_rule == "random":
            details += "."
        else:
            details += "; nobody was eliminated."
    else:
        details = "No valid votes were cast; nobody was eliminated."
    embed = discord.Embed(
        title=f"Day {game.day_number} · No Elimination",
        description=details,
        color=GOLD,
    )
    return _attach_gif(embed, LAUGHING_GIF)


def final_embed(game: MafiaGame) -> discord.Embed:
    embed = discord.Embed(
        title=f"Game Over · {game.winner or 'No winner'} wins",
        description="The game has ended. All roles are now revealed.",
        color=GOLD,
    )
    roles: dict[str, list[str]] = {}
    for player in game.players.values():
        if player.role is None:
            role_name = "Unassigned"
        else:
            role_name = DEFAULT_ROLE_CATALOG.get(player.role).name
        state = "living" if player.alive else "eliminated"
        roles.setdefault(role_name, []).append(f"{player_label(player)} — {state}")
    role_lines = []
    for role_name, players in roles.items():
        role_lines.append(f"**{role_name}**\n" + "\n".join(players))
    embed.add_field(name="Role reveal", value="\n\n".join(role_lines)[:1024], inline=False)
    embed.add_field(
        name="Game statistics",
        value=(
            f"Players: {len(game.players)}\n"
            f"Night phases: {game.round_number}\n"
            f"Daytime eliminations: {game.day_vote_count}\n"
            f"Final living players: {sum(player.alive for player in game.players.values())}"
        ),
        inline=True,
    )
    history = [event.public_text for event in game.history if event.kind != "game_end"]
    timeline = "\n".join(f"• {line}" for line in history[-10:])
    embed.add_field(name="Public event history", value=timeline[:1024] or "No events recorded.", inline=False)
    embed.set_footer(text="Private night actions, investigation results, and hidden votes are never included.")
    return _attach_gif(embed, DANCE_GIF)


def role_embed(game: MafiaGame, player_id: str) -> discord.Embed:
    player = game.players[player_id]
    definition = DEFAULT_ROLE_CATALOG.get(player.role or "townspeople")
    embed = discord.Embed(
        title=f"Your role · {definition.name}",
        description=definition.description,
        color=RED if definition.team == "mafia" else GOLD if definition.team == "neutral" else GREEN,
    )
    if player.role == RoleKey.MAFIA:
        teammates = [
            other for other in game.players_with_role(RoleKey.MAFIA) if other.user_id != player_id
        ]
        embed.add_field(
            name="Mafia teammates",
            value=_mentions(teammates) if teammates else "You are the only Mafia member.",
            inline=False,
        )
        if game.mafia_thread_id:
            embed.add_field(
                name="Private coordination",
                value=f"<#{game.mafia_thread_id}>",
                inline=False,
            )
    if definition.night_action:
        embed.add_field(name="Available action", value=definition.night_action.replace("_", " ").title(), inline=False)
    else:
        embed.add_field(name="Available action", value="No night action.", inline=False)
    embed.set_footer(text="Keep this role private. The bot will send night actions to this DM.")
    return embed
