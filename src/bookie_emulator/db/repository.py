"""Repositories over the emulator schema (SQLAlchemy Core, async)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import Row, and_, case, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from bookie_emulator.api.pagination import Cursor
from bookie_emulator.db.tables import bankroll_snapshots, bet_grades, paper_bets


@dataclass(frozen=True)
class PaperBetRecord:
    id: uuid.UUID
    game_id: uuid.UUID
    game_external_id: str
    league: str
    market_type: str
    selection: str
    side: str
    line_value: float | None
    sportsbook_id: uuid.UUID | None
    sportsbook_key: str
    odds_american: int
    odds_decimal: float
    stake: float
    predicted_probability: float
    edge_at_placement: float
    kelly_fraction: float
    reasoning: str | None
    prediction_id: uuid.UUID | None
    edge_id: uuid.UUID | None
    idempotency_key: str
    game_start_at: datetime | None
    status: str
    placed_at: datetime
    graded_at: datetime | None


@dataclass(frozen=True)
class BetGradeRecord:
    id: uuid.UUID
    bet_id: uuid.UUID
    actual_result: str
    actual_home_score: int | None
    actual_away_score: int | None
    actual_margin: int | None
    actual_total: int | None
    game_result_id: uuid.UUID | None
    profit_loss: float
    closing_line_value: float | None
    closing_odds: int | None
    clv: float | None
    graded_at: datetime


@dataclass(frozen=True)
class BankrollSnapshotRecord:
    id: uuid.UUID
    balance: float
    total_wagered: float
    total_profit_loss: float
    open_bets_count: int
    total_bets: int
    total_wins: int
    total_losses: int
    avg_clv: float | None
    snapshot_at: datetime


@dataclass(frozen=True)
class LedgerFilters:
    """Ledger/performance filters. Enum-valued fields carry DB enum values."""

    league: str | None = None
    market_type: str | None = None
    result: str | None = None
    status: str | None = None  # "open" | "graded" | None (all)
    date_from: datetime | None = None
    date_to: datetime | None = None
    min_edge: float | None = None  # fraction, matching edge_at_placement
    graded_from: datetime | None = None


def _opt_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _bet_from_row(row: Row[Any]) -> PaperBetRecord:
    return PaperBetRecord(
        id=row.id,
        game_id=row.game_id,
        game_external_id=row.game_external_id,
        league=row.league,
        market_type=row.market_type,
        selection=row.selection,
        side=row.side,
        line_value=_opt_float(row.line_value),
        sportsbook_id=row.sportsbook_id,
        sportsbook_key=row.sportsbook_key,
        odds_american=row.odds_american,
        odds_decimal=float(row.odds_decimal),
        stake=float(row.stake),
        predicted_probability=float(row.predicted_probability),
        edge_at_placement=float(row.edge_at_placement),
        kelly_fraction=float(row.kelly_fraction),
        reasoning=row.reasoning,
        prediction_id=row.prediction_id,
        edge_id=row.edge_id,
        idempotency_key=row.idempotency_key,
        game_start_at=row.game_start_at,
        status=row.status,
        placed_at=row.placed_at,
        graded_at=row.graded_at,
    )


_GRADE_COLUMNS = [
    bet_grades.c.id.label("grade_id"),
    bet_grades.c.bet_id.label("grade_bet_id"),
    bet_grades.c.actual_result,
    bet_grades.c.actual_home_score,
    bet_grades.c.actual_away_score,
    bet_grades.c.actual_margin,
    bet_grades.c.actual_total,
    bet_grades.c.game_result_id,
    bet_grades.c.profit_loss,
    bet_grades.c.closing_line_value,
    bet_grades.c.closing_odds,
    bet_grades.c.clv,
    bet_grades.c.graded_at.label("grade_graded_at"),
]


def _grade_from_row(row: Row[Any]) -> BetGradeRecord | None:
    if row.grade_id is None:
        return None
    return BetGradeRecord(
        id=row.grade_id,
        bet_id=row.grade_bet_id,
        actual_result=row.actual_result,
        actual_home_score=row.actual_home_score,
        actual_away_score=row.actual_away_score,
        actual_margin=row.actual_margin,
        actual_total=row.actual_total,
        game_result_id=row.game_result_id,
        profit_loss=float(row.profit_loss),
        closing_line_value=_opt_float(row.closing_line_value),
        closing_odds=row.closing_odds,
        clv=_opt_float(row.clv),
        graded_at=row.grade_graded_at,
    )


def _snapshot_from_row(row: Row[Any]) -> BankrollSnapshotRecord:
    return BankrollSnapshotRecord(
        id=row.id,
        balance=float(row.balance),
        total_wagered=float(row.total_wagered),
        total_profit_loss=float(row.total_profit_loss),
        open_bets_count=row.open_bets_count,
        total_bets=row.total_bets,
        total_wins=row.total_wins,
        total_losses=row.total_losses,
        avg_clv=_opt_float(row.avg_clv),
        snapshot_at=row.snapshot_at,
    )


def _filter_conditions(filters: LedgerFilters) -> list[Any]:
    conditions: list[Any] = []
    if filters.league is not None:
        conditions.append(paper_bets.c.league == filters.league)
    if filters.market_type is not None:
        conditions.append(paper_bets.c.market_type == filters.market_type)
    if filters.result is not None:
        conditions.append(paper_bets.c.status == filters.result)
    if filters.status == "open":
        conditions.append(paper_bets.c.status == "OPEN")
    elif filters.status == "graded":
        conditions.append(paper_bets.c.status != "OPEN")
    if filters.date_from is not None:
        conditions.append(paper_bets.c.placed_at >= filters.date_from)
    if filters.date_to is not None:
        conditions.append(paper_bets.c.placed_at <= filters.date_to)
    if filters.min_edge is not None:
        conditions.append(paper_bets.c.edge_at_placement >= filters.min_edge)
    if filters.graded_from is not None:
        conditions.append(paper_bets.c.graded_at >= filters.graded_from)
    return conditions


class PaperBetRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_idempotent(self, values: dict[str, Any]) -> tuple[PaperBetRecord, bool]:
        """Insert a bet; on an idempotency-key replay return the existing bet.

        Returns (record, created) where created is False for replays.
        """
        stmt = (
            pg_insert(paper_bets)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(paper_bets)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).one_or_none()
        if row is not None:
            return _bet_from_row(row), True
        existing = select(paper_bets).where(paper_bets.c.idempotency_key == values["idempotency_key"])
        async with self._engine.connect() as conn:
            row = (await conn.execute(existing)).one()
        return _bet_from_row(row), False

    async def get_with_grade(self, bet_id: uuid.UUID) -> tuple[PaperBetRecord, BetGradeRecord | None] | None:
        stmt = (
            select(paper_bets, *_GRADE_COLUMNS)
            .join(bet_grades, bet_grades.c.bet_id == paper_bets.c.id, isouter=True)
            .where(paper_bets.c.id == bet_id)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).one_or_none()
        if row is None:
            return None
        return _bet_from_row(row), _grade_from_row(row)

    async def list_ledger(
        self, filters: LedgerFilters, limit: int, cursor: Cursor | None = None
    ) -> tuple[list[tuple[PaperBetRecord, BetGradeRecord | None]], bool]:
        """Keyset-paginated ledger ordered by (placed_at DESC, id DESC)."""
        stmt = (
            select(paper_bets, *_GRADE_COLUMNS)
            .join(bet_grades, bet_grades.c.bet_id == paper_bets.c.id, isouter=True)
            .order_by(paper_bets.c.placed_at.desc(), paper_bets.c.id.desc())
            .limit(limit + 1)
        )
        conditions = _filter_conditions(filters)
        if cursor is not None:
            conditions.append(tuple_(paper_bets.c.placed_at, paper_bets.c.id) < (cursor.placed_at, cursor.id))
        if conditions:
            stmt = stmt.where(and_(*conditions))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        has_more = len(rows) > limit
        return [(_bet_from_row(row), _grade_from_row(row)) for row in rows[:limit]], has_more

    async def graded_bets(self, filters: LedgerFilters) -> list[tuple[PaperBetRecord, BetGradeRecord]]:
        """Graded bets with their grades, for live performance aggregation."""
        stmt = (
            select(paper_bets, *_GRADE_COLUMNS)
            .join(bet_grades, bet_grades.c.bet_id == paper_bets.c.id)
            .where(paper_bets.c.status != "OPEN")
            .order_by(paper_bets.c.graded_at.asc())
        )
        conditions = _filter_conditions(filters)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        pairs: list[tuple[PaperBetRecord, BetGradeRecord]] = []
        for row in rows:
            grade = _grade_from_row(row)
            if grade is not None:
                pairs.append((_bet_from_row(row), grade))
        return pairs

    async def open_bets_for_game(self, game_id: uuid.UUID) -> list[PaperBetRecord]:
        stmt = select(paper_bets).where(paper_bets.c.game_id == game_id, paper_bets.c.status == "OPEN")
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [_bet_from_row(row) for row in rows]

    async def open_game_ids_started_before(self, cutoff: datetime) -> list[uuid.UUID]:
        stmt = (
            select(paper_bets.c.game_id)
            .distinct()
            .where(paper_bets.c.status == "OPEN", paper_bets.c.game_start_at < cutoff)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [row.game_id for row in rows]

    async def total_profit(self) -> float:
        stmt = select(func.coalesce(func.sum(bet_grades.c.profit_loss), 0))
        async with self._engine.connect() as conn:
            return float((await conn.execute(stmt)).scalar_one())

    async def open_exposure(self) -> float:
        stmt = select(func.coalesce(func.sum(paper_bets.c.stake), 0)).where(paper_bets.c.status == "OPEN")
        async with self._engine.connect() as conn:
            return float((await conn.execute(stmt)).scalar_one())

    async def open_bets_count(self) -> int:
        stmt = select(func.count()).select_from(paper_bets).where(paper_bets.c.status == "OPEN")
        async with self._engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())

    async def health_stats(self) -> tuple[int, int, int]:
        """(open_bets, bets placed today, bets graded today) in UTC."""
        midnight = datetime.combine(utc_now().date(), time.min, tzinfo=UTC)
        open_stmt = select(func.count()).select_from(paper_bets).where(paper_bets.c.status == "OPEN")
        placed_stmt = select(func.count()).select_from(paper_bets).where(paper_bets.c.placed_at >= midnight)
        graded_stmt = select(func.count()).select_from(paper_bets).where(paper_bets.c.graded_at >= midnight)
        async with self._engine.connect() as conn:
            open_count = int((await conn.execute(open_stmt)).scalar_one())
            placed_today = int((await conn.execute(placed_stmt)).scalar_one())
            graded_today = int((await conn.execute(graded_stmt)).scalar_one())
        return open_count, placed_today, graded_today

    async def apply_grade(
        self,
        bet_id: uuid.UUID,
        status: str,
        grade_values: dict[str, Any],
        starting_bankroll: float,
        force: bool = False,
    ) -> bool:
        """Claim and grade a bet transactionally, appending a bankroll snapshot.

        The claim (`UPDATE ... WHERE status = 'OPEN' RETURNING id`) makes the
        event, poller, and manual grading paths race-safe: only one grader
        wins. With force=True the claim skips the status guard and the grade
        row is upserted (re-grade).
        """
        now = utc_now()
        async with self._engine.begin() as conn:
            claim = (
                update(paper_bets)
                .where(paper_bets.c.id == bet_id)
                .values(status=status, graded_at=now)
                .returning(paper_bets.c.id)
            )
            if not force:
                claim = claim.where(paper_bets.c.status == "OPEN")
            if (await conn.execute(claim)).first() is None:
                return False

            grade_stmt = pg_insert(bet_grades).values(bet_id=bet_id, graded_at=now, **grade_values)
            if force:
                grade_stmt = grade_stmt.on_conflict_do_update(
                    index_elements=["bet_id"], set_={**grade_values, "graded_at": now}
                )
            await conn.execute(grade_stmt)

            graded = bet_grades.join(paper_bets, paper_bets.c.id == bet_grades.c.bet_id)
            agg = (
                await conn.execute(
                    select(
                        func.count().label("total_bets"),
                        func.coalesce(func.sum(case((paper_bets.c.status == "WON", 1), else_=0)), 0).label("wins"),
                        func.coalesce(func.sum(case((paper_bets.c.status == "LOST", 1), else_=0)), 0).label("losses"),
                        func.coalesce(func.sum(paper_bets.c.stake), 0).label("wagered"),
                        func.coalesce(func.sum(bet_grades.c.profit_loss), 0).label("profit"),
                        func.avg(bet_grades.c.clv).label("avg_clv"),
                    ).select_from(graded)
                )
            ).one()
            open_agg = (
                await conn.execute(
                    select(func.count().label("open_count"))
                    .select_from(paper_bets)
                    .where(paper_bets.c.status == "OPEN")
                )
            ).one()
            await conn.execute(
                insert(bankroll_snapshots).values(
                    balance=starting_bankroll + float(agg.profit),
                    total_wagered=agg.wagered,
                    total_profit_loss=agg.profit,
                    open_bets_count=open_agg.open_count,
                    total_bets=agg.total_bets,
                    total_wins=agg.wins,
                    total_losses=agg.losses,
                    avg_clv=agg.avg_clv,
                    snapshot_at=now,
                )
            )
        return True

    async def is_healthy(self) -> bool:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(select(1))
            return True
        except Exception:  # noqa: BLE001 - any DB failure means unhealthy
            return False


class BankrollRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def latest_snapshot(self) -> BankrollSnapshotRecord | None:
        stmt = select(bankroll_snapshots).order_by(bankroll_snapshots.c.snapshot_at.desc()).limit(1)
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).one_or_none()
        return _snapshot_from_row(row) if row is not None else None

    async def history(self, date_from: datetime, date_to: datetime) -> list[BankrollSnapshotRecord]:
        stmt = (
            select(bankroll_snapshots)
            .where(bankroll_snapshots.c.snapshot_at >= date_from, bankroll_snapshots.c.snapshot_at <= date_to)
            .order_by(bankroll_snapshots.c.snapshot_at.asc())
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [_snapshot_from_row(row) for row in rows]


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
