"""Parlay flows (ADR-028): placement, idempotent replay, leg grading via
game.completed events, parent settlement with re-pricing, and the bet_class
performance breakdown."""

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis as sync_redis
from httpx import Response

from tests.integration.conftest import (
    LINES_URL,
    enveloped,
    game_lines_payload,
    iso,
    mock_best_lines,
    mock_scheduled_game,
)

STAKE = 1.0
# best-lines mock prices: SPREAD HOME -3.5 @ 1.952 (pinnacle),
# MONEYLINE HOME @ 2.2 (betmgm) -> combined 4.2944
SPREAD_DECIMAL = 1.952
MONEYLINE_DECIMAL = 2.2
PINNED_SPREAD_DECIMAL = 1.926  # fanduel -3.0 in game_lines_payload


def leg_body(
    game_id: str,
    ext_id: str,
    market_type: str = "SPREAD",
    selection: str = "Los Angeles Lakers -3.5",
    side: str = "HOME",
    **overrides: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "game_id": game_id,
        "game_external_id": ext_id,
        "market_type": market_type,
        "selection": selection,
        "side": side,
    }
    body.update(overrides)
    return body


def moneyline_leg(game_id: str, ext_id: str) -> dict[str, Any]:
    return leg_body(game_id, ext_id, market_type="MONEYLINE", selection="Los Angeles Lakers", side="HOME")


def post_parlay(client: Any, legs: list[dict[str, Any]], idempotency_key: str | None = None, **overrides: Any) -> Any:
    body: dict[str, Any] = {"legs": legs, "predicted_probability": 0.18, "edge_percentage": 3.5, "stake": STAKE}
    body.update(overrides)
    return client.post(
        "/api/v1/emulator/parlays",
        json=body,
        headers={"X-Idempotency-Key": idempotency_key or str(uuid.uuid4())},
    )


def mock_game_lines(router: Any, ext_id: str) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}").mock(
        return_value=Response(200, json=enveloped(game_lines_payload(ext_id)))
    )


def scheduled_pair(upstream: Any) -> tuple[tuple[str, str], tuple[str, str]]:
    """Two scheduled games with best-lines mocks; returns ((game, ext), (game, ext))."""
    games = []
    for _ in range(2):
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        mock_best_lines(upstream, ext_id)
        games.append((game_id, ext_id))
    return games[0], games[1]


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


def drive_events_until(
    client: Any,
    redis_url: str,
    bet_id: str,
    payloads: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float = 20.0,
) -> dict[str, Any] | None:
    """Republish game.completed events (pub/sub is fire-and-forget) until the
    parlay detail satisfies the predicate."""
    redis_client = sync_redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for payload in payloads:
                redis_client.publish("events:game.completed", json.dumps(payload))
            data = client.get(f"/api/v1/emulator/parlays/{bet_id}").json()["data"]
            if predicate(data):
                return data
            time.sleep(0.25)
        return None
    finally:
        redis_client.close()


