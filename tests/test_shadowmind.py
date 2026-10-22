"""Tests for Shadowmind's public-evidence reasoning."""

import unittest
from types import SimpleNamespace

from shadowmind import (
    choose_suspect,
    explain_suspicion,
    forget_game,
    suspicion_score,
    update_beliefs,
)


class FakeRNG:
    """Predictable random source for repeatable tests."""

    def choice(self, values):
        return values[0]


def make_player(user_id, personality="analytical"):
    return SimpleNamespace(
        user_id=user_id,
        personality=personality,
        alive=True,
    )


def make_game(game_id="shadowmind-test"):
    observer = make_player("ai-1")
    player_a = make_player("player-a")
    player_b = make_player("player-b")

    players = {
        observer.user_id: observer,
        player_a.user_id: player_a,
        player_b.user_id: player_b,
    }

    game = SimpleNamespace(
        game_id=game_id,
        players=players,
        evidence_ledger=[],
        living_players=lambda: [
            player for player in players.values()
            if player.alive
        ],
    )

    return game, observer, player_a, player_b


class ShadowmindTests(unittest.TestCase):

    def setUp(self):
        self.game, self.observer, self.a, self.b = make_game()
        forget_game(self.game.game_id)

    def tearDown(self):
        forget_game(self.game.game_id)

    def add_evidence(self, event_id, actor_id, kind="day_vote_changed"):
        self.game.evidence_ledger.append({
            "event_id": event_id,
            "kind": kind,
            "actor_id": actor_id,
            "target_id": None,
        })

    def test_public_vote_change_increases_suspicion(self):
        self.add_evidence("event-1", self.a.user_id)

        score = suspicion_score(
            self.game,
            self.observer.user_id,
            self.a.user_id,
        )

        self.assertEqual(score, 0.35)

    def test_same_event_is_not_counted_twice(self):
        self.add_evidence("event-1", self.a.user_id)

        update_beliefs(self.game, self.observer.user_id)
        update_beliefs(self.game, self.observer.user_id)

        score = suspicion_score(
            self.game,
            self.observer.user_id,
            self.a.user_id,
        )

        self.assertEqual(score, 0.35)

    def test_observer_own_actions_are_ignored(self):
        self.add_evidence("event-1", self.observer.user_id)

        score = suspicion_score(
            self.game,
            self.observer.user_id,
            self.observer.user_id,
        )

        self.assertEqual(score, 0.0)

    def test_unrelated_event_does_not_raise_suspicion(self):
        self.add_evidence(
            "event-1",
            self.a.user_id,
            kind="morning_story",
        )

        score = suspicion_score(
            self.game,
            self.observer.user_id,
            self.a.user_id,
        )

        self.assertEqual(score, 0.0)

    def test_ai_selects_a_candidate_with_public_evidence(self):
        self.add_evidence("event-1", self.a.user_id)

        selected = choose_suspect(
            game=self.game,
            observer=self.observer,
            candidates=[self.a, self.b],
            rng=FakeRNG(),
        )

        self.assertEqual(selected, self.a.user_id)

    def test_empty_candidates_return_none(self):
        selected = choose_suspect(
            game=self.game,
            observer=self.observer,
            candidates=[],
            rng=FakeRNG(),
        )

        self.assertIsNone(selected)

    def test_explanation_does_not_invent_evidence(self):
        explanation = explain_suspicion(
            self.game,
            self.observer.user_id,
            self.a.user_id,
        )

        self.assertIn("not enough,", explanation.lower())

if __name__ == "__main__":
    unittest.main()