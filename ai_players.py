"""Offline, role-aware player agents for mixed human/AI games."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Protocol

from models import MafiaGame, Player, RoleKey


class ChoiceSource(Protocol):
    def choice(self, values: list): ...
    def shuffle(self, values: list) -> None: ...


@dataclass(frozen=True)
class AIPersonality:
    key: str
    name: str
    style: str
    vote_style: str
    discussion_lines: tuple[str, ...]
    reply_lines: tuple[str, ...]


PERSONALITIES = (
    AIPersonality(
        "careful", "Mira", "careful and observant", "follow",
        ("Let's compare what actually happened before we settle on anyone.",
         "I would rather hear a reason than rush into a guess."),
        ("That's worth checking. What public clue points that way?",
         "Could be, but let's give them a chance to explain first."),
    ),
    AIPersonality(
        "analytical", "Theo", "analytical and measured", "follow",
        ("I am keeping track of the public timeline. Small details may matter.",
         "Let's separate what we know from what we are assuming."),
        ("Good question. I would compare that with the public timeline.",
         "That is one possibility; what evidence can we verify?"),
    ),
    AIPersonality(
        "curious", "Juniper", "curious and conversational", "random",
        ("I want to hear everyone's reasoning before I make up my mind.",
         "What changed for people between the last vote and now?"),
        ("I hear you. What made that stand out to you?",
         "Interesting. Has anyone else noticed the same thing?"),
    ),
    AIPersonality(
        "direct", "Rafi", "direct but fair", "follow",
        ("Let's ask for clear reasons before we vote.",
         "I am ready to decide once we have compared the clues."),
        ("That sounds worth following up on. What is the strongest clue?",
         "I would ask them directly before treating that as settled."),
    ),
    AIPersonality(
        "skeptical", "Cleo", "skeptical and independent", "contrarian",
        ("I am not convinced by the first explanation. Let's test it.",
         "A popular guess can still be wrong, so I want a second reason."),
        ("Maybe. I would like one more public clue before agreeing.",
         "Let's not let one guess become a fact without checking it."),
    ),
    AIPersonality(
        "warm", "Samira", "warm and encouraging", "random",
        ("Let's give everyone room to explain before the vote.",
         "We can disagree and still keep the discussion fair."),
        ("I hear your point. Let's see whether anyone can add context.",
         "That could matter. I would keep the discussion respectful."),
    ),
    AIPersonality(
        "bold", "Dax", "bold and energetic", "follow",
        ("We have to make a choice eventually. What is our best clue?",
         "I would rather test a theory than sit on guesses all day."),
        ("That is a lead. Let's see if the timeline backs it up.",
         "I am listening. What would change your mind about it?"),
    ),
    AIPersonality(
        "patient", "Nora", "patient and thoughtful", "contrarian",
        ("There is time to think this through. Let's not rush the vote.",
         "I want to hear from someone who has not spoken yet."),
        ("Fair question. I would wait for one more perspective.",
         "Let's check what is confirmed before drawing a conclusion."),
    ),
    AIPersonality(
        "playful", "Felix", "lighthearted but focused", "random",
        ("No wild guesses from me yet; let's untangle the timeline first.",
         "The town's detective work could use a little more evidence."),
        ("Good point. Let's keep the theories attached to actual clues.",
         "I am not ready to call it, but I want to hear more."),
    ),
    AIPersonality(
        "decisive", "Asha", "decisive and practical", "follow",
        ("Let's focus on the choices people can explain in public.",
         "I would like us to leave discussion with one reasoned vote."),
        ("That is useful. What is the clearest public reason for it?",
         "I can consider that if the rest of the timeline fits."),
    ),
    AIPersonality(
        "contrarian", "Kit", "independent and questioning", "contrarian",
        ("I am checking whether the crowd is following evidence or momentum.",
         "Before we pile on, let's hear a different explanation."),
        ("Maybe, but a lot of agreement is not proof by itself.",
         "I would hear another side before making that my vote."),
    ),
    AIPersonality(
        "steady", "Rowan", "steady and cooperative", "random",
        ("Let's keep notes on the public events and work it out together.",
         "I am open to changing my mind if the clues point somewhere else."),
        ("I see why you are asking. Let's compare notes before deciding.",
         "That is possible. What else happened that supports it?"),
    ),
    AIPersonality(
        "quiet", "Inez", "quiet and attentive", "contrarian",
        ("I have been listening. What is the strongest clue so far?",
         "I would like to hear from someone who has not had a turn yet."),
        ("I was listening to that. What part should we check first?",
         "Let's leave room for another explanation before voting."),
    ),
    AIPersonality(
        "optimistic", "Omar", "optimistic and team-minded", "random",
        ("We can figure this out if we keep the discussion focused.",
         "Let's hear a couple of perspectives and work toward a fair vote."),
        ("I think we can sort it out. What do we know for sure?",
         "That may help. Let's keep comparing notes."),
    ),
    AIPersonality(
        "witty", "Leo", "witty and observant", "contrarian",
        ("I am trying to solve the puzzle, not just follow the loudest theory.",
         "Let's put the clues on the table before we point fingers."),
        ("That is an interesting theory. Does the public timeline agree?",
         "Could be, but I would not call the case closed yet."),
    ),
    AIPersonality(
        "methodical", "Bea", "methodical and precise", "follow",
        ("Let's go one step at a time and check the public events.",
         "I want a clear timeline before I decide where my vote goes."),
        ("Let's check that against what happened publicly.",
         "That is helpful. What is the next clue in the timeline?"),
    ),
    AIPersonality(
        "independent", "Kim", "independent and calm", "contrarian",
        ("I am forming my own view, but I want to hear the evidence first.",
         "Let's not confuse a confident guess with a confirmed clue."),
        ("I will think about that. What can we verify?",
         "I would like to hear a reason beyond the number of people agreeing."),
    ),
    AIPersonality(
        "empathetic", "Talia", "empathetic and fair-minded", "random",
        ("Let's keep the debate about choices and clues, not personal attacks.",
         "We can challenge an idea without making the discussion unkind."),
        ("I understand the concern. Let's ask for their side too.",
         "That is worth discussing, as long as we keep it respectful."),
    ),
    AIPersonality(
        "confident", "Soren", "confident and inquisitive", "follow",
        ("I have a theory, but I want to test it against the public clues.",
         "Let's ask direct questions and see whether the explanations hold up."),
        ("That is a strong claim. Which public clue supports it?",
         "I am open to it if the timeline backs it up."),
    ),
    AIPersonality(
        "easygoing", "Piper", "easygoing and curious", "random",
        ("Let's take a breath and see what everyone noticed.",
         "I am happy to listen before I pick a side."),
        ("I see what you mean. What did the rest of the group notice?",
         "That could be right. Let's check it against the other clues."),
    ),
)

_PERSONALITY_BY_KEY = {personality.key: personality for personality in PERSONALITIES}


def validate_player_counts(total_players: int, human_players: int) -> None:
    if not 4 <= total_players <= 20:
        raise ValueError("Choose between 4 and 20 total players.")
    if not 1 <= human_players <= total_players:
        raise ValueError("Human players must be between 1 and the total player count.")


def build_ai_players(
    game_id: str,
    total_players: int,
    human_players: int,
    *,
    existing_names: tuple[str, ...] | list[str] = (),
    rng: ChoiceSource | None = None,
) -> list[Player]:
    """Build exactly the requested AI seats with unique names and personalities."""
    validate_player_counts(total_players, human_players)
    ai_count = total_players - human_players
    profiles = list(PERSONALITIES)
    (rng or random.SystemRandom()).shuffle(profiles)
    occupied = {name.casefold() for name in existing_names}
    players: list[Player] = []
    for index in range(ai_count):
        profile = profiles[index % len(profiles)]
        name = profile.name
        suffix = 2
        while name.casefold() in occupied:
            name = f"{profile.name} {suffix}"
            suffix += 1
        occupied.add(name.casefold())
        user_id = f"ai:{game_id}:{index}"
        players.append(
            Player(
                user_id=user_id,
                name=name,
                is_ai=True,
                personality=profile.key,
            )
        )
    return players


def personality_for(player: Player) -> AIPersonality:
    return _PERSONALITY_BY_KEY.get(player.personality or "", PERSONALITIES[0])


def choose_day_vote(
    game: MafiaGame,
    voter: Player,
    rng: ChoiceSource,
) -> str:
    living = [player for player in game.living_players() if player.user_id != voter.user_id]
    if not living:
        return voter.user_id

    # The Joker's public objective is to be eliminated by a daytime vote.
    if voter.role == RoleKey.JOKER:
        return voter.user_id

    if voter.role == RoleKey.MAFIA:
        living = [player for player in living if player.role != RoleKey.MAFIA]
    elif voter.role == RoleKey.DETECTIVE:
        result = game.investigation_results.get(voter.user_id)
        investigated_id = game.detective_targets.get(
            voter.user_id,
            game.detective_target,
        )
        if result is True and investigated_id in {player.user_id for player in living}:
            return investigated_id
        if result is False:
            living = [player for player in living if player.user_id != investigated_id]

    if not living:
        living = [player for player in game.living_players() if player.user_id != voter.user_id]
    if not living:
        return voter.user_id

    # AI can only use ballots as evidence when the game rules make them public.
    visible_votes = game.votes if not game.rules.anonymous_votes else {}
    incoming = {
        player.user_id: sum(1 for target in visible_votes.values() if target == player.user_id)
        for player in living
    }
    profile = personality_for(voter)
    if any(incoming.values()) and profile.vote_style == "follow":
        highest = max(incoming.values())
        candidates = [player for player in living if incoming[player.user_id] == highest]
    elif any(incoming.values()) and profile.vote_style == "contrarian":
        lowest = min(incoming.values())
        candidates = [player for player in living if incoming[player.user_id] == lowest]
    else:
        candidates = living
    return rng.choice(candidates).user_id


def choose_mafia_target(game: MafiaGame, actor: Player, rng: ChoiceSource) -> str | None:
    teammates = [player for player in game.players_with_role(RoleKey.MAFIA)]
    if actor not in teammates:
        return None
    existing_targets = [
        target for teammate_id, target in game.mafia_votes.items()
        if teammate_id in {player.user_id for player in teammates}
    ]
    if existing_targets:
        counts = {target: existing_targets.count(target) for target in set(existing_targets)}
        leaders = [target for target, count in counts.items() if count == max(counts.values())]
        return rng.choice(leaders)
    candidates = [
        player for player in game.living_players()
        if player.role != RoleKey.MAFIA
    ]
    return rng.choice(candidates).user_id if candidates else None


def choose_doctor_target(game: MafiaGame, actor: Player, rng: ChoiceSource) -> str | None:
    candidates = [
        player for player in game.living_players()
        if game.rules.doctor_self_protection or player.user_id != actor.user_id
    ]
    if not candidates:
        return None
    human_targets = [player for player in candidates if not player.is_ai]
    if human_targets and len(candidates) > 1:
        return rng.choice(human_targets).user_id
    return rng.choice(candidates).user_id


def choose_detective_target(game: MafiaGame, actor: Player, rng: ChoiceSource) -> str | None:
    investigated_before = {
        action["target_id"]
        for action in game.private_action_history
        if action.get("actor_id") == actor.user_id
        and action.get("action") == "investigate"
        and action.get("target_id")
    }
    candidates = [
        player for player in game.living_players()
        if player.user_id != actor.user_id and player.user_id not in investigated_before
    ]
    if not candidates:
        candidates = [
            player for player in game.living_players()
            if player.user_id != actor.user_id
        ]
    return rng.choice(candidates).user_id if candidates else None


def discussion_line(player: Player, day_number: int, rng: ChoiceSource) -> str:
    profile = personality_for(player)
    # These lines are intentionally role-blind: public speech never exposes
    # secret roles, investigations, or night actions.
    if player.role == RoleKey.JOKER:
        options = (
            "I am not sure I have much to add, so the group can make the call.",
            "If everyone needs a simple vote, I will not make a fuss about it.",
            "The vibe check is inconclusive, so I am staying out of the spotlight.",
            "I have a theory, but it is still in draft mode. What are your receipts?",
        )
    else:
        day_label = "one" if day_number == 1 else str(day_number)
        options = profile.discussion_lines + (
            "The town chat is in its theory era. I need receipts before I lock in a vote.",
            "Plot-twist energy is high, but public clues still matter more than vibes.",
            "My read is a draft, not a verdict. What detail backs up your theory?",
            "Quick vibe check: can we name one clue, not just one suspect?",
            f"Day {day_label}: keep the theories bold and the facts louder.",
        )
    return rng.choice(list(options))


_OPINION_MARKERS = (
    "i think", "i feel", "my read", "my take", "i suspect", "suspect",
    "suspicious", "sus ", "my vote", "i vote", "vote for", "vote",
    "agree", "disagree", "trust", "don't trust", "dont trust", "not trust",
    "because", "evidence", "clue", "receipts", "timeline", "innocent",
    "guilty", "defend", "defending", "maybe", "not sure", "town", "mafia",
)


def is_opinion_message(message_text: str) -> bool:
    """Recognize game opinions so replies do not require a question mark."""
    normalized = " ".join(message_text.casefold().replace("’", "'").split())
    return (
        any(marker in normalized for marker in _OPINION_MARKERS)
        or re.search(r"\bsus\b", normalized) is not None
    )


def _named_public_player(
    game: MafiaGame | None,
    actor: Player,
    message_text: str,
) -> Player | None:
    if game is None:
        return None
    candidates = sorted(
        (
            player for player in game.living_players()
            if player.user_id != actor.user_id
        ),
        key=lambda player: len(player.name),
        reverse=True,
    )
    for candidate in candidates:
        name = candidate.name.strip()
        if name and re.search(
            rf"(?<!\w){re.escape(name)}(?!\w)",
            message_text,
            re.IGNORECASE,
        ):
            return candidate
    return None


_PERSONALITY_CLOSERS = {
    "careful": "I want the timeline before a verdict.",
    "analytical": "I am checking that against the public vote record.",
    "curious": "Which detail changed your mind?",
    "direct": "Give me the clearest clue.",
    "skeptical": "A vibe is not proof yet.",
    "warm": "Let's keep the discussion fair.",
    "bold": "Bring the receipts.",
    "patient": "No rush; let's compare notes.",
    "playful": "Plot twist, maybe—but show me the clue.",
    "decisive": "Let's make the next vote count.",
    "contrarian": "I will test the other explanation too.",
    "steady": "I am staying open to new evidence.",
    "quiet": "I am listening for a public detail.",
    "optimistic": "We can solve it with the clues we have.",
    "witty": "I need better receipts than a plot twist.",
    "methodical": "First, check the public timeline.",
    "independent": "I want a reason I can verify.",
    "empathetic": "Let's hear them out, then decide.",
    "confident": "Show me what would change your mind.",
}

_MAFIA_SELF_DEFENSES = {
    "careful": "I hear the suspicion, but let's check the full public timeline before deciding.",
    "analytical": "Let's compare that claim with the public vote record before calling it a fact.",
    "curious": "Wait, what public clue actually put me at the top of your list?",
    "direct": "If you're accusing me, name the strongest public clue and I'll answer.",
    "skeptical": "That is a theory, not proof. What can we actually verify about me?",
    "warm": "I hear you, but let's keep it fair—what clue points at me?",
    "bold": "That's a bold call. Bring the receipts before we make it the vote.",
    "patient": "Let's not rush to vote me out; what evidence do we have so far?",
    "playful": "Plot twist: I'm the suspect? Okay, show me the receipts.",
    "decisive": "Let's be specific: which public vote or clue is your reason?",
    "contrarian": "Maybe—but let's pressure-test the theory instead of following the crowd.",
    "steady": "I get the concern. Let's compare it with the events we all saw.",
    "quiet": "I hear my name in the theory. Which public detail should I answer?",
    "optimistic": "We can sort this fairly—what public clue points to me?",
    "witty": "The plot says I'm sus; the receipts still have to show why.",
    "methodical": "Step one: name the public timeline detail that led you to me.",
    "independent": "I won't follow a pile-on without evidence; what's your clue?",
    "empathetic": "I hear the concern. Let's talk through the public clues calmly.",
    "confident": "I'm not convinced by that read. What evidence backs it up?",
}


def _voice_reply(player: Player, text: str) -> str:
    closer = _PERSONALITY_CLOSERS.get(personality_for(player).key)
    if not closer or closer in text:
        return text
    return f"{text} {closer}"


def reply_to_chat(
    player: Player,
    message_text: str,
    rng: ChoiceSource,
    *,
    game: MafiaGame | None = None,
) -> str:
    """Return a role-blind, context-aware reply; chat stays local."""
    normalized = " ".join(message_text.casefold().replace("’", "'").split())
    named_player = _named_public_player(game, player, message_text)
    sounds_like_accusation = any(
        marker in normalized
        for marker in ("sus", "suspect", "suspicious", "mafia", "vote out", "vote for")
    )
    sounds_like_defense = any(
        marker in normalized
        for marker in ("not ", "isn't", "isnt", "innocent", "defend", "trust ")
    )
    mentions_evidence = any(
        marker in normalized
        for marker in ("because", "evidence", "clue", "receipts", "timeline", "said")
    )
    agrees = any(marker in normalized for marker in ("agree", "same here", "exactly"))
    actor_named = bool(
        player.name.strip()
        and re.search(
            rf"(?<!\w){re.escape(player.name.strip())}(?!\w)",
            message_text,
            re.IGNORECASE,
        )
    )

    # Mafia can make a public, role-blind defense when challenged; it does not
    # disclose the role or claim any private night information.
    if player.role == RoleKey.MAFIA and actor_named and sounds_like_accusation:
        defense = _MAFIA_SELF_DEFENSES.get(
            personality_for(player).key,
            "That's a theory, not proof. What public clue points to me?",
        )
        return _voice_reply(player, defense)

    if named_player is not None:
        name = named_player.name
        if sounds_like_defense:
            options = (
                f"I hear you on {name}. What public detail makes you trust that read?",
                f"Fair defense of {name}. Let's see whether the public timeline backs it up.",
                f"Noted. I will not write {name} off or accuse them without receipts.",
            )
        elif sounds_like_accusation:
            options = (
                f"Noted on {name}. I will keep that theory open, but I need a public clue before I lock in.",
                f"Okay, {name} is on the board. What is the strongest receipt behind that read?",
                f"That is a bold call on {name}. Let's check the timeline before we vote.",
            )
        elif mentions_evidence:
            options = (
                f"That detail about {name} is worth checking. Does anyone else remember it the same way?",
                f"Okay, the {name} theory has an actual clue behind it. Let's compare notes.",
                f"That is a useful receipt on {name}; I am keeping it in my notes.",
            )
        else:
            options = (
                f"Interesting read on {name}. What clue is doing the heavy lifting for you?",
                f"Noted on {name}. I am keeping my vote flexible until we compare evidence.",
            )
        return _voice_reply(player, rng.choice(options))

    if sounds_like_defense:
        options = (
            "Fair point. What public clue backs up that read?",
            "I hear you. Let's check the timeline before we lock anyone in.",
            "That is a reasonable take; I am keeping the vote open while we compare receipts.",
        )
    elif sounds_like_accusation:
        options = (
            "That theory is on the board. What is the strongest public clue behind it?",
            "Okay, bold read. I want receipts before I let the group chat decide the vote.",
            "I am listening, but vibes alone are not enough to call it. What happened publicly?",
        )
    elif mentions_evidence:
        options = (
            "That timeline detail could matter. Does anyone else remember it the same way?",
            "Receipts! Let's compare what everyone actually saw before voting.",
            "That gives us something concrete to check instead of just a vibe.",
        )
    elif agrees:
        options = (
            "Same page, but let's make sure we have a clue behind the agreement.",
            "I see it too. What public detail should we check next?",
        )
    elif "?" in message_text:
        options = (
            "Good question. What public clue should we check first?",
            "I would compare the timeline before deciding. What do you think?",
            "I have a theory, but I want receipts before calling it. Anyone else?",
        )
    else:
        options = personality_for(player).reply_lines
    return _voice_reply(player, rng.choice(options))
