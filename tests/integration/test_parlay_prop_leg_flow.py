"""Phase 7 Wave 4: PLAYER_PROP parlay legs end to end.

Places a mixed 2-leg parlay (soccer three-way moneyline + anytime
goalscorer YES on the same game) through the API with mocked upstreams,
then grades the game: the final score settles the team leg and the stubbed
box score settles the prop leg, so the parent settles WON with the combined
payout. When the box score is missing the prop leg -- and therefore the
parent -- stays OPEN for the poller to retry.
"""

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import redis as sync_redis
from httpx import Response

from tests.integration.conftest import (
    LINES_URL,
    STATS_URL,
    enveloped,
    iso,
    mock_scheduled_game,
)

STAKE = 1.0
MONEYLINE_DECIMAL = 2.5
PROP_DECIMAL = 2.4

MONEYLINE_SELECTION = "Manchester City"
PROP_SELECTION = "Erling Haaland Anytime Goalscorer"


def mixed_best_lines_payload(ext_id: str) -> list[dict[str, Any]]:
    """A soccer game's best lines: three-way moneyline rows plus a per-player
    goalscorer prop row (prop best-line entries carry the player in the
    selection string)."""
    common = {
        "line_value": None,
        "sportsbook_id": "00000000-0000-4000-8000-00000000cccc",
        "sportsbook_key": "betmgm",
        "timestamp": "2026-07-19T12:00:00Z",
    }
    return [
        {
            **common,
            "market_type": "MONEYLINE",
            "selection": MONEYLINE_SELECTION,
            "side": "HOME",
            "best_odds_american": 150,
            "best_odds_decimal": MONEYLINE_DECIMAL,
            "implied_probability": 0.4,
        },
        {
            **common,
            "market_type": "MONEYLINE",
            "selection": "Draw",
            "side": "DRAW",
            "best_odds_american": 230,
            "best_odds_decimal": 3.3,
            "implied_probability": 0.303,
        },
        {
            **common,
            "market_type": "PLAYER_PROP",
            "selection": PROP_SELECTION,
            "side": "YES",
            "best_odds_american": 140,
            "best_odds_decimal": PROP_DECIMAL,
            "implied_probability": 0.4167,
        },
        {
            # another player's YES prop: selection matching must not capture it
            **common,
            "market_type": "PLAYER_PROP",
            "selection": "Kylian Mbappe Anytime Goalscorer",
            "side": "YES",
            "best_odds_american": 250,
            "best_odds_decimal": 3.5,
            "implied_probability": 0.2857,
        },
    ]


def mock_mixed_best_lines(router: Any, ext_id: str) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(
        return_value=Response(200, json=enveloped(mixed_best_lines_payload(ext_id)))
    )


def soccer_box_score_payload(game_id: str) -> dict[str, Any]:
    return {
        "game_id": game_id,
        "sport": "SOCCER",
        "status": "FINAL",
        "home_team": {
            "id": "team-home",
            "abbreviation": "MCI",
            "score": 2,
            "soccer_players": [
                {
                    "player_id": str(uuid.uuid4()),
                    "player_name": "Erling Haaland",
                    "position": "F",
                    "minutes": 90,
                    "goals": 1,
                    "assists": 0,
                    "shots": 3,
                    "shots_on_target": 2,
                    "yellow_cards": 0,
                    "red_cards": 0,
                }
            ],
        },
        "away_team": {"id": "team-away", "abbreviation": "PSG", "score": 1, "soccer_players": []},
    }


def mock_box_score(router: Any, game_id: str) -> None:
    router.get(f"{STATS_URL}/api/v1/stats/games/{game_id}/box-score").mock(
        return_value=Response(200, json=enveloped(soccer_box_score_payload(game_id)))
    )


def mixed_legs(game_id: str, ext_id: str) -> list[dict[str, Any]]:
    return [
        {
            "game_id": game_id,
            "game_external_id": ext_id,
            "market_type": "MONEYLINE",
            "selection": MONEYLINE_SELECTION,
            "side": "HOME",
        },
        {
            "game_id": game_id,
            "game_external_id": ext_id,
            "market_type": "PLAYER_PROP",
            "selection": PROP_SELECTION,
            "side": "YES",
            "player_external_id": "erling-haaland",
            "stat_type": "player_goal_scorer_anytime",
            "prop_type": "YES_NO",
        },
    ]


def post_parlay(client: Any, legs: list[dict[str, Any]], idempotency_key: str | None = None) -> Any:
    body = {"legs": legs, "predicted_probability": 0.15, "edge_percentage": 3.0, "stake": STAKE}
    return client.post(
        "/api/v1/emulator/parlays",
        json=body,
        headers={"X-Idempotency-Key": idempotency_key or str(uuid.uuid4())},
    )