class TestParlayPlacement:
    def test_place_two_leg_parlay_captures_combined_odds(self, client, upstream) -> None:
        (game_a, ext_a), (game_b, ext_b) = scheduled_pair(upstream)
        response = post_parlay(client, [leg_body(game_a, ext_a), moneyline_leg(game_b, ext_b)])
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["is_parlay"] is True
        assert data["game_id"] is None
        assert data["side"] is None
        assert data["line_value"] is None
        assert data["game_external_id"] == f"parlay:{ext_a}+1"
        assert data["market_type"] == "SPREAD"  # first leg's market; is_parlay is the discriminator
        assert data["selection"].startswith("2-leg parlay: ")
        assert data["sportsbook_key"] == "mixed"  # pinnacle + betmgm legs
        assert data["combined_odds_decimal"] == pytest.approx(SPREAD_DECIMAL * MONEYLINE_DECIMAL, abs=1e-3)
        assert data["combined_odds_decimal"] == data["odds_decimal"]
        assert data["odds_american"] == 329  # decimal_to_american(4.2944)
        assert data["result"] == "PENDING"
        legs = data["legs"]
        assert [leg["leg_index"] for leg in legs] == [0, 1]
        assert legs[0]["market_type"] == "SPREAD"
        assert legs[0]["line_value"] == pytest.approx(-3.5)
        assert legs[0]["odds_decimal"] == pytest.approx(SPREAD_DECIMAL)
        assert legs[1]["market_type"] == "MONEYLINE"
        assert legs[1]["odds_decimal"] == pytest.approx(MONEYLINE_DECIMAL)
        assert all(leg["leg_status"] == "PENDING" for leg in legs)

    def test_idempotent_replay_returns_same_parlay_without_duplicate_legs(self, client, upstream) -> None:
        (game_a, ext_a), (game_b, ext_b) = scheduled_pair(upstream)
        key = str(uuid.uuid4())
        legs = [leg_body(game_a, ext_a), moneyline_leg(game_b, ext_b)]
        first = post_parlay(client, legs, idempotency_key=key)
        assert first.status_code == 201, first.text
        bet_id = first.json()["data"]["id"]

        replay = post_parlay(client, legs, idempotency_key=key)
        assert replay.status_code == 200
        assert replay.json()["data"]["id"] == bet_id
        assert len(replay.json()["data"]["legs"]) == 2

        detail = client.get(f"/api/v1/emulator/parlays/{bet_id}")
        assert detail.status_code == 200
        assert len(detail.json()["data"]["legs"]) == 2

    def test_parlay_appears_in_ledger_with_is_parlay_filter(self, client, upstream) -> None:
        (game_a, ext_a), (game_b, ext_b) = scheduled_pair(upstream)
        bet_id = post_parlay(client, [leg_body(game_a, ext_a), moneyline_leg(game_b, ext_b)]).json()["data"]["id"]
        listed = client.get("/api/v1/emulator/bets", params={"is_parlay": True, "limit": 200})
        assert listed.status_code == 200
        assert bet_id in [bet["id"] for bet in listed.json()["data"]]

    def test_prop_leg_rejected_422(self, client, upstream) -> None:
        (game_a, ext_a), (game_b, ext_b) = scheduled_pair(upstream)
        prop_leg = leg_body(game_b, ext_b, market_type="PLAYER_PROP", selection="LeBron Over 27.5", side="OVER")
        response = post_parlay(client, [leg_body(game_a, ext_a), prop_leg])
        assert response.status_code == 422
        assert "only SPREAD, TOTAL, and MONEYLINE" in response.json()["error"]["message"]

    def test_duplicate_and_opposite_legs_rejected_422(self, client, upstream) -> None:
        (game_a, ext_a), _ = scheduled_pair(upstream)
        duplicate = post_parlay(client, [leg_body(game_a, ext_a), leg_body(game_a, ext_a)])
        assert duplicate.status_code == 422
        opposite = post_parlay(
            client,
            [
                leg_body(game_a, ext_a),
                leg_body(game_a, ext_a, selection="Boston Celtics +3.5", side="AWAY"),
            ],
        )
        assert opposite.status_code == 422
        assert "opposite-side" in opposite.json()["error"]["message"]

    def test_single_leg_fails_validation_400(self, client, upstream) -> None:
        (game_a, ext_a), _ = scheduled_pair(upstream)
        response = post_parlay(client, [leg_body(game_a, ext_a)])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_get_unknown_parlay_404(self, client) -> None:
        assert client.get(f"/api/v1/emulator/parlays/{uuid.uuid4()}").status_code == 404


