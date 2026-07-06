"""Grading matrix: every market x side x outcome, including pushes."""

import pytest

from bookie_emulator.core.grading import grade_bet, profit_loss


class TestSpread:
    # home wins 112-104 (margin +8)
    def test_home_favorite_covers(self) -> None:
        status, description = grade_bet("SPREAD", "HOME", -3.5, 112, 104, "LAL", "BOS")
        assert status == "WON"
        assert description == "LAL won by 8, covering -3.5"

    def test_home_favorite_fails_to_cover(self) -> None:
        status, description = grade_bet("SPREAD", "HOME", -8.5, 112, 104, "LAL", "BOS")
        assert status == "LOST"
        assert description == "LAL won by 8, failing to cover -8.5"

    def test_home_push_on_the_number(self) -> None:
        status, description = grade_bet("SPREAD", "HOME", -8.0, 112, 104, "LAL", "BOS")
        assert status == "PUSH"
        assert "push on -8" in description

    def test_away_dog_covers(self) -> None:
        # away +8.5 while losing by 8: selected_margin -8 + 8.5 > 0
        status, _ = grade_bet("SPREAD", "AWAY", 8.5, 112, 104)
        assert status == "WON"

    def test_away_dog_fails_to_cover(self) -> None:
        status, _ = grade_bet("SPREAD", "AWAY", 3.5, 112, 104)
        assert status == "LOST"

    def test_away_push(self) -> None:
        status, _ = grade_bet("SPREAD", "AWAY", 8.0, 112, 104)
        assert status == "PUSH"

    def test_away_favorite_covers_when_away_wins_big(self) -> None:
        # away -6.5, away wins by 10: selected_margin 10 - 6.5 > 0
        status, description = grade_bet("SPREAD", "AWAY", -6.5, 100, 110, "LAL", "BOS")
        assert status == "WON"
        assert description == "BOS won by 10, covering -6.5"

    def test_home_dog_covers_while_losing(self) -> None:
        status, description = grade_bet("SPREAD", "HOME", 5.5, 104, 107, "LAL", "BOS")
        assert status == "WON"
        assert description == "BOS won by 3, covering +5.5"

    def test_generic_team_names(self) -> None:
        _, description = grade_bet("SPREAD", "HOME", -3.5, 112, 104)
        assert description == "Home won by 8, covering -3.5"


class TestTotal:
    def test_over_wins(self) -> None:
        status, description = grade_bet("TOTAL", "OVER", 210.5, 112, 104)
        assert status == "WON"
        assert description == "Game landed 216, over 210.5"

    def test_over_loses(self) -> None:
        status, description = grade_bet("TOTAL", "OVER", 224.5, 112, 107)
        assert status == "LOST"
        assert description == "Game landed 219, under 224.5"

    def test_over_push(self) -> None:
        status, description = grade_bet("TOTAL", "OVER", 216.0, 112, 104)
        assert status == "PUSH"
        assert description == "Game landed 216, push on 216"

    def test_under_wins(self) -> None:
        status, _ = grade_bet("TOTAL", "UNDER", 224.5, 112, 104)
        assert status == "WON"

    def test_under_loses(self) -> None:
        status, _ = grade_bet("TOTAL", "UNDER", 210.5, 112, 104)
        assert status == "LOST"

    def test_under_push(self) -> None:
        status, _ = grade_bet("TOTAL", "UNDER", 216.0, 112, 104)
        assert status == "PUSH"


class TestMoneyline:
    def test_home_wins(self) -> None:
        status, description = grade_bet("MONEYLINE", "HOME", None, 112, 104, "LAL", "BOS")
        assert status == "WON"
        assert description == "LAL won by 8"

    def test_home_loses(self) -> None:
        status, _ = grade_bet("MONEYLINE", "HOME", None, 104, 112)
        assert status == "LOST"

    def test_away_wins(self) -> None:
        status, description = grade_bet("MONEYLINE", "AWAY", None, 104, 112, "LAL", "BOS")
        assert status == "WON"
        assert description == "BOS won by 8"

    def test_away_loses(self) -> None:
        status, _ = grade_bet("MONEYLINE", "AWAY", None, 112, 104)
        assert status == "LOST"

    def test_tie_pushes_both_sides(self) -> None:
        assert grade_bet("MONEYLINE", "HOME", None, 3, 3)[0] == "PUSH"
        assert grade_bet("MONEYLINE", "AWAY", None, 3, 3)[0] == "PUSH"


