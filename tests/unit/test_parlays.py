"""Phase 7 Wave 1: parlay math, settlement matrix, validators, bet_class."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from bookie_emulator.api.errors import UnprocessableError
from bookie_emulator.api.schemas import ParlayLegRequest, PlaceParlayRequest
from bookie_emulator.core.grading import profit_loss
from bookie_emulator.core.odds import decimal_to_american
from bookie_emulator.core.parlay import combined_decimal, leg_outcome_summary, settle_parlay
from bookie_emulator.core.performance import GradedBet, bet_class, compute_breakdown
from bookie_emulator.services.bets import validate_parlay_legs


def leg(**overrides: Any) -> ParlayLegRequest:
    defaults: dict[str, Any] = {
        "game_id": uuid.uuid4(),
        "market_type": "MONEYLINE",
        "selection": "Argentina",
        "side": "HOME",
    }
    defaults.update(overrides)
    return ParlayLegRequest(**defaults)


class TestCombinedOdds:
    def test_combined_decimal_is_product(self) -> None:
        assert combined_decimal([2.0, 2.0]) == pytest.approx(4.0)
        assert combined_decimal([1.952, 2.2]) == pytest.approx(4.2944)
        assert combined_decimal([1.909]) == pytest.approx(1.909)

    def test_empty_legs_raise(self) -> None:
        with pytest.raises(ValueError, match="at least one leg"):
            combined_decimal([])

    def test_combined_american_via_conversion(self) -> None:
        assert decimal_to_american(combined_decimal([2.0, 2.0])) == 300
        assert decimal_to_american(combined_decimal([1.5, 1.2])) == -125


class TestSettleParlay:
    def test_open_leg_defers_settlement(self) -> None:
        assert settle_parlay(["WON", "OPEN"], [2.0, 2.0]) is None

    def test_all_won(self) -> None:
        assert settle_parlay(["WON", "WON"], [2.0, 1.6]) == ("WON", pytest.approx(3.2))

    def test_any_lost_loses(self) -> None:
        status, _ = settle_parlay(["WON", "LOST", "WON"], [2.0, 1.9, 1.6]) or ("", 0.0)
        assert status == "LOST"

    def test_push_leg_reprices_over_survivors(self) -> None:
        assert settle_parlay(["WON", "PUSH", "WON"], [2.0, 1.9, 1.6]) == ("WON", pytest.approx(3.2))

    def test_void_leg_reprices_like_push(self) -> None:
        assert settle_parlay(["WON", "VOID"], [2.2, 1.9]) == ("WON", pytest.approx(2.2))

    def test_all_push_or_void_is_push(self) -> None:
        assert settle_parlay(["PUSH", "VOID"], [2.0, 1.9]) == ("PUSH", 1.0)

    def test_lost_beats_push(self) -> None:
        status, _ = settle_parlay(["PUSH", "LOST"], [2.0, 1.9]) or ("", 0.0)
        assert status == "LOST"

    def test_misaligned_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="align"):
            settle_parlay(["WON"], [2.0, 1.9])


class TestParlayProfitLoss:
    """Parent P/L reuses core grading: WON pays stake x (repriced - 1)."""

    def test_won_pays_repriced_decimal(self) -> None:
        assert profit_loss("WON", 1.5, 3.2) == pytest.approx(1.5 * 2.2)

    def test_lost_forfeits_stake(self) -> None:
        assert profit_loss("LOST", 1.5, 3.2) == -1.5

    def test_push_refunds_stake(self) -> None:
        assert profit_loss("PUSH", 1.5, 1.0) == 0.0


class TestLegOutcomeSummary:
    def test_repriced_won_summary(self) -> None:
        assert leg_outcome_summary(["WON", "WON", "PUSH"], "WON", 3.2) == "Legs: W-W-PUSH (re-priced 3.20)"

    def test_clean_won_omits_reprice(self) -> None:
        assert leg_outcome_summary(["WON", "WON"], "WON", 4.0) == "Legs: W-W"

    def test_lost_summary(self) -> None:
        assert leg_outcome_summary(["WON", "LOST"], "LOST", 2.0) == "Legs: W-L"

    def test_push_summary_mentions_refund(self) -> None:
        summary = leg_outcome_summary(["PUSH", "VOID"], "PUSH", 1.0)
        assert summary == "Legs: PUSH-VOID (all legs push/void: stake refunded)"


class TestPlaceParlayRequestValidation:
    """Structural rules live on the schema (400 VALIDATION_ERROR at the API)."""

    def _request(self, legs: list[ParlayLegRequest]) -> PlaceParlayRequest:
        return PlaceParlayRequest(legs=legs, predicted_probability=0.2, edge_percentage=3.0, stake=1.0)

    def test_two_to_six_legs_accepted(self) -> None:
        assert len(self._request([leg(), leg()]).legs) == 2
        assert len(self._request([leg() for _ in range(6)]).legs) == 6

    def test_single_leg_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._request([leg()])

    def test_seven_legs_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._request([leg() for _ in range(7)])

    def test_draw_requires_moneyline(self) -> None:
        with pytest.raises(ValidationError, match="only valid for MONEYLINE"):
            leg(market_type="SPREAD", side="DRAW")

    def test_draw_moneyline_accepted(self) -> None:
        assert leg(side="DRAW", selection="Draw").side == "DRAW"


class TestValidateParlayLegs:
    """Business rules raise UnprocessableError (pinned contract: 422)."""

    @pytest.mark.parametrize("market_type", ["PLAYER_PROP", "TEAM_PROP", "GAME_PROP"])
    def test_prop_markets_rejected(self, market_type: str) -> None:
        legs = [leg(), leg(market_type=market_type, side="OVER")]
        with pytest.raises(UnprocessableError, match="only SPREAD, TOTAL, and MONEYLINE"):
            validate_parlay_legs(legs)

    def test_duplicate_leg_rejected(self) -> None:
        game_id = uuid.uuid4()
        legs = [leg(game_id=game_id), leg(game_id=game_id)]
        with pytest.raises(UnprocessableError, match="duplicate or opposite-side"):
            validate_parlay_legs(legs)

    def test_opposite_sides_same_market_rejected(self) -> None:
        game_id = uuid.uuid4()
        legs = [
            leg(game_id=game_id, market_type="TOTAL", side="OVER", selection="Over 2.5"),
            leg(game_id=game_id, market_type="TOTAL", side="UNDER", selection="Under 2.5"),
        ]
        with pytest.raises(UnprocessableError, match="duplicate or opposite-side"):
            validate_parlay_legs(legs)

    def test_draw_conflicts_with_home_on_three_way(self) -> None:
        game_id = uuid.uuid4()
        legs = [leg(game_id=game_id, side="HOME"), leg(game_id=game_id, side="DRAW", selection="Draw")]
        with pytest.raises(UnprocessableError, match="duplicate or opposite-side"):
            validate_parlay_legs(legs)

    def test_same_game_different_markets_allowed(self) -> None:
        game_id = uuid.uuid4()
        validate_parlay_legs(
            [
                leg(game_id=game_id, market_type="SPREAD", selection="Lakers -3.5"),
                leg(game_id=game_id, market_type="TOTAL", side="OVER", selection="Over 220.5"),
            ]
        )

    def test_distinct_games_allowed(self) -> None:
        validate_parlay_legs([leg(), leg()])


def graded(**overrides: Any) -> GradedBet:
    defaults: dict[str, Any] = {
        "league": "NBA",
        "market_type": "SPREAD",
        "sportsbook_key": "draftkings",
        "stake": 1.0,
        "odds_decimal": 1.909,
        "predicted_probability": 0.55,
        "edge_at_placement": 0.04,
        "status": "WON",
        "profit_loss": 0.909,
        "clv": None,
        "placed_at": datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        "graded_at": datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return GradedBet(**defaults)


class TestBetClass:
    """single | parlay | prop | live with precedence parlay > prop > live."""

    def test_default_is_single(self) -> None:
        assert bet_class(graded()) == "single"

    def test_parlay_flag_wins(self) -> None:
        assert bet_class(graded(is_parlay=True)) == "parlay"

    def test_parlay_beats_prop_and_live(self) -> None:
        assert bet_class(graded(is_parlay=True, is_live=True, market_type="PLAYER_PROP")) == "parlay"

    @pytest.mark.parametrize("market_type", ["PLAYER_PROP", "TEAM_PROP", "GAME_PROP"])
    def test_prop_markets_are_prop(self, market_type: str) -> None:
        assert bet_class(graded(market_type=market_type)) == "prop"

    def test_prop_beats_live(self) -> None:
        assert bet_class(graded(market_type="PLAYER_PROP", is_live=True)) == "prop"

    def test_live_flag(self) -> None:
        assert bet_class(graded(is_live=True)) == "live"

    def test_breakdown_groups_by_bet_class(self) -> None:
        bets = [
            graded(),
            graded(is_parlay=True, odds_decimal=4.29, profit_loss=3.29),
            graded(is_parlay=True, status="LOST", profit_loss=-1.0),
            graded(market_type="PLAYER_PROP"),
        ]
        groups = {g.group: g for g in compute_breakdown(bets, "bet_class")}
        assert set(groups) == {"single", "parlay", "prop"}
        assert groups["parlay"].total_bets == 2
        assert groups["parlay"].wins == 1
        assert groups["parlay"].losses == 1
        assert groups["parlay"].total_profit_units == pytest.approx(2.29)
