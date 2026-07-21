"""BankrollService: derived balance math, snapshot fallbacks, and history
bucketing (per_bet / daily / weekly keep-last semantics)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bookie_emulator.config import Settings
from bookie_emulator.db.repository import BankrollSnapshotRecord
from bookie_emulator.services.bankroll import BankrollService


def snapshot(at: datetime, balance: float = 100.0, **overrides: Any) -> BankrollSnapshotRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "balance": balance,
        "total_wagered": 10.0,
        "total_profit_loss": balance - 100.0,
        "open_bets_count": 1,
        "total_bets": 4,
        "total_wins": 2,
        "total_losses": 1,
        "avg_clv": 0.01,
        "snapshot_at": at,
    }
    defaults.update(overrides)
    return BankrollSnapshotRecord(**defaults)


class StubBetRepo:
    def __init__(self, profit: float = 12.5, open_count: int = 3, exposure: float = 4.5) -> None:
        self.profit = profit
        self.open_count = open_count
        self.exposure = exposure

    async def total_profit(self) -> float:
        return self.profit

    async def open_bets_count(self) -> int:
        return self.open_count

    async def open_exposure(self) -> float:
        return self.exposure


class StubSnapshotRepo:
    def __init__(self, latest: BankrollSnapshotRecord | None = None, rows: list[BankrollSnapshotRecord] | None = None):
        self.latest = latest
        self.rows = rows or []
        self.ranges: list[tuple[datetime, datetime]] = []

    async def latest_snapshot(self) -> BankrollSnapshotRecord | None:
        return self.latest

    async def history(self, date_from: datetime, date_to: datetime) -> list[BankrollSnapshotRecord]:
        self.ranges.append((date_from, date_to))
        return self.rows


def make_service(
    bets: StubBetRepo | None = None, snapshots: StubSnapshotRepo | None = None, **settings: Any
) -> BankrollService:
    return BankrollService(
        bets or StubBetRepo(),  # type: ignore[arg-type]
        snapshots or StubSnapshotRepo(),  # type: ignore[arg-type]
        Settings(_env_file=None, **settings),
    )


class TestCurrent:
    async def test_balance_is_starting_plus_profit(self) -> None:
        latest = snapshot(datetime(2026, 7, 19, 12, 0, tzinfo=UTC), balance=112.5)
        service = make_service(
            StubBetRepo(profit=12.5),
            StubSnapshotRepo(latest=latest),
            starting_bankroll_units=100.0,
            unit_size_dollars=50.0,
        )
        data = await service.current()
        assert data.bankroll_units == 112.5
        assert data.bankroll_dollars == 5625.0
        assert data.total_profit_units == 12.5
        assert data.open_bets_count == 3
        assert data.open_bets_exposure_units == 4.5
        assert data.snapshot_at == latest.snapshot_at
        assert data.config.kelly_fraction == 0.25

    async def test_no_snapshot_falls_back_to_now(self) -> None:
        before = datetime.now(tz=UTC)
        data = await make_service().current()
        assert before <= data.snapshot_at <= datetime.now(tz=UTC)


class TestHistory:
    async def test_default_range_is_last_30_days(self) -> None:
        snapshots = StubSnapshotRepo()
        service = make_service(snapshots=snapshots)
        data = await service.history(None, None, "daily")
        assert data.interval == "daily"
        assert data.snapshots == []
        start, end = snapshots.ranges[0]
        assert end - start == timedelta(days=30)
        assert end <= datetime.now(tz=UTC)

    async def test_explicit_range_is_passed_through(self) -> None:
        start = datetime(2026, 6, 1, tzinfo=UTC)
        end = datetime(2026, 6, 30, tzinfo=UTC)
        snapshots = StubSnapshotRepo()
        await make_service(snapshots=snapshots).history(start, end, "per_bet")
        assert snapshots.ranges == [(start, end)]

    async def test_per_bet_keeps_every_snapshot(self) -> None:
        rows = [
            snapshot(datetime(2026, 7, 1, 10, 0, tzinfo=UTC), balance=101.0),
            snapshot(datetime(2026, 7, 1, 11, 0, tzinfo=UTC), balance=99.0),
        ]
        data = await make_service(snapshots=StubSnapshotRepo(rows=rows)).history(None, None, "per_bet")
        assert [s.bankroll_units for s in data.snapshots] == [101.0, 99.0]

    async def test_daily_keeps_last_snapshot_per_day(self) -> None:
        rows = [
            snapshot(datetime(2026, 7, 1, 10, 0, tzinfo=UTC), balance=101.0),
            snapshot(datetime(2026, 7, 1, 23, 0, tzinfo=UTC), balance=99.0),
            snapshot(datetime(2026, 7, 2, 9, 0, tzinfo=UTC), balance=104.0),
        ]
        data = await make_service(snapshots=StubSnapshotRepo(rows=rows)).history(None, None, "daily")
        assert [s.bankroll_units for s in data.snapshots] == [99.0, 104.0]

    async def test_weekly_buckets_by_iso_week(self) -> None:
        # 2026-07-01 (Wed) and 2026-07-03 (Fri) share ISO week 27; 2026-07-06 (Mon) opens week 28
        rows = [
            snapshot(datetime(2026, 7, 1, 10, 0, tzinfo=UTC), balance=101.0),
            snapshot(datetime(2026, 7, 3, 10, 0, tzinfo=UTC), balance=97.0),
            snapshot(datetime(2026, 7, 6, 10, 0, tzinfo=UTC), balance=105.0),
        ]
        data = await make_service(snapshots=StubSnapshotRepo(rows=rows)).history(None, None, "weekly")
        assert [s.bankroll_units for s in data.snapshots] == [97.0, 105.0]

    async def test_snapshot_conversion_math(self) -> None:
        rows = [
            snapshot(
                datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
                balance=110.0,
                total_wagered=20.0,
                total_profit_loss=10.0,
                total_wins=3,
                total_losses=1,
            )
        ]
        service = make_service(snapshots=StubSnapshotRepo(rows=rows), unit_size_dollars=10.0)
        entry = (await service.history(None, None, "per_bet")).snapshots[0]
        assert entry.bankroll_dollars == 1100.0
        assert entry.win_rate == pytest.approx(0.75)
        assert entry.roi == pytest.approx(0.5)
        assert entry.units_won == 10.0

    async def test_zero_activity_snapshot_rates_are_zero(self) -> None:
        rows = [
            snapshot(
                datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
                total_wagered=0.0,
                total_profit_loss=0.0,
                total_wins=0,
                total_losses=0,
            )
        ]
        entry = (await make_service(snapshots=StubSnapshotRepo(rows=rows)).history(None, None, "per_bet")).snapshots[0]
        assert entry.win_rate == 0.0
        assert entry.roi == 0.0
