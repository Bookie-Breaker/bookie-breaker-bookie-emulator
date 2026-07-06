"""Grading flows: manual endpoint, game.completed events, and the fallback poller."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import redis as sync_redis
import redis.asyncio as aioredis
from httpx import Response

from bookie_emulator.clients.lines import LinesClient
from bookie_emulator.clients.statistics import StatisticsClient
from bookie_emulator.config import Settings
from bookie_emulator.db.engine import create_engine
from bookie_emulator.db.repository import PaperBetRepository
from bookie_emulator.services.grader import GraderService
from bookie_emulator.services.poller import GradingPoller
from tests.integration.conftest import (
    LINES_URL,
    STATS_URL,
    closing_lines_payload,
    enveloped,
    final_game_payload,
    mock_best_lines,
    mock_scheduled_game,
    mock_soccer_best_lines,
    place_bet,
)


def mock_final_game(
    router: Any,
    game_id: str,
    home: int,
    away: int,
    result_id: str | None = None,
    league: str = "NBA",
    regulation_home_score: int | None = None,
    regulation_away_score: int | None = None,
) -> None:
    router.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(
        return_value=Response(
            200,
            json=enveloped(
                final_game_payload(
                    game_id,
                    home,
                    away,
                    result_id=result_id,
                    league=league,
                    regulation_home_score=regulation_home_score,
                    regulation_away_score=regulation_away_score,
                )
            ),
        )
    )


def mock_closing(router: Any, ext_id: str) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/closing").mock(
        return_value=Response(200, json=enveloped(closing_lines_payload(ext_id)))
    )


def wait_for_message(pubsub: Any, timeout: float = 10.0) -> dict[str, Any] | None:
    """Poll a pubsub until a data message arrives; a single get_message call
    can return None after merely consuming the subscribe confirmation."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message is not None:
            return message
    return None


