"""Closing Line Value: compare placement odds against the market close.

CLV is best-effort by design: when no closing line can be matched the bet
is graded anyway with NULL clv/closing fields.
"""

from collections.abc import Sequence

from bookie_emulator.clients.lines import LineSnapshot
from bookie_emulator.core.odds import implied_probability


def match_closing_line(
    closing_lines: Sequence[LineSnapshot],
    market_type: str,
    side: str | None,
    sportsbook_key: str,
    line_value: float | None,
) -> LineSnapshot | None:
    """Pick the closing snapshot to benchmark against.

    Preference order: the same sportsbook the bet was placed at, then any
    book offering the same market+side with the nearest line_value.
    """
    candidates = [s for s in closing_lines if s.market_type == market_type and s.side == side]
    if not candidates:
        return None
    same_book = [s for s in candidates if s.sportsbook_key == sportsbook_key]
    if same_book:
        return same_book[0]

    def distance(snapshot: LineSnapshot) -> float:
        if line_value is None or snapshot.line_value is None:
            return 0.0
        return abs(snapshot.line_value - line_value)

    return min(candidates, key=distance)


def compute_clv(placement_odds_american: int, closing_odds_american: int) -> float:
    """CLV as a probability fraction: implied(closing) - implied(placement).

    Positive means the market moved toward the bet after placement (value
    was captured before the close).
    """
    return implied_probability(closing_odds_american) - implied_probability(placement_odds_american)
