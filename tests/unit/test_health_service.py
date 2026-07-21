"""HealthService aggregation: dependency fan-out, degraded states, and the
DB-down fallbacks that keep /health from ever failing."""

from typing import Any

from bookie_emulator.services.health import HealthService


class StubClient:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    async def health(self) -> bool:
        return self.healthy


class StubRepo:
    def __init__(
        self,
        healthy: bool = True,
        stats: tuple[int, int, int] = (3, 5, 2),
        stats_error: Exception | None = None,
    ) -> None:
        self.healthy = healthy
        self.stats = stats
        self.stats_error = stats_error

    async def is_healthy(self) -> bool:
        return self.healthy

    async def health_stats(self) -> tuple[int, int, int]:
        if self.stats_error is not None:
            raise self.stats_error
        return self.stats


class StubRedis:
    def __init__(self, ping_result: Any = True, error: Exception | None = None) -> None:
        self.ping_result = ping_result
        self.error = error

    async def ping(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.ping_result


class StubSubscriber:
    status = "running"


def make_service(
    statistics: StubClient | None = None,
    lines: StubClient | None = None,
    repo: StubRepo | None = None,
    redis_client: StubRedis | None = None,
) -> HealthService:
    return HealthService(
        statistics or StubClient(),  # type: ignore[arg-type]
        lines or StubClient(),  # type: ignore[arg-type]
        repo or StubRepo(),  # type: ignore[arg-type]
        redis_client or StubRedis(),  # type: ignore[arg-type]
        StubSubscriber(),  # type: ignore[arg-type]
    )


class TestHealth:
    async def test_all_dependencies_healthy(self) -> None:
        data = await make_service().health()
        assert data.status == "healthy"
        assert data.dependencies == {
            "postgres": "healthy",
            "redis": "healthy",
            "lines_service": "healthy",
            "statistics_service": "healthy",
        }
        assert data.subscriber == "running"
        assert data.uptime_seconds >= 0
        assert data.stats.open_bets == 3
        assert data.stats.bets_today == 5
        assert data.stats.graded_today == 2

    async def test_redis_failure_degrades(self) -> None:
        data = await make_service(redis_client=StubRedis(error=ConnectionError("redis gone"))).health()
        assert data.status == "degraded"
        assert data.dependencies["redis"] == "unhealthy"
        assert data.dependencies["postgres"] == "healthy"

    async def test_falsy_redis_ping_degrades(self) -> None:
        data = await make_service(redis_client=StubRedis(ping_result=False)).health()
        assert data.status == "degraded"
        assert data.dependencies["redis"] == "unhealthy"

    async def test_upstream_service_failures_degrade(self) -> None:
        data = await make_service(statistics=StubClient(healthy=False), lines=StubClient(healthy=False)).health()
        assert data.status == "degraded"
        assert data.dependencies["statistics_service"] == "unhealthy"
        assert data.dependencies["lines_service"] == "unhealthy"

    async def test_db_stats_failure_zeroes_stats_without_500(self) -> None:
        repo = StubRepo(healthy=False, stats_error=RuntimeError("db down"))
        data = await make_service(repo=repo).health()
        assert data.status == "degraded"
        assert data.dependencies["postgres"] == "unhealthy"
        assert (data.stats.open_bets, data.stats.bets_today, data.stats.graded_today) == (0, 0, 0)