class TestManualGrading:
    def _place(self, client: Any, upstream: Any) -> tuple[str, str, str]:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        mock_best_lines(upstream, ext_id)
        bet_id = place_bet(client, game_id, ext_id).json()["data"]["id"]
        return game_id, ext_id, bet_id

    def test_grade_unknown_bet_404(self, client) -> None:
        response = client.post(f"/api/v1/emulator/bets/{uuid.uuid4()}/grade", json={})
        assert response.status_code == 404

    def test_not_final_game_422(self, client, upstream) -> None:
        game_id, _, bet_id = self._place(client, upstream)
        # the game is still SCHEDULED per the placement mock
        response = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={})
        assert response.status_code == 422
        assert "not completed" in response.json()["error"]["message"]

    def test_manual_grade_happy_path_persists_grade_snapshot_and_clv(self, client, upstream) -> None:
        game_id, ext_id, bet_id = self._place(client, upstream)
        result_id = str(uuid.uuid4())
        mock_final_game(upstream, game_id, 112, 104, result_id=result_id)  # home covers -3.5
        mock_closing(upstream, ext_id)
        before = datetime.now(tz=UTC) - timedelta(seconds=5)

        response = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={"force": False})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["result"] == "WIN"
        assert data["profit_loss"] == pytest.approx(1.5 * (1.952 - 1.0), abs=1e-3)
        assert data["profit_loss_dollars"] == pytest.approx(data["profit_loss"] * 100.0, abs=1e-2)
        assert data["graded_at"] is not None
        # CLV: placed -105 at pinnacle, pinnacle closed -120
        assert data["closing_line_value"] == -4.0
        assert data["closing_odds_american"] == -120
        assert data["clv"] == pytest.approx(0.03318, abs=1e-4)
        grade = data["grade"]
        assert grade["result"] == "WIN"
        assert grade["actual_home_score"] == 112
        assert grade["actual_away_score"] == 104
        assert grade["actual_margin"] == 8
        assert grade["actual_total"] == 216
        assert grade["game_result_id"] == result_id
        assert grade["result_description"] == "LAL won by 8, covering -3.5"

        # a bankroll snapshot was appended inside the grading transaction
        history = client.get(
            "/api/v1/emulator/bankroll/history",
            params={"interval": "per_bet", "date_from": before.isoformat()},
        )
        snapshots = history.json()["data"]["snapshots"]
        assert snapshots, "expected a snapshot from the grading transaction"
        assert snapshots[-1]["total_bets"] >= 1

    def test_manual_grade_publishes_bet_graded_event(self, client, upstream, redis_url) -> None:
        game_id, ext_id, bet_id = self._place(client, upstream)
        mock_final_game(upstream, game_id, 112, 104)
        mock_closing(upstream, ext_id)

        redis_client = sync_redis.Redis.from_url(redis_url, decode_responses=True)
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe("events:bet.graded")
        try:
            assert client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={}).status_code == 200
            message = wait_for_message(pubsub)
            assert message is not None, "no bet.graded event received"
            payload = json.loads(message["data"])
            assert payload["event"] == "bet.graded"
            assert payload["bet_id"] == bet_id
            assert payload["game_id"] == game_id
            assert payload["league"] == "NBA"
            assert payload["market_type"] == "SPREAD"
            assert payload["result"] == "WIN"
            assert payload["stake"] == pytest.approx(1.5)
            assert payload["profit_loss"] == pytest.approx(1.5 * (1.952 - 1.0), abs=1e-3)
            assert payload["clv"] == pytest.approx(0.03318, abs=1e-4)
            assert payload["timestamp"].endswith("Z")
        finally:
            pubsub.close()
            redis_client.close()

    def test_regrade_conflicts_without_force_and_regrades_with_force(self, client, upstream) -> None:
        game_id, ext_id, bet_id = self._place(client, upstream)
        mock_final_game(upstream, game_id, 112, 104)
        mock_closing(upstream, ext_id)

        assert client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={}).status_code == 200
        conflict = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "DUPLICATE_RESOURCE"

        # corrected final score flips the grade
        mock_final_game(upstream, game_id, 104, 112)
        forced = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={"force": True})
        assert forced.status_code == 200
        data = forced.json()["data"]
        assert data["result"] == "LOSS"
        assert data["profit_loss"] == -1.5


class TestEventDrivenGrading:
    def test_game_completed_event_grades_open_bets(self, client, upstream, redis_url) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        mock_best_lines(upstream, ext_id)
        mock_closing(upstream, ext_id)
        bet_id = place_bet(client, game_id, ext_id).json()["data"]["id"]

        payload = {
            "event": "game.completed",
            "timestamp": "2026-07-04T23:15:00Z",
            "game_id": game_id,
            "game_external_id": ext_id,
            "league": "NBA",
            "home_team": "LAL",
            "away_team": "BOS",
            "home_score": 112,
            "away_score": 104,
            "total": 216,
            "margin": 8,
            "overtime": False,
        }
        redis_client = sync_redis.Redis.from_url(redis_url, decode_responses=True)
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe("events:bet.graded")
        deadline = time.monotonic() + 15.0
        data = None
        while time.monotonic() < deadline:
            redis_client.publish("events:game.completed", json.dumps(payload))
            detail = client.get(f"/api/v1/emulator/bets/{bet_id}").json()["data"]
            if detail["result"] != "PENDING":
                data = detail
                break
            time.sleep(0.25)

        assert data is not None, "bet was never graded from the event"
        assert data["result"] == "WIN"
        assert data["grade"]["result_description"] == "LAL won by 8, covering -3.5"
        assert data["grade"]["game_result_id"] is None  # event path has no stats result id
        assert data["clv"] == pytest.approx(0.03318, abs=1e-4)

        # the event-driven grade also fans out a bet.graded event
        graded_event = wait_for_message(pubsub)
        pubsub.close()
        redis_client.close()
        assert graded_event is not None, "no bet.graded event received"
        graded_payload = json.loads(graded_event["data"])
        assert graded_payload["bet_id"] == bet_id
        assert graded_payload["result"] == "WIN"


