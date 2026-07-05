"""Pure bet grading: final scores + bet terms -> WON/LOST/PUSH and P/L.

Sign convention for spreads: ``line_value`` is stored relative to the
selected side (negative for favorites), so a bet covers when
``selected_margin + line_value > 0``.
"""

from typing import Literal

GradeStatus = Literal["WON", "LOST", "PUSH"]

_WIN_VERB = {"WON": "covering", "LOST": "failing to cover", "PUSH": "push on"}


def _settle(result: float) -> GradeStatus:
    if result > 0:
        return "WON"
    if result < 0:
        return "LOST"
    return "PUSH"


def _game_summary(home_score: int, away_score: int, home_team: str | None, away_team: str | None) -> str:
    home = home_team or "Home"
    away = away_team or "Away"
    margin = home_score - away_score
    if margin > 0:
        return f"{home} won by {margin}"
    if margin < 0:
        return f"{away} won by {-margin}"
    return "Game tied"


def grade_bet(
    market_type: str,
    side: str,
    line_value: float | None,
    home_score: int,
    away_score: int,
    home_team: str | None = None,
    away_team: str | None = None,
) -> tuple[GradeStatus, str]:
    """Grade a settled market and describe the outcome.

    Returns (status, human-readable description), e.g.
    ("WON", "LAL won by 8, covering -3.5").
    """
    margin = home_score - away_score
    total = home_score + away_score
    selected_margin = margin if side == "HOME" else -margin

    if market_type == "SPREAD":
        line = line_value or 0.0
        status = _settle(selected_margin + line)
        description = f"{_game_summary(home_score, away_score, home_team, away_team)}, {_WIN_VERB[status]} {line:+g}"
        return status, description

    if market_type == "TOTAL":
        line = line_value or 0.0
        over_result = total - line
        status = _settle(over_result if side == "OVER" else -over_result)
        if over_result > 0:
            relation = "over"
        elif over_result < 0:
            relation = "under"
        else:
            relation = "push on"
        return status, f"Game landed {total}, {relation} {line:g}"

    if market_type == "MONEYLINE":
        status = _settle(selected_margin)
        return status, _game_summary(home_score, away_score, home_team, away_team)

    raise ValueError(f"Cannot grade market type {market_type}")


def profit_loss(status: str, stake: float, odds_decimal: float) -> float:
    """P/L in units: win pays stake * (odds - 1); push and void return the stake."""
    if status == "WON":
        return stake * (odds_decimal - 1.0)
    if status == "LOST":
        return -stake
    return 0.0
