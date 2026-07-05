"""FastAPI dependency accessors backed by app.state."""

from fastapi import Request

from bookie_emulator.config import Settings
from bookie_emulator.db.repository import PaperBetRepository
from bookie_emulator.services.bankroll import BankrollService
from bookie_emulator.services.bets import BetService
from bookie_emulator.services.grader import GraderService
from bookie_emulator.services.health import HealthService


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_bet_repo(request: Request) -> PaperBetRepository:
    repo: PaperBetRepository = request.app.state.bet_repo
    return repo


def get_bet_service(request: Request) -> BetService:
    service: BetService = request.app.state.bet_service
    return service


def get_grader(request: Request) -> GraderService:
    service: GraderService = request.app.state.grader
    return service


def get_bankroll_service(request: Request) -> BankrollService:
    service: BankrollService = request.app.state.bankroll_service
    return service


def get_health_service(request: Request) -> HealthService:
    service: HealthService = request.app.state.health_service
    return service
