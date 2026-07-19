"""Performance analytics computed live over graded bets (pure functions).

At current volume (<=20K bets/year) the endpoints aggregate on the fly
from paper_bets JOIN bet_grades; performance_summaries materialization is
deferred (see schemas/database-schemas/bookie-emulator.md).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from bookie_emulator.core.odds import decimal_to_american

_SETTLED = ("WON", "LOST")

# local copy of the prop market values (core stays free of api imports)
_PROP_MARKETS = frozenset({"PLAYER_PROP", "TEAM_PROP", "GAME_PROP"})


@dataclass(frozen=True)
class GradedBet:
    """The slice of a graded (bet, grade) pair the analytics need."""

    league: str
    market_type: str
    sportsbook_key: str
    stake: float
    odds_decimal: float
    predicted_probability: float
    edge_at_placement: float  # fraction (0.042 = 4.2%)
    status: str  # WON | LOST | PUSH | VOID
    profit_loss: float
    clv: float | None
    placed_at: datetime
    graded_at: datetime
    is_parlay: bool = False
    is_live: bool = False


def bet_class(bet: GradedBet) -> str:
    """Classify a bet as parlay | prop | live | single (Phase 7 Wave 1).

    A parlay parent's market_type mirrors its first leg (the enum has no
    PARLAY value), so is_parlay is the discriminator and takes precedence:
    parlay > prop > live > single.
    """
    if bet.is_parlay:
        return "parlay"
    if bet.market_type in _PROP_MARKETS:
        return "prop"
    if bet.is_live:
        return "live"
    return "single"


@dataclass(frozen=True)
class PerformanceMetrics:
    total_bets: int
    wins: int
    losses: int
    pushes: int
    win_rate: float
    roi: float
    total_wagered_units: float
    total_profit_units: float
    avg_odds_american: int | None
    avg_edge_percentage: float | None
    avg_clv: float | None
    current_streak: int
    longest_win_streak: int
    longest_loss_streak: int
    brier_score: float | None
    calibration_error: float | None


@dataclass(frozen=True)
class CalibrationBin:
    """One reliability-diagram bucket over settled (WON/LOST) bets."""

    lower: float  # inclusive
    upper: float  # exclusive; the last bin includes 1.0
    bet_count: int
    avg_predicted_probability: float | None  # None when bet_count == 0
    actual_win_rate: float | None  # None when bet_count == 0


@dataclass(frozen=True)
class BreakdownGroup:
    group: str
    total_bets: int
    wins: int
    losses: int
    pushes: int
    win_rate: float
    roi: float
    total_profit_units: float
    avg_clv: float | None
    avg_edge_percentage: float | None


def win_rate(wins: int, losses: int) -> float:
    """Win rate over decided bets; pushes are excluded from the denominator."""
    decided = wins + losses
    return wins / decided if decided else 0.0


def roi(total_profit: float, total_wagered: float) -> float:
    return total_profit / total_wagered if total_wagered else 0.0


def streaks(bets: Sequence[GradedBet]) -> tuple[int, int, int]:
    """(current_streak, longest_win_streak, longest_loss_streak) by graded_at.

    current_streak is positive for a running win streak, negative for a
    running loss streak. Pushes and voids neither extend nor break streaks.
    """
    statuses = [b.status for b in sorted(bets, key=lambda b: b.graded_at) if b.status in _SETTLED]
    longest_win = longest_loss = 0
    run = 0
    run_status = ""
    for status in statuses:
        run = run + 1 if status == run_status else 1
        run_status = status
        if status == "WON":
            longest_win = max(longest_win, run)
        else:
            longest_loss = max(longest_loss, run)
    current = run if run_status == "WON" else -run
    return current, longest_win, longest_loss


def brier_score(bets: Sequence[GradedBet]) -> float | None:
    """Mean squared error of predicted probability vs outcome over WON/LOST bets."""
    settled = [b for b in bets if b.status in _SETTLED]
    if not settled:
        return None
    return sum((b.predicted_probability - (1.0 if b.status == "WON" else 0.0)) ** 2 for b in settled) / len(settled)


def calibration_bins(bets: Sequence[GradedBet], n_bins: int = 10) -> list[CalibrationBin]:
    """Equal-width predicted-probability buckets over WON/LOST bets.

    Always returns n_bins bins; empty buckets carry count 0 and None stats.
    """
    settled = [b for b in bets if b.status in _SETTLED]
    buckets: list[list[GradedBet]] = [[] for _ in range(n_bins)]
    for bet in settled:
        index = min(int(bet.predicted_probability * n_bins), n_bins - 1)
        buckets[index].append(bet)
    bins: list[CalibrationBin] = []
    for i, contents in enumerate(buckets):
        confidence = _mean([b.predicted_probability for b in contents])
        accuracy = sum(1.0 for b in contents if b.status == "WON") / len(contents) if contents else None
        bins.append(
            CalibrationBin(
                lower=i / n_bins,
                upper=(i + 1) / n_bins,
                bet_count=len(contents),
                avg_predicted_probability=confidence,
                actual_win_rate=accuracy,
            )
        )
    return bins


def expected_calibration_error(bets: Sequence[GradedBet], n_bins: int = 10) -> float | None:
    """n-bin expected calibration error over WON/LOST bets, derived from calibration_bins."""
    bins = calibration_bins(bets, n_bins)
    total = sum(b.bet_count for b in bins)
    if not total:
        return None
    return sum(
        (b.bet_count / total) * abs(b.actual_win_rate - b.avg_predicted_probability)
        for b in bins
        if b.bet_count and b.actual_win_rate is not None and b.avg_predicted_probability is not None
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_performance(bets: Sequence[GradedBet]) -> PerformanceMetrics:
    wins = sum(1 for b in bets if b.status == "WON")
    losses = sum(1 for b in bets if b.status == "LOST")
    pushes = sum(1 for b in bets if b.status == "PUSH")
    wagered = sum(b.stake for b in bets)
    profit = sum(b.profit_loss for b in bets)
    mean_decimal = _mean([b.odds_decimal for b in bets])
    mean_edge = _mean([b.edge_at_placement for b in bets])
    current, longest_win, longest_loss = streaks(bets)
    return PerformanceMetrics(
        total_bets=len(bets),
        wins=wins,
        losses=losses,
        pushes=pushes,
        win_rate=win_rate(wins, losses),
        roi=roi(profit, wagered),
        total_wagered_units=wagered,
        total_profit_units=profit,
        avg_odds_american=decimal_to_american(mean_decimal) if mean_decimal else None,
        avg_edge_percentage=mean_edge * 100.0 if mean_edge is not None else None,
        avg_clv=_mean([b.clv for b in bets if b.clv is not None]),
        current_streak=current,
        longest_win_streak=longest_win,
        longest_loss_streak=longest_loss,
        brier_score=brier_score(bets),
        calibration_error=expected_calibration_error(bets),
    )


_GROUP_KEYS: dict[str, Callable[[GradedBet], str]] = {
    "league": lambda b: b.league,
    "market_type": lambda b: b.market_type,
    "sportsbook": lambda b: b.sportsbook_key,
    "month": lambda b: b.placed_at.strftime("%Y-%m"),
    "bet_class": bet_class,
}


def compute_breakdown(bets: Sequence[GradedBet], group_by: str) -> list[BreakdownGroup]:
    key_of = _GROUP_KEYS[group_by]
    grouped: dict[str, list[GradedBet]] = {}
    for bet in bets:
        grouped.setdefault(key_of(bet), []).append(bet)
    breakdowns: list[BreakdownGroup] = []
    for group in sorted(grouped):
        members = grouped[group]
        wins = sum(1 for b in members if b.status == "WON")
        losses = sum(1 for b in members if b.status == "LOST")
        pushes = sum(1 for b in members if b.status == "PUSH")
        profit = sum(b.profit_loss for b in members)
        mean_edge = _mean([b.edge_at_placement for b in members])
        breakdowns.append(
            BreakdownGroup(
                group=group,
                total_bets=len(members),
                wins=wins,
                losses=losses,
                pushes=pushes,
                win_rate=win_rate(wins, losses),
                roi=roi(profit, sum(b.stake for b in members)),
                total_profit_units=profit,
                avg_clv=_mean([b.clv for b in members if b.clv is not None]),
                avg_edge_percentage=mean_edge * 100.0 if mean_edge is not None else None,
            )
        )
    return breakdowns
