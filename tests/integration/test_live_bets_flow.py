"""Live (in-game) bet flows (Phase 7 Wave 2): placement against an
IN_PROGRESS game with live-line preference, normal settlement through the
game.completed grading flow, and the ?is_live= ledger filter."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import redis as sync_redis
from httpx import Response

from tests.integration.conftest import (
    LINES_URL,
    STATS_URL,
    best_lines_payload,
    closing_lines_payload,
    enveloped,
    game_payload,
    iso,
    mock_best_lines,
    mock_scheduled_game,
    place_bet,
)

LIVE_SPREAD_LINE = -4.5
LIVE_SPREAD_AMERICAN = -115
LIVE_SPREAD_DECIMAL = 1.87
PREGAME_SPREAD_AMERICAN = -105  # pinnacle -3.5 in best_lines_payload


def in_progress_game_payload(game_id: str) -> dict[str, Any]:
    return game_payload(
        game_id,
        status="IN_PROGRESS",
        scheduled_start=iso(datetime.now(tz=UTC) - timedelta(hours=1)),
    )


def mock_in_progress_game(router: Any, game_id: str) -> None:
    router.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(
        return_value=Response(200, json=enveloped(in_progress_game_payload(game_id)))
    )


def mock_final_game(router: Any, game_id: str) -> None:
    payload = game_payload(game_id, status="FINAL", scheduled_start=iso(datetime.now(tz=UTC) - timedelta(hours=3)))
    router.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(return_value=Response(200, json=enveloped(payload)))


def live_best_lines_payload(ext_id: str) -> list[dict[str, Any]]:
    """Pregame best lines plus a fresher in-game SPREAD line (is_live=true)."""
    live_line = {
        "market_type": "SPREAD",
        "selection": f"Los Angeles Lakers {LIVE_SPREAD_LINE}",
        "side": "HOME",
        "line_value": LIVE_SPREAD_LINE,
        "best_odds_american": LIVE_SPREAD_AMERICAN,
        "best_odds_decimal": LIVE_SPREAD_DECIMAL,
        "implied_probability": 0.5349,
        "sportsbook_id": "00000000-0000-4000-8000-00000000ffff",
        "sportsbook_key": "draftkings",
        "is_live": True,
        "timestamp": "2026-07-19T18:00:00Z",
    }
    return [*best_lines_payload(ext_id), live_line]


def mock_live_best_lines(router: Any, ext_id: str) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(
        return_value=Response(200, json=enveloped(live_best_lines_payload(ext_id)))
    )


def mock_closing(router: Any, ext_id: str) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/closing").mock(
        return_value=Response(200, json=enveloped(closing_lines_payload(ext_id)))
    )


def completed_payload(game_id: str, ext_id: str, home: int, away: int) -> dict[str, Any]:
    return {
        "event": "game.completed",
        "timestamp": iso(datetime.now(tz=UTC)),
        "game_id": game_id,
        "game_external_id": ext_id,
        "league": "NBA",
        "home_team": "LAL",
        "away_team": "BOS",
        "home_score": home,
        "away_score": away,
        "total": home + away,
        "margin": home - away,
        "overtime": False,
    }


def drive_grading_until_settled(
    client: Any, redis_url: str, bet_id: str, payload: dict[str, Any], timeout: float = 20.0
) -> dict[str, Any] | None:
    """Republish game.completed (pub/sub is fire-and-forget) until the bet
    leaves PENDING."""
    redis_client = sync_redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            redis_client.publish("events:game.completed", json.dumps(payload))
            data = client.get(f"/api/v1/emulator/bets/{bet_id}").json()["data"]
            if data["result"] != "PENDING":
                return data
            time.sleep(0.25)
        return None
    finally:
        redis_client.close()


def place_live_bet(client: Any, upstream: Any) -> tuple[str, str, dict[str, Any]]:
    game_id = str(uuid.uuid4())
    ext_id = f"odds-{uuid.uuid4().hex}"
    mock_in_progress_game(upstream, game_id)
    mock_live_best_lines(upstream, ext_id)
    response = place_bet(
        client,
        game_id,
        ext_id,
        selection=f"Los Angeles Lakers {LIVE_SPREAD_LINE}",
        is_live=True,
    )
    assert response.status_code == 201, response.text
    return game_id, ext_id, response.json()["data"]


class TestLivePlacement:
    def test_live_bet_on_in_progress_game_captures_live_line(self, client, upstream) -> None:
        _, _, data = place_live_bet(client, upstream)
        assert data["is_live"] is True
        assert data["is_parlay"] is False
        # the is_live=true draftkings line beats the pregame pinnacle spread
        assert data["line_value"] == LIVE_SPREAD_LINE
        assert data["odds_american"] == LIVE_SPREAD_AMERICAN
        assert data["sportsbook_key"] == "draftkings"
        assert data["result"] == "PENDING"

    def test_default_bet_on_in_progress_game_still_422(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_in_progress_game(upstream, game_id)
        mock_live_best_lines(upstream, ext_id)
        response = place_bet(client, game_id, ext_id)
        assert response.status_code == 422
        assert "already started" in response.json()["error"]["message"]

    def test_live_bet_on_final_game_422(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_final_game(upstream, game_id)
        mock_live_best_lines(upstream, ext_id)
        response = place_bet(client, game_id, ext_id, is_live=True)
        assert response.status_code == 422
        assert "has ended" in response.json()["error"]["message"]

    def test_live_bet_without_live_lines_falls_back_to_pregame(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_in_progress_game(upstream, game_id)
        mock_best_lines(upstream, ext_id)  # pregame-only payload
        response = place_bet(client, game_id, ext_id, is_live=True)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["is_live"] is True
        assert data["odds_american"] == PREGAME_SPREAD_AMERICAN


class TestLiveGrading:
    def test_live_bet_settles_normally_on_game_completed(self, client, upstream, redis_url) -> None:
        game_id, ext_id, placed = place_live_bet(client, upstream)
        mock_closing(upstream, ext_id)
        # LAL by 8 covers the live -4.5 spread
        data = drive_grading_until_settled(
            client, redis_url, placed["id"], completed_payload(game_id, ext_id, 112, 104)
        )
        assert data is not None, "live bet was never graded"
        assert data["result"] == "WIN"
        assert data["is_live"] is True
        expected_profit = placed["stake"] * (LIVE_SPREAD_DECIMAL - 1.0)
        assert abs(data["profit_loss"] - expected_profit) < 1e-3

        detail = client.get(f"/api/v1/emulator/bets/{placed['id']}").json()["data"]
        assert detail["grade"]["actual_home_score"] == 112
        assert detail["grade"]["actual_away_score"] == 104


class TestLiveLedgerFilter:
    def test_is_live_filter_partitions_the_ledger(self, client, upstream) -> None:
        _, _, live_data = place_live_bet(client, upstream)
        pregame_game = str(uuid.uuid4())
        pregame_ext = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, pregame_game)
        mock_best_lines(upstream, pregame_ext)
        pregame = place_bet(client, pregame_game, pregame_ext)
        assert pregame.status_code == 201, pregame.text
        pregame_id = pregame.json()["data"]["id"]

        live_ids = [
            bet["id"]
            for bet in client.get("/api/v1/emulator/bets", params={"is_live": True, "limit": 200}).json()["data"]
        ]
        assert live_data["id"] in live_ids
        assert pregame_id not in live_ids

        pregame_ids = [
            bet["id"]
            for bet in client.get("/api/v1/emulator/bets", params={"is_live": False, "limit": 200}).json()["data"]
        ]
        assert pregame_id in pregame_ids
        assert live_data["id"] not in pregame_ids