class TestPoller:
    async def test_run_once_grades_started_final_games(self, migrated_database_url, upstream) -> None:
        engine = create_engine(migrated_database_url)
        repo = PaperBetRepository(engine)
        game_id = uuid.uuid4()
        ext_id = f"odds-{uuid.uuid4().hex}"
        bet, created = await repo.insert_idempotent(
            {
                "game_id": game_id,
                "game_external_id": ext_id,
                "league": "NBA",
                "market_type": "SPREAD",
                "selection": "Los Angeles Lakers -3.5",
                "side": "HOME",
                "line_value": -3.5,
                "sportsbook_id": None,
                "sportsbook_key": "pinnacle",
                "odds_american": -105,
                "odds_decimal": 1.952,
                "stake": 1.0,
                "predicted_probability": 0.55,
                "edge_at_placement": 0.042,
                "kelly_fraction": 0.25,
                "reasoning": None,
                "prediction_id": None,
                "edge_id": None,
                "idempotency_key": str(uuid.uuid4()),
                "game_start_at": datetime.now(tz=UTC) - timedelta(hours=4),
            }
        )
        assert created

        result_id = str(uuid.uuid4())
        mock_final_game(upstream, str(game_id), 112, 104, result_id=result_id)
        upstream.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/closing").mock(return_value=Response(500))

        http_client = httpx.AsyncClient()
        settings = Settings(starting_bankroll_units=100.0)
        grader = GraderService(
            StatisticsClient(STATS_URL, http_client), LinesClient(LINES_URL, http_client), repo, settings
        )
        poller = GradingPoller(
            StatisticsClient(STATS_URL, http_client), repo, grader, poll_seconds=10_000, grace_seconds=10_800
        )
        try:
            assert await poller.run_once() == 1
            found = await repo.get_with_grade(bet.id)
            assert found is not None
            graded_bet, grade = found
            assert graded_bet.status == "WON"
            assert grade is not None
            assert grade.game_result_id == uuid.UUID(result_id)
            assert grade.clv is None  # closing lines were down: graded anyway
            # already graded: a second sweep finds nothing
            assert await poller.run_once() == 0
        finally:
            await http_client.aclose()
            await engine.dispose()

    async def test_grading_succeeds_when_event_publish_fails(self, migrated_database_url, upstream) -> None:
        """A broken Redis must never block grading: publish is fire-and-forget."""
        engine = create_engine(migrated_database_url)
        repo = PaperBetRepository(engine)
        game_id = uuid.uuid4()
        ext_id = f"odds-{uuid.uuid4().hex}"
        bet, created = await repo.insert_idempotent(
            {
                "game_id": game_id,
                "game_external_id": ext_id,
                "league": "NBA",
                "market_type": "SPREAD",
                "selection": "Los Angeles Lakers -3.5",
                "side": "HOME",
                "line_value": -3.5,
                "sportsbook_id": None,
                "sportsbook_key": "pinnacle",
                "odds_american": -105,
                "odds_decimal": 1.952,
                "stake": 1.0,
                "predicted_probability": 0.55,
                "edge_at_placement": 0.042,
                "kelly_fraction": 0.25,
                "reasoning": None,
                "prediction_id": None,
                "edge_id": None,
                "idempotency_key": str(uuid.uuid4()),
                "game_start_at": datetime.now(tz=UTC) - timedelta(hours=4),
            }
        )
        assert created
        mock_final_game(upstream, str(game_id), 112, 104)
        upstream.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/closing").mock(return_value=Response(500))

        # port 1 is unroutable: every publish raises, and must be swallowed
        dead_redis = aioredis.Redis.from_url("redis://127.0.0.1:1", socket_connect_timeout=0.2)
        http_client = httpx.AsyncClient()
        grader = GraderService(
            StatisticsClient(STATS_URL, http_client),
            LinesClient(LINES_URL, http_client),
            repo,
            Settings(starting_bankroll_units=100.0),
            redis_client=dead_redis,
        )
        try:
            assert await grader.grade_game(str(game_id), 112, 104) == 1
            found = await repo.get_with_grade(bet.id)
            assert found is not None and found[0].status == "WON"
        finally:
            await http_client.aclose()
            await dead_redis.aclose()
            await engine.dispose()

    async def test_run_once_survives_stats_failures(self, migrated_database_url, upstream) -> None:
        engine = create_engine(migrated_database_url)
        repo = PaperBetRepository(engine)
        game_id = uuid.uuid4()
        await repo.insert_idempotent(
            {
                "game_id": game_id,
                "game_external_id": "odds-down",
                "league": "NBA",
                "market_type": "MONEYLINE",
                "selection": "Los Angeles Lakers",
                "side": "HOME",
                "line_value": None,
                "sportsbook_id": None,
                "sportsbook_key": "betmgm",
                "odds_american": 120,
                "odds_decimal": 2.2,
                "stake": 1.0,
                "predicted_probability": 0.55,
                "edge_at_placement": 0.03,
                "kelly_fraction": 0.25,
                "reasoning": None,
                "prediction_id": None,
                "edge_id": None,
                "idempotency_key": str(uuid.uuid4()),
                "game_start_at": datetime.now(tz=UTC) - timedelta(hours=4),
            }
        )
        upstream.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(return_value=Response(500))

        http_client = httpx.AsyncClient()
        grader = GraderService(
            StatisticsClient(STATS_URL, http_client), LinesClient(LINES_URL, http_client), repo, Settings()
        )
        poller = GradingPoller(
            StatisticsClient(STATS_URL, http_client), repo, grader, poll_seconds=10_000, grace_seconds=10_800
        )
        try:
            assert await poller.run_once() == 0  # logged, not raised
        finally:
            await http_client.aclose()
            await engine.dispose()


