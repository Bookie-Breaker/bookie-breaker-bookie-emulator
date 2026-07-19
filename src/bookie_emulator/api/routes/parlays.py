"""Parlay placement and detail endpoints (ADR-028, Phase 7 Wave 1)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Response

from bookie_emulator.api.dependencies import get_bet_repo, get_bet_service, get_settings
from bookie_emulator.api.envelope import Envelope, envelope
from bookie_emulator.api.errors import NotFoundError
from bookie_emulator.api.schemas import ParlayDetailData, PlaceParlayRequest, parlay_to_detail
from bookie_emulator.config import Settings
from bookie_emulator.db.repository import PaperBetRepository
from bookie_emulator.services.bets import BetService

router = APIRouter(tags=["parlays"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RepoDep = Annotated[PaperBetRepository, Depends(get_bet_repo)]
BetServiceDep = Annotated[BetService, Depends(get_bet_service)]


@router.post("/parlays", status_code=201, response_model=Envelope[ParlayDetailData])
async def place_parlay(
    request: PlaceParlayRequest,
    response: Response,
    service: BetServiceDep,
    settings: SettingsDep,
    x_idempotency_key: Annotated[str, Header(description="UUID preventing duplicate placement on retries.")],
) -> Envelope[ParlayDetailData]:
    """Place a parlay (2-6 team-market legs), capturing per-leg odds from
    lines-service and pricing the parent at the product of leg decimals.

    Replaying an X-Idempotency-Key returns the existing parlay with 200 OK.
    """
    parent, legs, created = await service.place_parlay(request, idempotency_key=x_idempotency_key)
    if not created:
        response.status_code = 200
    return envelope(parlay_to_detail(parent, None, legs, settings.unit_size_dollars))


@router.get("/parlays/{bet_id}", response_model=Envelope[ParlayDetailData])
async def get_parlay(
    bet_id: Annotated[uuid.UUID, Path(description="The parlay parent bet identifier.")],
    repo: RepoDep,
    settings: SettingsDep,
) -> Envelope[ParlayDetailData]:
    """Get a parlay parent with its legs, per-leg statuses, and grade when settled."""
    found = await repo.get_with_grade(bet_id)
    if found is None or not found[0].is_parlay:
        raise NotFoundError(f"Parlay {bet_id} not found")
    bet, grade = found
    legs = await repo.legs_for_bet(bet_id)
    return envelope(parlay_to_detail(bet, grade, legs, settings.unit_size_dollars))
