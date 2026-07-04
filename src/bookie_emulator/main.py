"""FastAPI application entry point."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI

from bookie_emulator import __version__
from bookie_emulator.api.envelope import RequestIDMiddleware
from bookie_emulator.api.errors import register_error_handlers
from bookie_emulator.api.routes import bankroll, bets, health, performance
from bookie_emulator.clients.lines import LinesClient
from bookie_emulator.clients.reconcile import GameReconciler
from bookie_emulator.clients.statistics import StatisticsClient
from bookie_emulator.config import Settings, get_settings
from bookie_emulator.db.engine import create_engine
from bookie_emulator.db.repository import BankrollRepository, PaperBetRepository
from bookie_emulator.events.subscriber import GameCompletedSubscriber
from bookie_emulator.services.bankroll import BankrollService
from bookie_emulator.services.bets import BetService
from bookie_emulator.services.grader import GraderService
from bookie_emulator.services.health import HealthService
from bookie_emulator.services.poller import GradingPoller
from bookie_emulator.telemetry import configure_telemetry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Engine/redis construction is lazy (no connections yet), so startup
        # never crash-loops on unmigrated or unavailable dependencies: repos
        # fail lazily per-request and /health reports degraded instead.
        engine = create_engine(settings.database_url)
        redis_client: aioredis.Redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))

        statistics = StatisticsClient(settings.statistics_service_url, http_client)
        lines = LinesClient(settings.lines_service_url, http_client)
        reconciler = GameReconciler(lines, redis_client, ttl_seconds=settings.game_map_ttl_seconds)

        bet_repo = PaperBetRepository(engine)
        bankroll_repo = BankrollRepository(engine)
        grader = GraderService(statistics, lines, bet_repo, settings)
        subscriber = GameCompletedSubscriber(redis_client, grader)
        poller = GradingPoller(
            statistics,
            bet_repo,
            grader,
            poll_seconds=settings.grading_poll_seconds,
            grace_seconds=settings.grading_grace_seconds,
        )

        app.state.settings = settings
        app.state.bet_repo = bet_repo
        app.state.bankroll_repo = bankroll_repo
        app.state.bet_service = BetService(statistics, lines, reconciler, bet_repo, settings)
        app.state.grader = grader
        app.state.bankroll_service = BankrollService(bet_repo, bankroll_repo, settings)
        app.state.health_service = HealthService(statistics, lines, bet_repo, redis_client, subscriber)
        app.state.subscriber = subscriber
        app.state.poller = poller

        subscriber_task = asyncio.create_task(subscriber.run(), name="game-completed-subscriber")
        poller_task = asyncio.create_task(poller.run(), name="grading-poller")
        try:
            yield
        finally:
            for task in (subscriber_task, poller_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await http_client.aclose()
            await redis_client.aclose()
            await engine.dispose()

    app = FastAPI(
        title="BookieBreaker Bookie Emulator",
        version=__version__,
        description="Paper trading: places virtual bets on detected edges, grades them on game "
        "completion, and tracks ROI, CLV, calibration, and bankroll performance.",
        contact={
            "name": "BookieBreaker",
            "url": "https://github.com/Bookie-Breaker",
            "email": "jsamuelsen11@gmail.com",
        },
        license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
        servers=[{"url": "http://localhost:8005", "description": "Local development"}],
        openapi_tags=[
            {"name": "bets", "description": "Place, list, inspect, and manually grade paper bets."},
            {"name": "performance", "description": "Aggregate and grouped performance metrics."},
            {"name": "bankroll", "description": "Bankroll state and snapshot history."},
            {"name": "health", "description": "Service health, dependencies, and bet stats."},
        ],
        lifespan=lifespan,
    )
    app.add_middleware(RequestIDMiddleware)
    register_error_handlers(app)
    app.include_router(bets.router, prefix="/api/v1/emulator")
    app.include_router(performance.router, prefix="/api/v1/emulator")
    app.include_router(bankroll.router, prefix="/api/v1/emulator")
    app.include_router(health.router, prefix="/api/v1/emulator")
    configure_telemetry(app, settings)
    return app


app = create_app()
