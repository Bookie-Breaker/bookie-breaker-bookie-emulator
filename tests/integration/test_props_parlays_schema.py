"""Phase 7 Wave 0 schema round-trips: migration 0003, YES/null sides, parlay legs.

Covers what unit tests cannot: the widened check constraints, nullable
side/game_id, prop columns, and the new parlay_legs table against a real
migrated Postgres (testcontainers). API-level PLAYER_PROP placement runs
through the full placement path with a mocked prop best-line.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import respx
from httpx import Response
from sqlalchemy import insert, select
from starlette.testclient import TestClient

from bookie_emulator.db.engine import create_engine
from bookie_emulator.db.repository import PaperBetRepository
from bookie_emulator.db.tables import paper_bets, parlay_legs
from tests.integration.conftest import LINES_URL, enveloped, future_start, mock_scheduled_game, place_bet


def _paper_bet_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "game_id": uuid.uuid4(),
        "game_external_id": f"ext-{uuid.uuid4().hex[:8]}",
        "league": "FIFA_WC",
        "market_type": "MONEYLINE",
        "selection": "Argentina",
        "side": "HOME",
        "sportsbook_key": "pinnacle",
        "odds_american": -110,
        "odds_decimal": 1.909,
        "stake": 1.0,
        "predicted_probability": 0.55,
        "edge_at_placement": 0.04,
        "kelly_fraction": 0.05,
        "idempotency_key": f"key-{uuid.uuid4().hex}",
    }
    values.update(overrides)
    return values


async def _insert_bet(engine: Any, values: dict[str, Any]) -> uuid.UUID:
    async with engine.begin() as conn:
        row = (await conn.execute(insert(paper_bets).values(**values).returning(paper_bets.c.id))).first()
    assert row is not None
    return row.id


class TestSchemaRoundTrips:
    async def test_yes_side_and_prop_columns_round_trip(self, migrated_database_url: str) -> None:
        engine = create_engine(migrated_database_url)
        try:
            bet_id = await _insert_bet(
                engine,
                _paper_bet_values(
                    market_type="PLAYER_PROP",
                    selection="Erling Haaland Anytime Goalscorer Yes",
                    side="YES",
                    player_external_id="espn-haaland",
                    stat_type="player_goal_scorer_anytime",
                    prop_type="YES_NO",
                ),
            )
            found = await PaperBetRepository(engine).get_with_grade(bet_id)
            assert found is not None
            bet = found[0]
            assert bet.side == "YES"
            assert bet.player_external_id == "espn-haaland"
            assert bet.stat_type == "player_goal_scorer_anytime"
            assert bet.prop_type == "YES_NO"
        finally:
            await engine.dispose()

    async def test_parlay_parent_null_side_and_game_id_with_legs(self, migrated_database_url: str) -> None:
        engine = create_engine(migrated_database_url)
        try:
            parent_id = await _insert_bet(
                engine,
                _paper_bet_values(
                    game_id=None,
                    game_external_id="parlay",
                    selection="2-leg same-game parlay",
                    side=None,
                    is_parlay=True,
                    odds_american=264,
                    odds_decimal=3.64,
                ),
            )
            legs = [
                ("MONEYLINE", "Argentina", "HOME"),
                ("TOTAL", "Over 2.5", "OVER"),
            ]
            async with engine.begin() as conn:
                for i, (market, selection, side) in enumerate(legs):
                    await conn.execute(
                        insert(parlay_legs).values(
                            bet_id=parent_id,
                            leg_index=i,
                            game_id=uuid.uuid4(),
                            game_external_id="wc-final",
                            league="FIFA_WC",
                            market_type=market,
                            selection=selection,
                            side=side,
                            odds_american=-110,
                            odds_decimal=1.909,
                        )
                    )
                rows = (
                    await conn.execute(
                        select(parlay_legs).where(parlay_legs.c.bet_id == parent_id).order_by(parlay_legs.c.leg_index)
                    )
                ).fetchall()
            assert [r.leg_index for r in rows] == [0, 1]
            assert all(r.leg_status == "OPEN" for r in rows)
        finally:
            await engine.dispose()

    async def test_parlay_parent_invisible_to_game_grading_paths(self, migrated_database_url: str) -> None:
        engine = create_engine(migrated_database_url)
        try:
            await _insert_bet(
                engine,
                _paper_bet_values(
                    game_id=None,
                    game_external_id="parlay",
                    side=None,
                    is_parlay=True,
                    game_start_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
                ),
            )
            repo = PaperBetRepository(engine)
            game_ids = await repo.open_game_ids_started_before(datetime.now(tz=UTC))
            assert None not in game_ids
        finally:
            await engine.dispose()


class TestPlayerPropPlacement:
    def test_place_player_prop_bet_via_api(self, client: TestClient, upstream: respx.MockRouter) -> None:
        game_id = str(uuid.uuid4())
        ext_id = "wc-semi-1"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        upstream.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(
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
        response = place_bet(
            client,
            game_id,
            ext_id,
            market_type="PLAYER_PROP",
            selection="Erling Haaland Over 2.5 Shots",
            side="OVER",
            player_external_id="espn-haaland",
            stat_type="player_shots",
            prop_type="OVER_UNDER",
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["market_type"] == "PLAYER_PROP"
        assert data["side"] == "OVER"
        assert data["player_external_id"] == "espn-haaland"
        assert data["stat_type"] == "player_shots"
        assert data["line_value"] == 2.5
