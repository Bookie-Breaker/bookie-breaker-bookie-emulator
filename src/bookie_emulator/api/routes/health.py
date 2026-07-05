"""Health endpoint with dependency status, subscriber state, and bet stats."""

from typing import Annotated

from fastapi import APIRouter, Depends

from bookie_emulator.api.dependencies import get_health_service
from bookie_emulator.api.envelope import Envelope, envelope
from bookie_emulator.api.schemas import HealthData
from bookie_emulator.services.health import HealthService

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Envelope[HealthData])
async def get_health(service: Annotated[HealthService, Depends(get_health_service)]) -> Envelope[HealthData]:
    """Liveness plus dependency status. Returns 200 even when degraded so
    container healthchecks measure this service, not its dependencies."""
    return envelope(await service.health())
