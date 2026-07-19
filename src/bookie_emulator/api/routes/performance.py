"""Performance analytics endpoints, computed live over graded bets."""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from bookie_emulator.api.dependencies import get_bet_repo, get_settings
from bookie_emulator.api.envelope import Envelope, envelope
from bookie_emulator.api.routes.bets import as_utc
from bookie_emulator.api.schemas import (
    BreakdownData,
    BreakdownEntry,
    CalibrationBinData,
    CalibrationData,
    GroupBy,
    League,
    MarketType,
    PerformanceData,
    PeriodData,
    Window,
)
from bookie_emulator.config import Settings
from bookie_emulator.core.performance import (
    GradedBet,
    brier_score,
    calibration_bins,
    compute_breakdown,
    compute_performance,
    expected_calibration_error,
)
from bookie_emulator.db.repository import BetGradeRecord, LedgerFilters, PaperBetRecord, PaperBetRepository, utc_now

router = APIRouter(tags=["performance"])

RepoDep = Annotated[PaperBetRepository, Depends(get_bet_repo)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_WINDOW_SPANS: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}


def _to_graded(pairs: list[tuple[PaperBetRecord, BetGradeRecord]]) -> list[GradedBet]:
    return [
        GradedBet(
            league=bet.league,
            market_type=bet.market_type,
            sportsbook_key=bet.sportsbook_key,
            stake=bet.stake,
            odds_decimal=bet.odds_decimal,
            predicted_probability=bet.predicted_probability,
            edge_at_placement=bet.edge_at_placement,
            status=bet.status,
            profit_loss=grade.profit_loss,
            clv=grade.clv,
            placed_at=bet.placed_at,
            graded_at=grade.graded_at,
            is_parlay=bet.is_parlay,
            is_live=bet.is_live,
        )
        for bet, grade in pairs
    ]


@router.get("/performance", response_model=Envelope[PerformanceData])
async def get_performance(
    repo: RepoDep,
    settings: SettingsDep,
    league: Annotated[League | None, Query(description="Filter by league.")] = None,
    market_type: Annotated[MarketType | None, Query(description="Filter by market type.")] = None,
    date_from: Annotated[datetime | None, Query(description="Start date (ISO 8601) on placed_at.")] = None,
    date_to: Annotated[datetime | None, Query(description="End date (ISO 8601) on placed_at.")] = None,
    window: Annotated[Window, Query(description="Rolling window applied to graded_at.")] = "all_time",
) -> Envelope[PerformanceData]:
    """Aggregate performance metrics over graded bets."""
    now = utc_now()
    graded_from = now - _WINDOW_SPANS[window] if window != "all_time" else None
    filters = LedgerFilters(
        league=league,
        market_type=market_type,
        date_from=as_utc(date_from),
        date_to=as_utc(date_to),
        graded_from=graded_from,
    )
    graded = _to_graded(await repo.graded_bets(filters))
    metrics = compute_performance(graded)
    period_from = filters.date_from or graded_from or (min(b.placed_at for b in graded) if graded else None)
    return envelope(
        PerformanceData(
            period=PeriodData(from_=period_from, to=filters.date_to or now, window=window),
            total_bets=metrics.total_bets,
            total_wins=metrics.wins,
            total_losses=metrics.losses,
            total_pushes=metrics.pushes,
            win_rate=metrics.win_rate,
            roi=metrics.roi,
            total_wagered_units=metrics.total_wagered_units,
            total_profit_units=metrics.total_profit_units,
            total_wagered_dollars=metrics.total_wagered_units * settings.unit_size_dollars,
            total_profit_dollars=metrics.total_profit_units * settings.unit_size_dollars,
            avg_odds_american=metrics.avg_odds_american,
            avg_edge_percentage=metrics.avg_edge_percentage,
            avg_clv=metrics.avg_clv,
            longest_win_streak=metrics.longest_win_streak,
            longest_loss_streak=metrics.longest_loss_streak,
            brier_score=metrics.brier_score,
            calibration_error=metrics.calibration_error,
        )
    )


@router.get("/performance/calibration", response_model=Envelope[CalibrationData])
async def get_calibration(
    repo: RepoDep,
    league: Annotated[League | None, Query(description="Filter by league.")] = None,
    market_type: Annotated[MarketType | None, Query(description="Filter by market type.")] = None,
    date_from: Annotated[datetime | None, Query(description="Start date (ISO 8601) on placed_at.")] = None,
    date_to: Annotated[datetime | None, Query(description="End date (ISO 8601) on placed_at.")] = None,
    window: Annotated[Window, Query(description="Rolling window applied to graded_at.")] = "all_time",
    bins: Annotated[int, Query(ge=2, le=20, description="Number of equal-width probability bins.")] = 10,
) -> Envelope[CalibrationData]:
    """Reliability-diagram bins over settled bets (predicted probability vs actual win rate).

    All bins are always returned; empty bins carry a zero count and null stats.
    ECE and Brier are noisy at low volume — consumers should surface total_graded.
    """
    now = utc_now()
    graded_from = now - _WINDOW_SPANS[window] if window != "all_time" else None
    filters = LedgerFilters(
        league=league,
        market_type=market_type,
        date_from=as_utc(date_from),
        date_to=as_utc(date_to),
        graded_from=graded_from,
    )
    graded = _to_graded(await repo.graded_bets(filters))
    computed = calibration_bins(graded, n_bins=bins)
    period_from = filters.date_from or graded_from or (min(b.placed_at for b in graded) if graded else None)
    return envelope(
        CalibrationData(
            period=PeriodData(from_=period_from, to=filters.date_to or now, window=window),
            n_bins=bins,
            total_graded=sum(b.bet_count for b in computed),
            brier_score=brier_score(graded),
            calibration_error=expected_calibration_error(graded, n_bins=bins),
            bins=[CalibrationBinData(**vars(b)) for b in computed],
        )
    )


@router.get("/performance/breakdown", response_model=Envelope[BreakdownData])
async def get_breakdown(
    repo: RepoDep,
    group_by: Annotated[GroupBy, Query(description="Grouping dimension.")] = "league",
    date_from: Annotated[datetime | None, Query(description="Start date (ISO 8601) on placed_at.")] = None,
    date_to: Annotated[datetime | None, Query(description="End date (ISO 8601) on placed_at.")] = None,
) -> Envelope[BreakdownData]:
    """Performance broken down by league, market type, sportsbook, month, or
    bet class (single | parlay | prop | live; precedence parlay > prop > live)."""
    filters = LedgerFilters(date_from=as_utc(date_from), date_to=as_utc(date_to))
    graded = _to_graded(await repo.graded_bets(filters))
    groups = compute_breakdown(graded, group_by)
    return envelope(
        BreakdownData(
            group_by=group_by,
            breakdowns=[BreakdownEntry(**vars(group)) for group in groups],
        )
    )