class TestThreeWayMoneyline:
    """ADR-027 truth table: every side x margin sign, with no push path."""

    @pytest.mark.parametrize(
        ("side", "home_score", "away_score", "expected"),
        [
            ("HOME", 2, 1, "WON"),
            ("HOME", 1, 2, "LOST"),
            ("HOME", 1, 1, "LOST"),
            ("AWAY", 2, 1, "LOST"),
            ("AWAY", 1, 2, "WON"),
            ("AWAY", 1, 1, "LOST"),
            ("DRAW", 2, 1, "LOST"),
            ("DRAW", 1, 2, "LOST"),
            ("DRAW", 1, 1, "WON"),
        ],
    )
    def test_truth_table(self, side: str, home_score: int, away_score: int, expected: str) -> None:
        status, _ = grade_bet("MONEYLINE", side, None, home_score, away_score, three_way_moneyline=True)
        assert status == expected

    def test_tie_never_pushes_any_side(self) -> None:
        for side in ("HOME", "AWAY", "DRAW"):
            status, _ = grade_bet("MONEYLINE", side, None, 0, 0, three_way_moneyline=True)
            assert status != "PUSH"

    def test_descriptions(self) -> None:
        status, description = grade_bet("MONEYLINE", "DRAW", None, 2, 2, "ARG", "FRA", three_way_moneyline=True)
        assert status == "WON"
        assert description == "Game tied"
        status, description = grade_bet("MONEYLINE", "HOME", None, 3, 1, "ARG", "FRA", three_way_moneyline=True)
        assert status == "WON"
        assert description == "ARG won by 2"

    def test_two_way_tie_still_pushes(self) -> None:
        # the flag defaults off: existing two-way behavior is untouched
        assert grade_bet("MONEYLINE", "HOME", None, 3, 3)[0] == "PUSH"
        assert grade_bet("MONEYLINE", "HOME", None, 3, 3, three_way_moneyline=False)[0] == "PUSH"


class TestDrawSideRejection:
    def test_draw_rejected_on_spread(self) -> None:
        with pytest.raises(ValueError, match="DRAW"):
            grade_bet("SPREAD", "DRAW", -0.5, 1, 1)

    def test_draw_rejected_on_total(self) -> None:
        with pytest.raises(ValueError, match="DRAW"):
            grade_bet("TOTAL", "DRAW", 2.5, 1, 1)

    def test_draw_rejected_on_two_way_moneyline(self) -> None:
        with pytest.raises(ValueError, match="DRAW"):
            grade_bet("MONEYLINE", "DRAW", None, 1, 1)

    def test_draw_rejected_on_spread_even_with_three_way_flag(self) -> None:
        with pytest.raises(ValueError, match="DRAW"):
            grade_bet("SPREAD", "DRAW", -0.5, 1, 1, three_way_moneyline=True)


class TestUnsupportedMarket:
    def test_prop_market_raises(self) -> None:
        with pytest.raises(ValueError, match="PLAYER_PROP"):
            grade_bet("PLAYER_PROP", "HOME", None, 112, 104)


class TestProfitLoss:
    def test_win_pays_decimal_minus_one(self) -> None:
        assert profit_loss("WON", 1.5, 1.909) == pytest.approx(1.3635)

    def test_loss_forfeits_stake(self) -> None:
        assert profit_loss("LOST", 1.5, 1.909) == -1.5

    def test_push_and_void_return_stake(self) -> None:
        assert profit_loss("PUSH", 1.5, 1.909) == 0.0
        assert profit_loss("VOID", 1.5, 1.909) == 0.0
