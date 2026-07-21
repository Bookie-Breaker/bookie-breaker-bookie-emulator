"""ServiceClient envelope unwrapping and error mapping, plus the typed
lines/statistics clients' request assembly, over an httpx.MockTransport."""

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from bookie_emulator.api.errors import DependencyError, DependencyTimeoutError, NotFoundError
from bookie_emulator.clients.base import ServiceClient
from bookie_emulator.clients.lines import LinesClient
from bookie_emulator.clients.statistics import StatisticsClient

BASE_URL = "http://upstream.test"


def enveloped(data: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"timestamp": "2026-07-20T12:00:00Z", "request_id": "req-test"}}


def transport_returning(status_code: int, body: dict[str, Any] | list[Any] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body if body is not None else {})

    return httpx.MockTransport(handler)


def transport_raising(error: Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return httpx.MockTransport(handler)


def capture_transport(body: dict[str, Any] | list[Any]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=enveloped(body) if not isinstance(body, dict) or "data" not in body else body)

    return httpx.MockTransport(handler), requests


def make_client[T: ServiceClient](cls: Callable[[str, httpx.AsyncClient], T], transport: httpx.MockTransport) -> T:
    return cls(BASE_URL + "/", httpx.AsyncClient(transport=transport))


def line_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "game_id": "ext-1",
        "sportsbook_key": "pinnacle",
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "odds_american": -110,
        "odds_decimal": 1.909,
        "timestamp": "2026-07-20T12:00:00Z",
    }
    payload.update(overrides)
    return payload


class TestGetData:
    async def test_unwraps_the_data_payload(self) -> None:
        client = make_client(ServiceClient, transport_returning(200, enveloped({"value": 7})))
        assert await client.get_data("/thing", "thing") == {"value": 7}

    async def test_404_raises_not_found(self) -> None:
        client = make_client(ServiceClient, transport_returning(404))
        with pytest.raises(NotFoundError, match="thing not found in upstream"):
            await client.get_data("/thing", "thing")

    async def test_5xx_raises_dependency_error(self) -> None:
        client = make_client(ServiceClient, transport_returning(503))
        with pytest.raises(DependencyError, match="returned 503"):
            await client.get_data("/thing", "thing")

    async def test_timeout_raises_dependency_timeout(self) -> None:
        client = make_client(ServiceClient, transport_raising(httpx.ReadTimeout("slow")))
        with pytest.raises(DependencyTimeoutError, match="timed out fetching thing"):
            await client.get_data("/thing", "thing")

    async def test_transport_failure_raises_dependency_error(self) -> None:
        client = make_client(ServiceClient, transport_raising(httpx.ConnectError("refused")))
        with pytest.raises(DependencyError, match="is unavailable"):
            await client.get_data("/thing", "thing")

    async def test_missing_data_key_raises_dependency_error(self) -> None:
        client = make_client(ServiceClient, transport_returning(200, {"nope": 1}))
        with pytest.raises(DependencyError, match="malformed envelope"):
            await client.get_data("/thing", "thing")


class TestIsHealthy:
    async def test_200_is_healthy(self) -> None:
        client = make_client(ServiceClient, transport_returning(200, {"status": "healthy"}))
        assert await client.is_healthy("/health") is True

    async def test_non_200_is_unhealthy(self) -> None:
        client = make_client(ServiceClient, transport_returning(500))
        assert await client.is_healthy("/health") is False

    async def test_transport_failure_is_unhealthy(self) -> None:
        client = make_client(ServiceClient, transport_raising(httpx.ConnectError("refused")))
        assert await client.is_healthy("/health") is False


