"""Phase 7 Wave 0: nullable side/game_id guards, YES/NO vocabulary, prop fields.

Schema-level round-trips (migration 0003, parlay_legs) are covered in
tests/integration; these tests pin the pure/validator behavior.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from bookie_emulator.api.schemas import PlaceBetRequest, bet_to_data
from bookie_emulator.core.clv import match_closing_line
from bookie_emulator.core.grading import grade_bet
from bookie_emulator.db.repository import PaperBetRecord
from bookie_emulator.events.publisher import bet_graded_payload


def make_bet(**overrides: Any) -> PaperBetRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "game_id": uuid.uuid4(),
        "game_external_id": "odds-api-game-1",
        "league": "FIFA_WC",
        "market_type": "MONEYLINE",
        "selection": "Argentina",
        "side": "HOME",
        "line_value": None,
        "sportsbook_id": None,
        "sportsbook_key": "draftkings",
        "odds_american": -110,
        "odds_decimal": 1.909,
        "stake": 1.5,
        "predicted_probability": 0.56,
        "edge_at_placement": 0.04,
        "kelly_fraction": 0.08,
        "reasoning": None,
        "prediction_id": None,
        "edge_id": None,
        "idempotency_key": "key-1",
        "game_start_at": datetime(2026, 7, 10, 19, 0, tzinfo=UTC),
        "status": "OPEN",
        "placed_at": datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        "graded_at": None,
    }
    defaults.update(overrides)
    return PaperBetRecord(**defaults)


def make_request(**overrides: Any) -> PlaceBetRequest:
    defaults: dict[str, Any] = {
        "game_id": uuid.uuid4(),
        "market_type": "PLAYER_PROP",
        "selection": "Erling Haaland Over 2.5 Shots",
        "side": "OVER",
        "player_external_id": "espn-haaland",
        "stat_type": "player_shots",
        "prop_type": "OVER_UNDER",
        "predicted_probability": 0.58,
        "edge_percentage": 4.2,
        "stake": 1.0,
    }
    defaults.update(overrides)
    return PlaceBetRequest(**defaults)


class TestGradeBetNullSide:
    """A None side must raise, not silently grade as AWAY/UNDER."""

    @pytest.mark.parametrize("market_type", ["SPREAD", "TOTAL", "MONEYLINE"])
    def test_none_side_raises(self, market_type: str) -> None:
        with pytest.raises(ValueError, match="without a side"):
            grade_bet(market_type, None, -3.5, 2, 1)

    def test_sided_grading_unchanged(self) -> None:
        assert grade_bet("MONEYLINE", "HOME", None, 2, 1)[0] == "WON"


class TestPlaceBetRequestPropValidation:
    def test_player_prop_over_under_accepted(self) -> None:
        request = make_request()
        assert request.side == "OVER"
        assert request.stat_type == "player_shots"

    def test_yes_accepted_on_prop_markets(self) -> None:
        request = make_request(
            selection="Erling Haaland Anytime Goalscorer Yes",
            side="YES",
            stat_type="player_goal_scorer_anytime",
            prop_type="YES_NO",
        )
        assert request.side == "YES"

    @pytest.mark.parametrize("side", ["YES", "NO"])
    @pytest.mark.parametrize("market_type", ["SPREAD", "TOTAL", "MONEYLINE"])
    def test_yes_no_rejected_on_core_markets(self, side: str, market_type: str) -> None:
        with pytest.raises(ValidationError, match="only valid for prop markets"):
            make_request(market_type=market_type, side=side)

    def test_prop_market_requires_stat_type(self) -> None:
        with pytest.raises(ValidationError, match="require stat_type"):
            make_request(stat_type=None)

    def test_draw_still_moneyline_only(self) -> None:
        with pytest.raises(ValidationError, match="only valid for MONEYLINE"):
            make_request(market_type="SPREAD", side="DRAW", stat_type=None, prop_type=None)


class TestParlayParentGuards:
    def test_graded_payload_null_game_id(self) -> None:
        bet = make_bet(game_id=None, side=None, is_parlay=True)
        payload = bet_graded_payload(bet, "WON", {"profit_loss": 1.2, "clv": None})
        assert payload["game_id"] is None
        assert payload["bet_id"] == str(bet.id)

    def test_match_closing_line_none_side_matches_nothing(self) -> None:
        assert match_closing_line([], "MONEYLINE", None, "draftkings", None) is None

    def test_bet_to_data_serializes_null_side_and_prop_fields(self) -> None:
        bet = make_bet(
            game_id=None,
            side=None,
            is_parlay=True,
            player_external_id=None,
            stat_type=None,
        )
        data = bet_to_data(bet, None, unit_size_dollars=10.0)
        assert data.game_id is None
        assert data.side is None
        assert data.is_parlay is True

    def test_bet_to_data_carries_prop_fields(self) -> None:
        bet = make_bet(
            market_type="PLAYER_PROP",
            selection="Erling Haaland Over 2.5 Shots",
            side="OVER",
            line_value=2.5,
            player_external_id="espn-haaland",
            stat_type="player_shots",
            prop_type="OVER_UNDER",
        )
        data = bet_to_data(bet, None, unit_size_dollars=10.0)
        assert data.player_external_id == "espn-haaland"
        assert data.stat_type == "player_shots"
        assert data.prop_type == "OVER_UNDER"