def completed_payload(game_id: str, ext_id: str) -> dict[str, Any]:
    return {
        "event": "game.completed",
        "timestamp": iso(datetime.now(tz=UTC)),
        "game_id": game_id,
        "game_external_id": ext_id,
        "league": "FIFA_WC",
        "home_team": "MCI",
        "away_team": "PSG",
        "home_score": 2,
        "away_score": 1,
        "total": 3,
        "margin": 1,
        "overtime": False,
    }


def drive_event_until(
    client: Any,
    redis_url: str,
    bet_id: str,
    payload: dict[str, Any],
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float = 20.0,
) -> dict[str, Any] | None:
    """Republish game.completed (pub/sub is fire-and-forget) until the parlay
    detail satisfies the predicate; None on timeout."""
    redis_client = sync_redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            redis_client.publish("events:game.completed", json.dumps(payload))
            data = client.get(f"/api/v1/emulator/parlays/{bet_id}").json()["data"]
            if predicate(data):
                return data
            time.sleep(0.25)
        return None
    finally:
        redis_client.close()


class TestMixedParlayPlacement:
    def test_place_mixed_parlay_persists_prop_columns_on_leg(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_mixed_best_lines(upstream, ext_id)

        response = post_parlay(client, mixed_legs(game_id, ext_id))
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["is_parlay"] is True
        assert data["combined_odds_decimal"] == pytest.approx(MONEYLINE_DECIMAL * PROP_DECIMAL, abs=1e-3)

        legs = data["legs"]
        assert [leg["leg_index"] for leg in legs] == [0, 1]
        team_leg, prop_leg = legs
        assert team_leg["market_type"] == "MONEYLINE"
        assert team_leg["player_external_id"] is None
        prop = prop_leg
        assert prop["market_type"] == "PLAYER_PROP"
        assert prop["side"] == "YES"
        assert prop["player_external_id"] == "erling-haaland"
        assert prop["stat_type"] == "player_goal_scorer_anytime"
        assert prop["prop_type"] == "YES_NO"
        # selection matching captured Haaland's price, not Mbappe's 3.5
        assert prop["odds_decimal"] == pytest.approx(PROP_DECIMAL)
        assert prop["leg_status"] == "PENDING"

    def test_same_player_same_stat_twice_rejected_422(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_mixed_best_lines(upstream, ext_id)

        legs = mixed_legs(game_id, ext_id)
        response = post_parlay(client, [legs[1], legs[1]])
        assert response.status_code == 422
        assert "duplicate or opposite-side" in response.json()["error"]["message"]


class TestMixedParlayGrading:
    def test_score_and_box_score_settle_parent_won_with_combined_payout(self, client, upstream, redis_url) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_mixed_best_lines(upstream, ext_id)
        mock_box_score(upstream, game_id)

        placed = post_parlay(client, mixed_legs(game_id, ext_id))
        assert placed.status_code == 201, placed.text
        bet_id = placed.json()["data"]["id"]

        data = drive_event_until(
            client,
            redis_url,
            bet_id,
            completed_payload(game_id, ext_id),
            lambda data: data["result"] != "PENDING",
        )
        assert data is not None, "mixed parlay was never settled"
        # MCI win 2-1 settles the HOME moneyline leg; Haaland's goal in the
        # box score settles the goalscorer YES leg
        assert data["result"] == "WIN"
        assert [leg["leg_status"] for leg in data["legs"]] == ["WIN", "WIN"]
        expected_profit = STAKE * (MONEYLINE_DECIMAL * PROP_DECIMAL - 1.0)
        assert data["profit_loss"] == pytest.approx(expected_profit, abs=1e-3)
        assert data["grade"]["result_description"] == "Legs: W-W"

    def test_missing_box_score_leaves_prop_leg_and_parent_open(self, client, upstream, redis_url) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_mixed_best_lines(upstream, ext_id)
        upstream.get(f"{STATS_URL}/api/v1/stats/games/{game_id}/box-score").mock(return_value=Response(404))

        placed = post_parlay(client, mixed_legs(game_id, ext_id))
        assert placed.status_code == 201, placed.text
        bet_id = placed.json()["data"]["id"]

        data = drive_event_until(
            client,
            redis_url,
            bet_id,
            completed_payload(game_id, ext_id),
            lambda data: data["legs"][0]["leg_status"] == "WIN",
        )
        assert data is not None, "team leg was never graded"
        # the team leg settled from the score, but the prop leg (and the
        # parent) wait for the box score; the fallback poller retries
        assert data["result"] == "PENDING"
        assert data["legs"][1]["leg_status"] == "PENDING"

        # manual parent grading is rejected and hints at the box-score wait
        manual = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={})
        assert manual.status_code == 422
        message = manual.json()["error"]["message"]
        assert "open legs" in message
        assert "box scores" in message
