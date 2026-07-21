"""create_app wiring: lifespan service construction/teardown, router mounts,
middleware, and the degraded-health guarantee when no dependency is up.

Dependency URLs point at closed local ports: engine/redis construction is
lazy by design, so startup succeeds and /health reports degraded instead of
crash-looping (see main.py)."""

from fastapi.testclient import TestClient

from bookie_emulator.config import Settings, get_settings
from bookie_emulator.main import create_app
from bookie_emulator.services.bets import BetService
from bookie_emulator.services.grader import GraderService


def unreachable_settings() -> Settings:
    # port 1 is never listening locally: every dependency fails fast
    return Settings(
        _env_file=None,
        database_url="postgres://svc:pw@127.0.0.1:1/bookie?search_path=emulator,public",
        redis_url="redis://127.0.0.1:1",
        lines_service_url="http://127.0.0.1:1",
        statistics_service_url="http://127.0.0.1:1",
    )


class TestCreateApp:
    def test_mounts_all_routers_with_prefix(self) -> None:
        app = create_app(unreachable_settings())
        paths = set(app.openapi()["paths"])
        assert {
            "/api/v1/emulator/bets",
            "/api/v1/emulator/bets/{bet_id}",
            "/api/v1/emulator/bets/{bet_id}/grade",
            "/api/v1/emulator/parlays",
            "/api/v1/emulator/parlays/{bet_id}",
            "/api/v1/emulator/performance",
            "/api/v1/emulator/performance/calibration",
            "/api/v1/emulator/performance/breakdown",
            "/api/v1/emulator/bankroll",
            "/api/v1/emulator/bankroll/history",
            "/api/v1/emulator/health",
        } <= paths

    def test_lifespan_wires_state_and_health_degrades_without_dependencies(self) -> None:
        app = create_app(unreachable_settings())
        with TestClient(app) as client:
            assert isinstance(app.state.bet_service, BetService)
            assert isinstance(app.state.grader, GraderService)
            response = client.get("/api/v1/emulator/health")
            assert response.status_code == 200  # degraded, never 5xx
            data = response.json()["data"]
            assert data["status"] == "degraded"
            assert data["dependencies"] == {
                "postgres": "unhealthy",
                "redis": "unhealthy",
                "lines_service": "unhealthy",
                "statistics_service": "unhealthy",
            }
            # the DB is down, so bet stats fall back to zeros
            assert data["stats"] == {"open_bets": 0, "bets_today": 0, "graded_today": 0}
        # exiting the context cancels the subscriber/poller tasks cleanly

    def test_openapi_document_carries_the_service_contract(self) -> None:
        app = create_app(unreachable_settings())
        with TestClient(app) as client:
            spec = client.get("/openapi.json").json()
        assert spec["info"]["title"] == "BookieBreaker Bookie Emulator"
        assert [tag["name"] for tag in spec["tags"]] == ["bankroll", "bets", "health", "parlays", "performance"]

    def test_request_id_middleware_is_installed(self) -> None:
        app = create_app(unreachable_settings())
        with TestClient(app) as client:
            response = client.get("/api/v1/emulator/health", headers={"X-Request-ID": "req-lifespan"})
        assert response.headers["X-Request-ID"] == "req-lifespan"


class TestGetSettings:
    def test_settings_are_cached(self) -> None:
        assert get_settings() is get_settings()
