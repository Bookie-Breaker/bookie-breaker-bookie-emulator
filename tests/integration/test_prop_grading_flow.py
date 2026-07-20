"""Phase 7 Wave 3: player-prop grading end to end.

Places a soccer PLAYER_PROP bet through the API (Wave 0 placement path with
a mocked prop best-line), publishes game.completed, and stubs the
statistics-service box-score endpoint: the matched bet grades WON with
actual_stat_value persisted on the grade row; an unmatched player stays
OPEN (never VOID) so a later box-score correction can still settle it.
"""

import json
import time
import uuid
from typing import Any

import redis as sync_redis
from httpx import Response

from bookie_emulator.db.engine import create_engine
from bookie_emulator.db.repository import PaperBetRepository
from tests.integration.conftest import (
    LINES_URL,
    STATS_URL,
    enveloped,
    final_game_payload,
    future_start,
    mock_scheduled_game,
    place_bet,
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
        "away_team": {
            "id": "team-away",
            "abbreviation": "PSG",
            "score": 1,
            "soccer_players": [
                {
                    "player_id": str(uuid.uuid4()),
                    "player_name": "Kylian Mbappé",
                    "position": "F",
                    "minutes": 90,
                    "goals": 1,
                    "assists": 0,
                    "shots": 4,
                    "shots_on_target": 3,
                    "yellow_cards": 1,
                    "red_cards": 0,
                }
            ],
        },
    }


def mock_box_score(router: Any, game_id: str) -> None:
    router.get(f"{STATS_URL}/api/v1/stats/games/{game_id}/box-score").mock(
        return_value=Response(200, json=enveloped(soccer_box_score_payload(game_id)))
    )


def mock_prop_best_lines(router: Any, ext_id: str) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(
        return_value=Response(
            200,
            json=enveloped(
                [
                    {
                        "market_type": "PLAYER_PROP",
                        "selection": "Erling Haaland Over 2.5 Shots",
                        "side": "OVER",
                        "line_value": 2.5,
                        "best_odds_american": -115,
                        "best_odds_decimal": 1.87,
                        "implied_probability": 0.5349,
                        "sportsbook_id": "00000000-0000-4000-8000-00000000dddd",
                        "sportsbook_key": "draftkings",
                        "timestamp": future_start(hours=-1),
                    }
                ]
            ),
        )
    )


def place_prop_bet(client: Any, game_id: str, ext_id: str, player_external_id: str) -> str:
    response = place_bet(
        client,
        game_id,
        ext_id,
        market_type="PLAYER_PROP",
        selection="Erling Haaland Over 2.5 Shots",
        side="OVER",
        player_external_id=player_external_id,
        stat_type="player_shots",
        prop_type="OVER_UNDER",
    )
    assert response.status_code == 201, response.text
    bet_id: str = response.json()["data"]["id"]
    return bet_id


def game_completed_payload(game_id: str, ext_id: str) -> dict[str, Any]:
    return {
        "event": "game.completed",
        "timestamp": "2026-07-19T22:15:00Z",
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


class TestPlayerPropGradingFlow:
    def test_event_grades_matched_prop_and_leaves_unmatched_open(
        self, client, upstream, redis_url, migrated_database_url
    ) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_prop_best_lines(upstream, ext_id)
        mock_box_score(upstream, game_id)

        # matched: player_external_id is the Odds API name slug for the
        # box score's "Erling Haaland"; unmatched: nobody slugs to messi
        matched_id = place_prop_bet(client, game_id, ext_id, "erling-haaland")
        unmatched_id = place_prop_bet(client, game_id, ext_id, "lionel-messi")

        payload = game_completed_payload(game_id, ext_id)
        redis_client = sync_redis.Redis.from_url(redis_url, decode_responses=True)
        deadline = time.monotonic() + 15.0
        data = None
        while time.monotonic() < deadline:
            redis_client.publish("events:game.completed", json.dumps(payload))
            detail = client.get(f"/api/v1/emulator/bets/{matched_id}").json()["data"]
            if detail["result"] != "PENDING":
                data = detail
                break
            time.sleep(0.25)
        redis_client.close()

        assert data is not None, "matched prop bet was never graded from the event"
        # OVER 2.5 shots with 3 shots in the box score: WON
        assert data["result"] == "WIN"
        assert data["market_type"] == "PLAYER_PROP"
        assert data["clv"] is None  # prop CLV is deferred in v1
        assert data["grade"]["result_description"] == "Erling Haaland landed 3 shots, over 2.5"
        assert data["grade"]["actual_home_score"] == 2
        assert data["grade"]["actual_away_score"] == 1

        # the same event was processed for the whole game, so the unmatched
        # bet has had its chance: no slug match keeps it OPEN, never VOID
        unmatched = client.get(f"/api/v1/emulator/bets/{unmatched_id}").json()["data"]
        assert unmatched["result"] == "PENDING"
        assert unmatched["graded_at"] is None

    async def test_actual_stat_value_persisted_on_grade_row(
        self, client, upstream, redis_url, migrated_database_url
    ) -> None:
        """The manual grading path persists actual_stat_value + stat_type."""
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_prop_best_lines(upstream, ext_id)
        bet_id = place_prop_bet(client, game_id, ext_id, "erling-haaland")

        # the game goes FINAL and the box score becomes available
        upstream.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(
            return_value=Response(200, json=enveloped(final_game_payload(game_id, 2, 1, league="FIFA_WC")))
        )
        mock_box_score(upstream, game_id)

        response = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={"force": False})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["result"] == "WIN"

        engine = create_engine(migrated_database_url)
        try:
            found = await PaperBetRepository(engine).get_with_grade(uuid.UUID(bet_id))
            assert found is not None
            bet, grade = found
            assert bet.status == "WON"
            assert grade is not None
            assert grade.actual_stat_value == 3.0
            assert grade.stat_type == "player_shots"
            assert grade.clv is None
            assert grade.actual_result == "Erling Haaland landed 3 shots, over 2.5"
        finally:
            await engine.dispose()

    def test_manual_grade_422_while_box_score_missing(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_prop_best_lines(upstream, ext_id)
        bet_id = place_prop_bet(client, game_id, ext_id, "erling-haaland")

        upstream.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(
            return_value=Response(200, json=enveloped(final_game_payload(game_id, 2, 1, league="FIFA_WC")))
        )
        upstream.get(f"{STATS_URL}/api/v1/stats/games/{game_id}/box-score").mock(return_value=Response(404))

        response = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={})
        assert response.status_code == 422
        assert "not available yet" in response.json()["error"]["message"]

        # the bet is still OPEN: the poller will retry once the box score lands
        detail = client.get(f"/api/v1/emulator/bets/{bet_id}").json()["data"]
        assert detail["result"] == "PENDING"
