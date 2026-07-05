"""Bankroll state and history endpoints."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from bookie_emulator.api.dependencies import get_bankroll_service
from bookie_emulator.api.envelope import Envelope, envelope
from bookie_emulator.api.routes.bets import as_utc
from bookie_emulator.api.schemas import BankrollData, BankrollHistoryData, HistoryInterval
from bookie_emulator.services.bankroll import BankrollService

router = APIRouter(tags=["bankroll"])

ServiceDep = Annotated[BankrollService, Depends(get_bankroll_service)]


@router.get("/bankroll", response_model=Envelope[BankrollData])
async def get_bankroll(service: ServiceDep) -> Envelope[BankrollData]:
    """Current bankroll: starting units plus cumulative graded P/L, with open exposure."""
    return envelope(await service.current())


@router.get("/bankroll/history", response_model=Envelope[BankrollHistoryData])
async def get_bankroll_history(
    service: ServiceDep,
    date_from: Annotated[datetime | None, Query(description="Start date (ISO 8601). Default: 30 days ago.")] = None,
    date_to: Annotated[datetime | None, Query(description="End date (ISO 8601). Default: now.")] = None,
    interval: Annotated[HistoryInterval, Query(description="per_bet, daily, or weekly.")] = "daily",
) -> Envelope[BankrollHistoryData]:
    """Bankroll snapshots for charting; daily/weekly keep the last snapshot per bucket."""
    return envelope(await service.history(as_utc(date_from), as_utc(date_to), interval))
