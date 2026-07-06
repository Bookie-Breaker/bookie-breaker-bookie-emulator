"""Closing-line matching preference and CLV sign convention."""

import pytest

from bookie_emulator.clients.lines import LineSnapshot
from bookie_emulator.core.clv import compute_clv, match_closing_line


def snapshot(
    sportsbook_key: str, market_type: str = "SPREAD", side: str = "HOME", line_value: float | None = -3.5
) -> LineSnapshot:
    return LineSnapshot(
        id=f"snap-{sportsbook_key}-{market_type}-{side}",
        game_id="odds-1",
        sportsbook_key=sportsbook_key,
        market_type=market_type,
        side=side,
        line_value=line_value,
        odds_american=-110,
        odds_decimal=1.909,
        is_closing=True,
    )


class TestMatchClosingLine:
    def test_prefers_same_sportsbook(self) -> None:
        closing = [snapshot("fanduel"), snapshot("draftkings")]
        match = match_closing_line(closing, "SPREAD", "HOME", "draftkings", -3.5)
        assert match is not None
        assert match.sportsbook_key == "draftkings"

    def test_falls_back_to_nearest_line_value(self) -> None:
        closing = [snapshot("fanduel", line_value=-5.0), snapshot("betmgm", line_value=-4.0)]
        match = match_closing_line(closing, "SPREAD", "HOME", "pinnacle", -3.5)
        assert match is not None
        assert match.sportsbook_key == "betmgm"

    def test_requires_same_market_and_side(self) -> None:
        closing = [snapshot("draftkings", market_type="TOTAL", side="OVER"), snapshot("draftkings", side="AWAY")]
        assert match_closing_line(closing, "SPREAD", "HOME", "draftkings", -3.5) is None

    def test_empty_closing_lines(self) -> None:
        assert match_closing_line([], "SPREAD", "HOME", "draftkings", -3.5) is None

    def test_moneyline_matches_without_line_values(self) -> None:
        closing = [snapshot("fanduel", market_type="MONEYLINE", line_value=None)]
        match = match_closing_line(closing, "MONEYLINE", "HOME", "pinnacle", None)
        assert match is not None

    def _three_way_closing(self) -> list[LineSnapshot]:
        return [
            snapshot("pinnacle", market_type="MONEYLINE", side="HOME", line_value=None),
            snapshot("pinnacle", market_type="MONEYLINE", side="DRAW", line_value=None),
            snapshot("pinnacle", market_type="MONEYLINE", side="AWAY", line_value=None),
        ]

    def test_draw_bet_matches_draw_side_in_three_way_market(self) -> None:
        match = match_closing_line(self._three_way_closing(), "MONEYLINE", "DRAW", "pinnacle", None)
        assert match is not None
        assert match.side == "DRAW"

    def test_home_bet_ignores_draw_row_in_three_way_market(self) -> None:
        match = match_closing_line(self._three_way_closing(), "MONEYLINE", "HOME", "pinnacle", None)
        assert match is not None
        assert match.side == "HOME"

    def test_draw_bet_finds_nothing_in_two_way_market(self) -> None:
        two_way = [
            snapshot("pinnacle", market_type="MONEYLINE", side="HOME", line_value=None),
            snapshot("pinnacle", market_type="MONEYLINE", side="AWAY", line_value=None),
        ]
        assert match_closing_line(two_way, "MONEYLINE", "DRAW", "pinnacle", None) is None


class TestComputeClv:
    def test_positive_when_market_moved_toward_bet(self) -> None:
        # placed at -110 (0.5238 implied), closed at -120 (0.5455 implied)
        assert compute_clv(-110, -120) == pytest.approx(0.02165, abs=1e-4)

    def test_negative_when_market_moved_away(self) -> None:
        # placed at -110, closed at +100 (0.5 implied)
        assert compute_clv(-110, 100) == pytest.approx(-0.02381, abs=1e-4)

    def test_zero_when_unchanged(self) -> None:
        assert compute_clv(-110, -110) == 0.0