def soccer_moneyline_values(game_id: uuid.UUID, ext_id: str, side: str, selection: str) -> dict[str, Any]:
    return {
        "game_id": game_id,
        "game_external_id": ext_id,
        "league": "FIFA_WC",
        "market_type": "MONEYLINE",
        "selection": selection,
        "side": side,
        "line_value": None,
        "sportsbook_id": None,
        "sportsbook_key": "betmgm",
        "odds_american": 230,
        "odds_decimal": 3.3,
        "stake": 1.0,
        "predicted_probability": 0.35,
        "edge_at_placement": 0.042,
        "kelly_fraction": 0.25,
        "reasoning": None,
        "prediction_id": None,
        "edge_id": None,
        "idempotency_key": str(uuid.uuid4()),
        "game_start_at": datetime.now(tz=UTC) - timedelta(hours=4),
    }


class TestSoccerThreeWayGrading:
    """ADR-026/027: DRAW bets, three-way grading, regulation-score settlement."""

    def test_event_grades_draw_bet_on_regulation_score(self, client, upstream, redis_url) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_soccer_best_lines(upstream, ext_id)
        mock_closing(upstream, ext_id)
        placed = place_bet(client, game_id, ext_id, market_type="MONEYLINE", selection="Draw", side="DRAW")
        assert placed.status_code == 201, placed.text
        bet_id = placed.json()["data"]["id"]

        # 2-2 after 90 minutes, 3-2 after extra time: the DRAW bet WINS
        payload = {
            "event": "game.completed",
            "timestamp": "2026-07-05T22:15:00Z",
            "game_id": game_id,
            "game_external_id": ext_id,
            "league": "FIFA_WC",
            "home_team": "ARG",
            "away_team": "FRA",
            "home_score": 3,
            "away_score": 2,
            "total": 5,
            "margin": 1,
            "overtime": True,
            "regulation_home_score": 2,
            "regulation_away_score": 2,
        }
        redis_client = sync_redis.Redis.from_url(redis_url, decode_responses=True)
        deadline = time.monotonic() + 15.0
        data = None
        while time.monotonic() < deadline:
            redis_client.publish("events:game.completed", json.dumps(payload))
            detail = client.get(f"/api/v1/emulator/bets/{bet_id}").json()["data"]
            if detail["result"] != "PENDING":
                data = detail
                break
            time.sleep(0.25)
        redis_client.close()

        assert data is not None, "bet was never graded from the event"
        assert data["result"] == "WIN"
        grade = data["grade"]
        assert grade["actual_home_score"] == 2  # regulation, not final
        assert grade["actual_away_score"] == 2
        assert grade["actual_margin"] == 0
        assert grade["actual_total"] == 4
        assert grade["result_description"] == "Game tied"

    async def test_poller_tie_wins_draw_and_loses_home_without_push(self, migrated_database_url, upstream) -> None:
        engine = create_engine(migrated_database_url)
        repo = PaperBetRepository(engine)
        game_id = uuid.uuid4()
        ext_id = f"odds-{uuid.uuid4().hex}"
        # the DRAW insert exercises the widened chk_paper_bets_side constraint
        # and the FIFA_WC league_enum value on a real Postgres
        draw_bet, created = await repo.insert_idempotent(soccer_moneyline_values(game_id, ext_id, "DRAW", "Draw"))
        assert created
        home_bet, created = await repo.insert_idempotent(soccer_moneyline_values(game_id, ext_id, "HOME", "Argentina"))
        assert created

        # regulation fields absent: settlement falls back to the 2-2 final
        mock_final_game(upstream, str(game_id), 2, 2, league="FIFA_WC")
        upstream.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/closing").mock(return_value=Response(500))

        http_client = httpx.AsyncClient()
        settings = Settings(starting_bankroll_units=100.0)
        grader = GraderService(
            StatisticsClient(STATS_URL, http_client), LinesClient(LINES_URL, http_client), repo, settings
        )
        poller = GradingPoller(
            StatisticsClient(STATS_URL, http_client), repo, grader, poll_seconds=10_000, grace_seconds=10_800
        )
        try:
            assert await poller.run_once() == 2
            found = await repo.get_with_grade(draw_bet.id)
            assert found is not None and found[0].status == "WON"
            found = await repo.get_with_grade(home_bet.id)
            assert found is not None and found[0].status == "LOST"  # three-way: a tie is not a PUSH
        finally:
            await http_client.aclose()
            await engine.dispose()


class TestHealth:
    def test_health_is_200_even_when_degraded(self, client, upstream) -> None:
        upstream.get(f"{STATS_URL}/api/v1/stats/health").mock(return_value=Response(500))
        upstream.get(f"{LINES_URL}/api/v1/lines/health").mock(return_value=Response(500))
        response = client.get("/api/v1/emulator/health")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "degraded"
        assert data["service"] == "bookie-emulator"
        assert data["dependencies"]["postgres"] == "healthy"
        assert data["dependencies"]["redis"] == "healthy"
        assert data["dependencies"]["lines_service"] == "unhealthy"
        assert data["dependencies"]["statistics_service"] == "unhealthy"
        assert data["subscriber"] in {"running", "reconnecting"}
        assert data["stats"]["open_bets"] >= 0

    def test_health_healthy_when_dependencies_up(self, client, upstream) -> None:
        upstream.get(f"{STATS_URL}/api/v1/stats/health").mock(
            return_value=Response(200, json=enveloped({"status": "healthy"}))
        )
        upstream.get(f"{LINES_URL}/api/v1/lines/health").mock(
            return_value=Response(200, json=enveloped({"status": "healthy"}))
        )
        response = client.get("/api/v1/emulator/health")
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "healthy"
