"""Bankroll state derived from grades plus snapshot history for charting.

The bankroll is never stored as a mutable balance: it is always
starting_bankroll_units + SUM(bet_grades.profit_loss). Snapshots exist for
trend charting only (one is appended inside every grading transaction).
"""

from datetime import datetime, timedelta

from bookie_emulator.api.schemas import (
    BankrollConfigData,
    BankrollData,
    BankrollHistoryData,
    HistoryInterval,
    snapshot_to_data,
)
from bookie_emulator.config import Settings
from bookie_emulator.db.repository import BankrollRepository, BankrollSnapshotRecord, PaperBetRepository, utc_now


def _bucket_key(snapshot: BankrollSnapshotRecord, interval: HistoryInterval) -> str:
    if interval == "weekly":
        year, week, _ = snapshot.snapshot_at.isocalendar()
        return f"{year}-W{week:02d}"
    return snapshot.snapshot_at.date().isoformat()


class BankrollService:
    def __init__(self, bet_repo: PaperBetRepository, bankroll_repo: BankrollRepository, settings: Settings) -> None:
        self._bets = bet_repo
        self._snapshots = bankroll_repo
        self._settings = settings

    async def current(self) -> BankrollData:
        profit = await self._bets.total_profit()
        bankroll = self._settings.starting_bankroll_units + profit
        latest = await self._snapshots.latest_snapshot()
        return BankrollData(
            bankroll_units=bankroll,
            bankroll_dollars=bankroll * self._settings.unit_size_dollars,
            unit_size_dollars=self._settings.unit_size_dollars,
            starting_bankroll_units=self._settings.starting_bankroll_units,
            total_profit_units=profit,
            open_bets_count=await self._bets.open_bets_count(),
            open_bets_exposure_units=await self._bets.open_exposure(),
            config=BankrollConfigData(
                max_bet_units=self._settings.max_bet_units,
                max_daily_exposure_units=self._settings.max_daily_exposure_units,
                kelly_fraction=self._settings.kelly_fraction,
                kelly_enabled=self._settings.kelly_enabled,
            ),
            snapshot_at=latest.snapshot_at if latest is not None else utc_now(),
        )

    async def history(
        self, date_from: datetime | None, date_to: datetime | None, interval: HistoryInterval
    ) -> BankrollHistoryData:
        end = date_to or utc_now()
        start = date_from or end - timedelta(days=30)
        rows = await self._snapshots.history(start, end)
        if interval != "per_bet":
            last_per_bucket: dict[str, BankrollSnapshotRecord] = {}
            for row in rows:
                last_per_bucket[_bucket_key(row, interval)] = row
            rows = sorted(last_per_bucket.values(), key=lambda r: r.snapshot_at)
        return BankrollHistoryData(
            interval=interval,
            snapshots=[snapshot_to_data(row, self._settings.unit_size_dollars) for row in rows],
        )