class TestParlayGrading:
    def test_parent_settles_won_with_combined_payout_and_snapshot(self, client, upstream, redis_url) -> None:
        (game_a, ext_a), (game_b, ext_b) = scheduled_pair(upstream)
        placed = post_parlay(client, [leg_body(game_a, ext_a), moneyline_leg(game_b, ext_b)])
        assert placed.status_code == 201, placed.text
        bet_id = placed.json()["data"]["id"]
        before = datetime.now(tz=UTC) - timedelta(seconds=5)

        # game A first: the SPREAD leg wins but the parent must stay PENDING
        partial = drive_events_until(
            client,
            redis_url,
            bet_id,
            [completed_payload(game_a, ext_a, 112, 104)],  # LAL by 8 covers -3.5
            lambda data: data["legs"][0]["leg_status"] == "WIN",
        )
        assert partial is not None, "first leg was never graded"
        assert partial["result"] == "PENDING"
        assert partial["legs"][1]["leg_status"] == "PENDING"

        # manual grading is rejected while legs remain open
        manual = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={})
        assert manual.status_code == 422
        assert "open legs" in manual.json()["error"]["message"]

        data = drive_events_until(
            client,
            redis_url,
            bet_id,
            [completed_payload(game_b, ext_b, 100, 90)],  # MONEYLINE HOME wins
            lambda data: data["result"] != "PENDING",
        )
        assert data is not None, "parlay was never settled"
        assert data["result"] == "WIN"
        assert [leg["leg_status"] for leg in data["legs"]] == ["WIN", "WIN"]
        expected_profit = STAKE * (SPREAD_DECIMAL * MONEYLINE_DECIMAL - 1.0)
        assert data["profit_loss"] == pytest.approx(expected_profit, abs=1e-3)
        grade = data["grade"]
        assert grade["result_description"] == "Legs: W-W"
        assert grade["actual_home_score"] is None  # the parent spans games
        assert data["clv"] is None  # CLV for parlays is deferred in v1

        # a bankroll snapshot was appended inside the settlement transaction
        history = client.get(
            "/api/v1/emulator/bankroll/history",
            params={"interval": "per_bet", "date_from": before.isoformat()},
        )
        snapshots = history.json()["data"]["snapshots"]
        assert snapshots, "expected a snapshot from the parlay settlement"

    def test_push_leg_reprices_parent_over_surviving_leg(self, client, upstream, redis_url) -> None:
        game_c = str(uuid.uuid4())
        ext_c = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_c)
        mock_game_lines(upstream, ext_c)  # pinned fanduel SPREAD -3.0 @ 1.926
        game_d = str(uuid.uuid4())
        ext_d = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_d)
        mock_best_lines(upstream, ext_d)

        pinned_leg = leg_body(game_c, ext_c, selection="Los Angeles Lakers -3", side="HOME", sportsbook_key="fanduel")
        placed = post_parlay(client, [pinned_leg, moneyline_leg(game_d, ext_d)])
        assert placed.status_code == 201, placed.text
        bet_id = placed.json()["data"]["id"]
        assert placed.json()["data"]["combined_odds_decimal"] == pytest.approx(
            PINNED_SPREAD_DECIMAL * MONEYLINE_DECIMAL, abs=1e-3
        )

        data = drive_events_until(
            client,
            redis_url,
            bet_id,
            [
                completed_payload(game_c, ext_c, 110, 107),  # margin 3 pushes -3.0
                completed_payload(game_d, ext_d, 100, 90),  # MONEYLINE HOME wins
            ],
            lambda data: data["result"] != "PENDING",
        )
        assert data is not None, "parlay was never settled"
        assert data["result"] == "WIN"
        assert [leg["leg_status"] for leg in data["legs"]] == ["PUSH", "WIN"]
        # the pushed leg drops out: payout re-prices to the surviving leg
        assert data["profit_loss"] == pytest.approx(STAKE * (MONEYLINE_DECIMAL - 1.0), abs=1e-3)
        assert data["grade"]["result_description"] == f"Legs: PUSH-W (re-priced {MONEYLINE_DECIMAL:.2f})"

    def test_breakdown_by_bet_class_shows_parlay_row(self, client, upstream, redis_url) -> None:
        (game_a, ext_a), (game_b, ext_b) = scheduled_pair(upstream)
        bet_id = post_parlay(client, [leg_body(game_a, ext_a), moneyline_leg(game_b, ext_b)]).json()["data"]["id"]
        settled = drive_events_until(
            client,
            redis_url,
            bet_id,
            [
                completed_payload(game_a, ext_a, 112, 104),
                completed_payload(game_b, ext_b, 100, 90),
            ],
            lambda data: data["result"] != "PENDING",
        )
        assert settled is not None, "parlay was never settled"

        response = client.get("/api/v1/emulator/performance/breakdown", params={"group_by": "bet_class"})
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["group_by"] == "bet_class"
        groups = {entry["group"]: entry for entry in body["breakdowns"]}
        assert "parlay" in groups
        assert groups["parlay"]["total_bets"] >= 1
