"""Integration fixtures: session-scoped Postgres + Redis containers.

The conftest replicates infra-ops init-db (emulator schema + public enums)
before running the Alembic migration, which is itself under test. Upstream
statistics/lines services are respx-mocked. Kept light for pre-push on
modest hardware.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from bookie_emulator.config import Settings
from bookie_emulator.main import create_app

STATS_URL = "http://stats.test"
LINES_URL = "http://lines.test"

INIT_SQL = """
CREATE SCHEMA IF NOT EXISTS emulator;
CREATE TYPE league_enum AS ENUM
    ('NFL', 'NBA', 'MLB', 'NCAA_FB', 'NCAA_BB', 'NCAA_BSB', 'FIFA_WC', 'EPL', 'NHL', 'NCAA_HKY');
CREATE TYPE market_type_enum AS ENUM
    ('SPREAD', 'TOTAL', 'MONEYLINE', 'PLAYER_PROP', 'TEAM_PROP', 'GAME_PROP', 'FUTURE', 'LIVE');
CREATE TYPE sport_enum AS ENUM ('FOOTBALL', 'BASKETBALL', 'BASEBALL', 'SOCCER', 'HOCKEY');
CREATE TYPE bet_result_enum AS ENUM ('OPEN', 'WON', 'LOST', 'PUSH', 'VOID');
"""


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        admin_url = f"postgresql://test:test@{host}:{port}/test"

        async def init_db() -> None:
            conn = await asyncpg.connect(admin_url)
            try:
                for statement in INIT_SQL.strip().split(";"):
                    if statement.strip():
                        await conn.execute(statement)
            finally:
                await conn.close()

        asyncio.run(init_db())
        yield f"postgres://test:test@{host}:{port}/test?search_path=emulator,public"


@pytest.fixture(scope="session")
def migrated_database_url(database_url: str) -> str:
    from alembic.config import Config

    from alembic import command

    os.environ["DATABASE_URL"] = database_url
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    return database_url


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}"


@pytest.fixture(scope="session")
def client(migrated_database_url: str, redis_url: str) -> Iterator[TestClient]:
    settings = Settings(
        database_url=migrated_database_url,
        redis_url=redis_url,
        statistics_service_url=STATS_URL,
        lines_service_url=LINES_URL,
        starting_bankroll_units=100.0,
        unit_size_dollars=100.0,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def upstream() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def enveloped(data: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"timestamp": "2026-07-04T12:00:00Z", "request_id": "req-test"}}


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def future_start(hours: int = 24) -> str:
    return iso(datetime.now(tz=UTC) + timedelta(hours=hours))


def game_payload(
    game_id: str,
    league: str = "NBA",
    status: str = "SCHEDULED",
    scheduled_start: str | None = None,
) -> dict[str, Any]:
    return {
        "id": game_id,
        "league": league,
        "status": status,
        "home_team": {"id": "team-home", "name": "Los Angeles Lakers", "abbreviation": "LAL"},
        "away_team": {"id": "team-away", "name": "Boston Celtics", "abbreviation": "BOS"},
        "scheduled_start": scheduled_start or future_start(),
        "season": 2026,
    }


def final_game_payload(
    game_id: str,
    home_score: int,
    away_score: int,
    league: str = "NBA",
    result_id: str | None = None,
    regulation_home_score: int | None = None,
    regulation_away_score: int | None = None,
) -> dict[str, Any]:
    payload = game_payload(game_id, league=league, status="FINAL", scheduled_start=iso(datetime.now(tz=UTC)))
    payload["home_score"] = home_score
    payload["away_score"] = away_score
    payload["result"] = {
        "id": result_id or str(uuid.uuid4()),
        "home_score": home_score,
        "away_score": away_score,
        "total_score": home_score + away_score,
        "margin": home_score - away_score,
        "overtime": False,
        "completed_at": iso(datetime.now(tz=UTC)),
    }
    # optional regulation-time fields (ADR-027): omitted entirely when absent,
    # mirroring the statistics-service payload
    if regulation_home_score is not None:
        payload["result"]["regulation_home_score"] = regulation_home_score
    if regulation_away_score is not None:
        payload["result"]["regulation_away_score"] = regulation_away_score
    return payload


def best_lines_payload(ext_id: str) -> list[dict[str, Any]]:
    return [
        {
            "market_type": "SPREAD",
            "selection": "Los Angeles Lakers -3.5",
            "side": "HOME",
            "line_value": -3.5,
            "best_odds_american": -105,
            "best_odds_decimal": 1.952,
            "implied_probability": 0.5122,
            "sportsbook_id": "00000000-0000-4000-8000-00000000aaaa",
            "sportsbook_key": "pinnacle",
            "timestamp": "2026-07-04T12:00:00Z",
        },
        {
            "market_type": "TOTAL",
            "selection": "Over 220.5",
            "side": "OVER",
            "line_value": 220.5,
            "best_odds_american": -108,
            "best_odds_decimal": 1.926,
            "implied_probability": 0.5192,
            "sportsbook_id": "00000000-0000-4000-8000-00000000bbbb",
            "sportsbook_key": "fanduel",
            "timestamp": "2026-07-04T12:00:00Z",
        },
        {
            "market_type": "MONEYLINE",
            "selection": "Los Angeles Lakers",
            "side": "HOME",
            "line_value": None,
            "best_odds_american": 120,
            "best_odds_decimal": 2.2,
            "implied_probability": 0.4545,
            "sportsbook_id": "00000000-0000-4000-8000-00000000cccc",
            "sportsbook_key": "betmgm",
            "timestamp": "2026-07-04T12:00:00Z",
        },
    ]


def soccer_best_lines_payload(ext_id: str) -> list[dict[str, Any]]:
    """Three-outcome moneyline market (ADR-027): HOME, DRAW, and AWAY rows."""
    common = {
        "market_type": "MONEYLINE",
        "line_value": None,
        "sportsbook_id": "00000000-0000-4000-8000-00000000cccc",
        "sportsbook_key": "betmgm",
        "timestamp": "2026-07-05T12:00:00Z",
    }
    return [
        {
            **common,
            "selection": "Los Angeles Lakers",
            "side": "HOME",
            "best_odds_american": 150,
            "best_odds_decimal": 2.5,
            "implied_probability": 0.4,
        },
        {
            **common,
            "selection": "Draw",
            "side": "DRAW",
            "best_odds_american": 230,
            "best_odds_decimal": 3.3,
            "implied_probability": 0.303,
        },
        {
            **common,
            "selection": "Boston Celtics",
            "side": "AWAY",
            "best_odds_american": 210,
            "best_odds_decimal": 3.1,
            "implied_probability": 0.3226,
        },
    ]


def game_lines_payload(ext_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": str(uuid.uuid4()),
            "game_id": ext_id,
            "sportsbook_id": "00000000-0000-4000-8000-00000000dddd",
            "sportsbook_key": "draftkings",
            "market_type": "SPREAD",
            "selection": "Los Angeles Lakers -3.5",
            "side": "HOME",
            "line_value": -3.5,
            "odds_american": -110,
            "odds_decimal": 1.909,
            "implied_probability": 0.5238,
            "timestamp": "2026-07-04T12:00:00Z",
        },
        {
            "id": str(uuid.uuid4()),
            "game_id": ext_id,
            "sportsbook_id": "00000000-0000-4000-8000-00000000eeee",
            "sportsbook_key": "fanduel",
            "market_type": "SPREAD",
            "selection": "Los Angeles Lakers -3",
            "side": "HOME",
            "line_value": -3.0,
            "odds_american": -108,
            "odds_decimal": 1.926,
            "implied_probability": 0.5192,
            "timestamp": "2026-07-04T12:00:00Z",
        },
    ]


def closing_lines_payload(ext_id: str, sportsbook_key: str = "pinnacle") -> list[dict[str, Any]]:
    return [
        {
            "id": str(uuid.uuid4()),
            "game_id": ext_id,
            "sportsbook_id": "00000000-0000-4000-8000-00000000aaaa",
            "sportsbook_key": sportsbook_key,
            "market_type": "SPREAD",
            "selection": "Los Angeles Lakers -4",
            "side": "HOME",
            "line_value": -4.0,
            "odds_american": -120,
            "odds_decimal": 1.833,
            "implied_probability": 0.5455,
            "is_closing": True,
            "timestamp": "2026-07-04T23:00:00Z",
        }
    ]


def mock_scheduled_game(router: respx.MockRouter, game_id: str, league: str = "NBA") -> None:
    router.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(
        return_value=Response(200, json=enveloped(game_payload(game_id, league=league)))
    )


def mock_best_lines(router: respx.MockRouter, ext_id: str) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(
        return_value=Response(200, json=enveloped(best_lines_payload(ext_id)))
    )


def mock_soccer_best_lines(router: respx.MockRouter, ext_id: str) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(
        return_value=Response(200, json=enveloped(soccer_best_lines_payload(ext_id)))
    )


def bet_body(game_id: str, ext_id: str | None = None, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "game_id": game_id,
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "predicted_probability": 0.5712,
        "edge_percentage": 4.2,
        "stake": 1.5,
    }
    if ext_id is not None:
        body["game_external_id"] = ext_id
    body.update(overrides)
    return body


def place_bet(
    client: TestClient,
    game_id: str,
    ext_id: str | None = None,
    idempotency_key: str | None = None,
    **overrides: Any,
) -> Any:
    return client.post(
        "/api/v1/emulator/bets",
        json=bet_body(game_id, ext_id, **overrides),
        headers={"X-Idempotency-Key": idempotency_key or str(uuid.uuid4())},
    )
