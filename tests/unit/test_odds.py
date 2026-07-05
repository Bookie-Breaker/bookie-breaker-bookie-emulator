"""Odds conversion round-trips for both American odds signs."""

import pytest

from bookie_emulator.core.odds import (
    american_to_decimal,
    decimal_to_american,
    implied_probability,
    implied_probability_decimal,
)


class TestConversions:
    def test_negative_american_to_decimal(self) -> None:
        assert american_to_decimal(-110) == pytest.approx(1.9091, abs=1e-4)

    def test_positive_american_to_decimal(self) -> None:
        assert american_to_decimal(150) == 2.5

    def test_even_money(self) -> None:
        assert american_to_decimal(100) == 2.0
        assert american_to_decimal(-100) == 2.0

    @pytest.mark.parametrize("odds", [-500, -200, -150, -110, -105, 100, 105, 110, 150, 200, 500])
    def test_round_trip_both_signs(self, odds: int) -> None:
        assert decimal_to_american(american_to_decimal(odds)) == odds

    def test_zero_american_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot be 0"):
            american_to_decimal(0)

    def test_decimal_at_or_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="greater than 1"):
            decimal_to_american(1.0)


class TestImpliedProbability:
    def test_favorite(self) -> None:
        assert implied_probability(-110) == pytest.approx(0.5238, abs=1e-4)

    def test_underdog(self) -> None:
        assert implied_probability(150) == pytest.approx(0.4)

    def test_from_decimal(self) -> None:
        assert implied_probability_decimal(2.0) == 0.5

    def test_from_decimal_rejects_invalid(self) -> None:
        with pytest.raises(ValueError, match="greater than 1"):
            implied_probability_decimal(0.9)
