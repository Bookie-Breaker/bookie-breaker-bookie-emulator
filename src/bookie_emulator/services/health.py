"""Health aggregation across dependencies, the event subscriber, and bet stats."""

import asyncio
import time

import redis.asyncio as aioredis

from bookie_emulator import __version__
from bookie_emulator.api.schemas import HealthData, HealthStats
from bookie_emulator.clients.lines import LinesClient
from bookie_emulator.clients.statistics import StatisticsClient
from bookie_emulator.db.repository import PaperBetRepository
from bookie_emulator.events.subscriber import GameCompletedSubscriber


class HealthService:
    def __init__(
        self,
        statistics: StatisticsClient,
        lines: LinesClient,
        repo: PaperBetRepository,
        redis_client: "aioredis.Redis",
        subscriber: GameCompletedSubscriber,
    ) -> None:
        self._statistics = statistics
        self._lines = lines
        self._repo = repo
        self._redis = redis_client
        self._subscriber = subscriber
        self._started = time.monotonic()

    async def _redis_ok(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001 - any redis failure means unhealthy
            return False

    async def _stats(self) -> HealthStats:
        try:
            open_bets, bets_today, graded_today = await self._repo.health_stats()
        except Exception:  # noqa: BLE001 - health must not 500 when the DB is down
            open_bets = bets_today = graded_today = 0
        return HealthStats(open_bets=open_bets, bets_today=bets_today, graded_today=graded_today)

    async def health(self) -> HealthData:
        stats_ok, lines_ok, redis_ok, db_ok = await asyncio.gather(
            self._statistics.health(), self._lines.health(), self._redis_ok(), self._repo.is_healthy()
        )
        healthy = stats_ok and lines_ok and redis_ok and db_ok
        return HealthData(
            status="healthy" if healthy else "degraded",
            version=__version__,
            uptime_seconds=int(time.monotonic() - self._started),
            dependencies={
                "postgres": "healthy" if db_ok else "unhealthy",
                "redis": "healthy" if redis_ok else "unhealthy",
                "lines_service": "healthy" if lines_ok else "unhealthy",
                "statistics_service": "healthy" if stats_ok else "unhealthy",
            },
            subscriber=self._subscriber.status,
            stats=await self._stats(),
        )
