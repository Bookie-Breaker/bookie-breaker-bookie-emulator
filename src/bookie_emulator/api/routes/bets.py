"""Bet placement, ledger, detail, and manual grading endpoints."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Response

from bookie_emulator.api.dependencies import get_bet_repo, get_bet_service, get_grader, get_settings
from bookie_emulator.api.envelope import Envelope, PagedEnvelope, Pagination, envelope, paged_envelope
from bookie_emulator.api.errors import NotFoundError
from bookie_emulator.api.pagination import Cursor, decode_cursor, encode_cursor
from bookie_emulator.api.schemas import (
    API_TO_DB_RESULT,
    ApiResult,
    BetData,
    BetDetailData,
    GradeRequest,
    League,
    MarketType,
    PlaceBetRequest,
    StatusFilter,
    bet_to_data,
    bet_to_detail,
    edge_percent_to_fraction,
)
from bookie_emulator.config import Settings
from bookie_emulator.db.repository import LedgerFilters, PaperBetRepository
from bookie_emulator.services.bets import BetService
from bookie_emulator.services.grader import GraderService

router = APIRouter(tags=["bets"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RepoDep = Annotated[PaperBetRepository, Depends(get_bet_repo)]
BetServiceDep = Annotated[BetService, Depends(get_bet_service)]
GraderDep = Annotated[GraderService, Depends(get_grader)]

_DATE_FROM = Query(description="Start date (ISO 8601), inclusive, on placed_at.")
_DATE_TO = Query(description="End date (ISO 8601), inclusive, on placed_at.")


def as_utc(value: datetime | None) -> datetime | None:
    """Treat naive query datetimes (e.g. bare dates) as UTC for timestamptz columns."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@router.post("/bets", status_code=201, response_model=Envelope[BetData])
async def place_bet(
    request: PlaceBetRequest,
    response: Response,
    service: BetServiceDep,
    settings: SettingsDep,
    x_idempotency_key: Annotated[str, Header(description="UUID preventing duplicate placement on retries.")],
) -> Envelope[BetData]:
    """Place a paper bet, capturing current odds from lines-service.

    Replaying an X-Idempotency-Key returns the existing bet with 200 OK.
    """
    bet, created = await service.place_bet(request, idempotency_key=x_idempotency_key)
    if not created:
        response.status_code = 200
    return envelope(bet_to_data(bet, None, settings.unit_size_dollars))


@router.get("/bets", response_model=PagedEnvelope[list[BetData]])
async def list_bets(
    repo: RepoDep,
    settings: SettingsDep,
    league: Annotated[League | None, Query(description="Filter by league.")] = None,
    market_type: Annotated[MarketType | None, Query(description="Filter by market type.")] = None,
    result: Annotated[ApiResult | None, Query(description="Filter by result.")] = None,
    date_from: Annotated[datetime | None, _DATE_FROM] = None,
    date_to: Annotated[datetime | None, _DATE_TO] = None,
    min_edge: Annotated[float | None, Query(description="Minimum edge percentage.")] = None,
    is_parlay: Annotated[bool | None, Query(description="true for parlay parents only, false to exclude them.")] = None,
    is_live: Annotated[bool | None, Query(description="true for live bets only, false for pregame only.")] = None,
    status: Annotated[StatusFilter, Query(description="open for pending, graded for completed, or all.")] = "all",
    limit: Annotated[int, Query(ge=1, le=200, description="Max results per page.")] = 50,
    cursor: Annotated[str | None, Query(description="Opaque pagination cursor from a previous page.")] = None,
) -> PagedEnvelope[list[BetData]]:
    """The bet ledger: cursor-paginated bets ordered by placed_at descending."""
    filters = LedgerFilters(
        league=league,
        market_type=market_type,
        result=API_TO_DB_RESULT[result] if result is not None else None,
        status=None if status == "all" else status,
        date_from=as_utc(date_from),
        date_to=as_utc(date_to),
        min_edge=edge_percent_to_fraction(min_edge) if min_edge is not None else None,
        is_parlay=is_parlay,
        is_live=is_live,
    )
    decoded = decode_cursor(cursor) if cursor is not None else None
    rows, has_more = await repo.list_ledger(filters, limit=limit, cursor=decoded)
    next_cursor = (
        encode_cursor(Cursor(placed_at=rows[-1][0].placed_at, id=rows[-1][0].id)) if has_more and rows else None
    )
    data = [bet_to_data(bet, grade, settings.unit_size_dollars) for bet, grade in rows]
    return paged_envelope(data, Pagination(limit=limit, has_more=has_more, next_cursor=next_cursor))


@router.get("/bets/{bet_id}", response_model=Envelope[BetDetailData])
async def get_bet(
    bet_id: Annotated[uuid.UUID, Path(description="The paper bet identifier.")],
    repo: RepoDep,
    settings: SettingsDep,
) -> Envelope[BetDetailData]:
    """Get a bet with its grade details when graded."""
    found = await repo.get_with_grade(bet_id)
    if found is None:
        raise NotFoundError(f"Bet {bet_id} not found")
    bet, grade = found
    return envelope(bet_to_detail(bet, grade, settings.unit_size_dollars))


@router.post("/bets/{bet_id}/grade", response_model=Envelope[BetDetailData])
async def grade_bet(
    bet_id: Annotated[uuid.UUID, Path(description="The paper bet identifier.")],
    grader: GraderDep,
    settings: SettingsDep,
    request: GradeRequest | None = None,
) -> Envelope[BetDetailData]:
    """Manually grade a bet from the statistics-service game result.

    Used when automatic grading (game.completed event or the fallback
    poller) has not fired. force=true re-grades an already-graded bet.
    """
    force = request.force if request is not None else False
    bet, grade = await grader.grade_manual(bet_id, force=force)
    return envelope(bet_to_detail(bet, grade, settings.unit_size_dollars))
