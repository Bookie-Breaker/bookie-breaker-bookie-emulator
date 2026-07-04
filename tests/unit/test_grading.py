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