class TestLinesClient:
    async def test_current_lines_sends_all_filters(self) -> None:
        transport, requests = capture_transport([line_payload()])
        client = make_client(LinesClient, transport)
        lines = await client.current_lines(
            league="EPL", game_id="ext-1", market_type="SPREAD", date="2026-07-20", limit=25
        )
        assert [line.market_type for line in lines] == ["SPREAD"]
        assert lines[0].odds_decimal == 1.909
        request = requests[0]
        assert request.url.path == "/api/v1/lines/current"
        assert dict(request.url.params) == {
            "league": "EPL",
            "limit": "25",
            "game_id": "ext-1",
            "market_type": "SPREAD",
            "date": "2026-07-20",
        }

    async def test_current_lines_omits_unset_filters(self) -> None:
        transport, requests = capture_transport([])
        client = make_client(LinesClient, transport)
        assert await client.current_lines() == []
        assert dict(requests[0].url.params) == {"league": "NBA", "limit": "200"}

    async def test_game_lines_with_market_filter(self) -> None:
        transport, requests = capture_transport([line_payload()])
        client = make_client(LinesClient, transport)
        lines = await client.game_lines("ext-1", market_type="SPREAD")
        assert lines[0].game_id == "ext-1"
        assert requests[0].url.path == "/api/v1/lines/game/ext-1"
        assert dict(requests[0].url.params) == {"market_type": "SPREAD"}

    async def test_best_lines_defaults_have_no_params(self) -> None:
        best = {
            "market_type": "SPREAD",
            "selection": "Los Angeles Lakers -3.5",
            "side": "HOME",
            "line_value": -3.5,
            "best_odds_american": -105,
            "best_odds_decimal": 1.952,
            "sportsbook_key": "pinnacle",
            "timestamp": "2026-07-20T12:00:00Z",
        }
        transport, requests = capture_transport([best])
        client = make_client(LinesClient, transport)
        lines = await client.best_lines("ext-1")
        assert lines[0].best_odds_american == -105
        assert requests[0].url.path == "/api/v1/lines/game/ext-1/best"
        assert dict(requests[0].url.params) == {}

    async def test_game_lines_defaults_have_no_params(self) -> None:
        transport, requests = capture_transport([])
        client = make_client(LinesClient, transport)
        assert await client.game_lines("ext-1") == []
        assert dict(requests[0].url.params) == {}

    async def test_best_lines_with_market_filter(self) -> None:
        transport, requests = capture_transport([])
        client = make_client(LinesClient, transport)
        assert await client.best_lines("ext-1", market_type="TOTAL") == []
        assert dict(requests[0].url.params) == {"market_type": "TOTAL"}

    async def test_closing_lines_defaults_have_no_params(self) -> None:
        transport, requests = capture_transport([])
        client = make_client(LinesClient, transport)
        assert await client.closing_lines("ext-1") == []
        assert dict(requests[0].url.params) == {}

    async def test_closing_lines_with_market_filter(self) -> None:
        transport, requests = capture_transport([line_payload(is_closing=True)])
        client = make_client(LinesClient, transport)
        lines = await client.closing_lines("ext-1", market_type="SPREAD")
        assert lines[0].is_closing is True
        assert requests[0].url.path == "/api/v1/lines/game/ext-1/closing"
        assert dict(requests[0].url.params) == {"market_type": "SPREAD"}

    async def test_health_hits_the_lines_health_path(self) -> None:
        transport, requests = capture_transport({"data": {"status": "healthy"}})
        client = make_client(LinesClient, transport)
        assert await client.health() is True
        assert requests[0].url.path == "/api/v1/lines/health"


class TestStatisticsClient:
    async def test_get_game_parses_the_game(self) -> None:
        game = {
            "id": "game-1",
            "league": "NBA",
            "status": "SCHEDULED",
            "home_team": {"id": "t1", "name": "Los Angeles Lakers", "abbreviation": "LAL"},
            "away_team": {"id": "t2", "name": "Boston Celtics", "abbreviation": "BOS"},
            "scheduled_start": "2026-07-21T00:00:00Z",
            "season": 2026,
        }
        transport, requests = capture_transport(enveloped(game))
        client = make_client(StatisticsClient, transport)
        parsed = await client.get_game("game-1")
        assert parsed.home_team.abbreviation == "LAL"
        assert parsed.result is None
        assert requests[0].url.path == "/api/v1/stats/games/game-1"

    async def test_get_box_score_parses_players(self) -> None:
        box = {
            "game_id": "game-1",
            "sport": "BASKETBALL",
            "status": "FINAL",
            "home_team": {
                "id": "t1",
                "abbreviation": "LAL",
                "score": 110,
                "players": [{"player_id": "p1", "player_name": "Test Player", "points": 31}],
            },
            "away_team": {"id": "t2", "abbreviation": "BOS", "score": 100},
        }
        transport, requests = capture_transport(enveloped(box))
        client = make_client(StatisticsClient, transport)
        parsed = await client.get_box_score("game-1")
        assert parsed.home_team.players[0].points == 31
        assert parsed.away_team.players == []
        assert requests[0].url.path == "/api/v1/stats/games/game-1/box-score"

    async def test_health_hits_the_stats_health_path(self) -> None:
        transport, requests = capture_transport({"data": {"status": "healthy"}})
        client = make_client(StatisticsClient, transport)
        assert await client.health() is True
        assert requests[0].url.path == "/api/v1/stats/health"


class TestBaseUrlNormalization:
    async def test_trailing_slash_is_stripped(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=enveloped([]))

        client = ServiceClient(BASE_URL + "///", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await client.get_data("/path", "path")
        assert seen == [BASE_URL + "/path"]
