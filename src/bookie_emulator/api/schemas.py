"""Request/response models mirroring api-contracts/bookie-emulator-api.md.

The API boundary owns two representations:

- bet status: DB bet_result_enum OPEN/WON/LOST/PUSH/VOID maps 1:1 onto the
  API's PENDING/WIN/LOSS/PUSH/VOID.
- edge size: the API speaks percentage points (4.2 = 4.2%); the DB stores
  fractions (0.042).
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from bookie_emulator.db.repository import BankrollSnapshotRecord, BetGradeRecord, PaperBetRecord

MarketType = Literal["SPREAD", "TOTAL", "MONEYLINE"]
Side = Literal["HOME", "AWAY", "OVER", "UNDER"]
League = Literal["NFL", "NBA", "MLB", "NCAA_FB", "NCAA_BB", "NCAA_BSB"]
ApiResult = Literal["PENDING", "WIN", "LOSS", "PUSH", "VOID"]
StatusFilter = Literal["open", "graded", "all"]
Window = Literal["daily", "weekly", "monthly", "all_time"]
GroupBy = Literal["league", "market_type", "sportsbook", "month"]
HistoryInterval = Literal["per_bet", "daily", "weekly"]

DB_TO_API_RESULT: dict[str, ApiResult] = {
    "OPEN": "PENDING",
    "WON": "WIN",
    "LOST": "LOSS",
    "PUSH": "PUSH",
    "VOID": "VOID",
}
API_TO_DB_RESULT: dict[str, str] = {api: db for db, api in DB_TO_API_RESULT.items()}


def edge_percent_to_fraction(percent: float) -> float:
    return percent / 100.0


def edge_fraction_to_percent(fraction: float) -> float:
    return fraction * 100.0


class PlaceBetRequest(BaseModel):
    game_id: uuid.UUID
    game_external_id: str | None = None
    edge_id: uuid.UUID | None = None
    prediction_id: uuid.UUID | None = None
    market_type: MarketType
    selection: str
    side: Side
    sportsbook_key: str | None = None
    predicted_probability: float = Field(gt=0.0, lt=1.0)
    edge_percentage: float = Field(gt=0.0, description="Edge in percentage points (4.2 = 4.2%).")
    stake: float = Field(description="Stake in units. Must fit the available bankroll (checked at placement).")
    kelly_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    reasoning: str | None = None


class GradeRequest(BaseModel):
    force: bool = False


class GradeData(BaseModel):
    id: uuid.UUID
    game_result_id: uuid.UUID | None
    actual_home_score: int | None
    actual_away_score: int | None
    actual_margin: int | None
    actual_total: int | None
    result: ApiResult
    result_description: str
    graded_at: datetime


class BetData(BaseModel):
    id: uuid.UUID
    game_id: uuid.UUID
    game_external_id: str
    edge_id: uuid.UUID | None
    prediction_id: uuid.UUID | None
    sportsbook_id: uuid.UUID | None
    sportsbook_key: str
    market_type: str
    selection: str
    side: str
    line_value: float | None
    odds_american: int
    odds_decimal: float
    stake: float
    stake_dollars: float
    predicted_probability: float
    edge_percentage: float
    kelly_fraction: float
    reasoning: str | None
    result: ApiResult
    profit_loss: float | None
    profit_loss_dollars: float | None
    clv: float | None
    placed_at: datetime
    graded_at: datetime | None


class BetDetailData(BetData):
    closing_line_value: float | None
    closing_odds_american: int | None
    grade: GradeData | None


def bet_to_data(bet: PaperBetRecord, grade: BetGradeRecord | None, unit_size_dollars: float) -> BetData:
    profit = grade.profit_loss if grade is not None else None
    return BetData(
        id=bet.id,
        game_id=bet.game_id,
        game_external_id=bet.game_external_id,
        edge_id=bet.edge_id,
        prediction_id=bet.prediction_id,
        sportsbook_id=bet.sportsbook_id,
        sportsbook_key=bet.sportsbook_key,
        market_type=bet.market_type,
        selection=bet.selection,
        side=bet.side,
        line_value=bet.line_value,
        odds_american=bet.odds_american,
        odds_decimal=bet.odds_decimal,
        stake=bet.stake,
        stake_dollars=bet.stake * unit_size_dollars,
        predicted_probability=bet.predicted_probability,
        edge_percentage=edge_fraction_to_percent(bet.edge_at_placement),
        kelly_fraction=bet.kelly_fraction,
        reasoning=bet.reasoning,
        result=DB_TO_API_RESULT[bet.status],
        profit_loss=profit,
        profit_loss_dollars=profit * unit_size_dollars if profit is not None else None,
        clv=grade.clv if grade is not None else None,
        placed_at=bet.placed_at,
        graded_at=bet.graded_at,
    )


def bet_to_detail(bet: PaperBetRecord, grade: BetGradeRecord | None, unit_size_dollars: float) -> BetDetailData:
    base = bet_to_data(bet, grade, unit_size_dollars)
    grade_data = (
        GradeData(
            id=grade.id,
            game_result_id=grade.game_result_id,
            actual_home_score=grade.actual_home_score,
            actual_away_score=grade.actual_away_score,
            actual_margin=grade.actual_margin,
            actual_total=grade.actual_total,
            result=DB_TO_API_RESULT[bet.status],
            result_description=grade.actual_result,
            graded_at=grade.graded_at,
        )
        if grade is not None
        else None
    )
    return BetDetailData(
        **base.model_dump(),
        closing_line_value=grade.closing_line_value if grade is not None else None,
        closing_odds_american=grade.closing_odds if grade is not None else None,
        grade=grade_data,
    )


class PeriodData(BaseModel):
    from_: datetime | None = Field(default=None, serialization_alias="from")
    to: datetime
    window: Window


class PerformanceData(BaseModel):
    period: PeriodData
    total_bets: int
    total_wins: int
    total_losses: int
    total_pushes: int
    win_rate: float
    roi: float
    total_wagered_units: float
    total_profit_units: float
    total_wagered_dollars: float
    total_profit_dollars: float
    avg_odds_american: int | None
    avg_edge_percentage: float | None
    avg_clv: float | None
    longest_win_streak: int
    longest_loss_streak: int
    brier_score: float | None
    calibration_error: float | None


class BreakdownEntry(BaseModel):
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


class BreakdownData(BaseModel):
    group_by: GroupBy
    breakdowns: list[BreakdownEntry]


class CalibrationBinData(BaseModel):
    lower: float
    upper: float
    bet_count: int
    avg_predicted_probability: float | None
    actual_win_rate: float | None


class CalibrationData(BaseModel):
    period: PeriodData
    n_bins: int
    total_graded: int
    brier_score: float | None
    calibration_error: float | None
    bins: list[CalibrationBinData]


class BankrollConfigData(BaseModel):
    max_bet_units: float
    max_daily_exposure_units: float
    kelly_fraction: float
    kelly_enabled: bool


class BankrollData(BaseModel):
    bankroll_units: float
    bankroll_dollars: float
    unit_size_dollars: float
    starting_bankroll_units: float
    total_profit_units: float
    open_bets_count: int
    open_bets_exposure_units: float
    config: BankrollConfigData
    snapshot_at: datetime


class HistorySnapshotData(BaseModel):
    timestamp: datetime
    bankroll_units: float
    bankroll_dollars: float
    total_bets: int
    total_wins: int
    total_losses: int
    win_rate: float
    roi: float
    units_won: float
    avg_clv: float | None


class BankrollHistoryData(BaseModel):
    interval: HistoryInterval
    snapshots: list[HistorySnapshotData]


def snapshot_to_data(snapshot: BankrollSnapshotRecord, unit_size_dollars: float) -> HistorySnapshotData:
    decided = snapshot.total_wins + snapshot.total_losses
    return HistorySnapshotData(
        timestamp=snapshot.snapshot_at,
        bankroll_units=snapshot.balance,
        bankroll_dollars=snapshot.balance * unit_size_dollars,
        total_bets=snapshot.total_bets,
        total_wins=snapshot.total_wins,
        total_losses=snapshot.total_losses,
        win_rate=snapshot.total_wins / decided if decided else 0.0,
        roi=snapshot.total_profit_loss / snapshot.total_wagered if snapshot.total_wagered else 0.0,
        units_won=snapshot.total_profit_loss,
        avg_clv=snapshot.avg_clv,
    )


class HealthStats(BaseModel):
    open_bets: int
    bets_today: int
    graded_today: int


class HealthData(BaseModel):
    status: str
    service: str = "bookie-emulator"
    version: str
    uptime_seconds: int
    dependencies: dict[str, str]
    subscriber: str
    stats: HealthStats
