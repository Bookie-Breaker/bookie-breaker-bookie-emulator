"""Typed async client for the lines-service REST API (port 8001).

Note: lines-service game_id is the Odds API external id, NOT the
statistics-service game UUID -- see clients/reconcile.py.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from bookie_emulator.clients.base import ServiceClient


class LineSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    game_id: str
    sportsbook_id: str | None = None
    sportsbook_key: str = ""
    league: str = ""
    market_type: str = ""
    selection: str = ""
    side: str = ""
    line_value: float | None = None
    odds_american: int = 0
    odds_decimal: float = 0.0
    implied_probability: float | None = None
    is_opening: bool = False
    is_closing: bool = False
    timestamp: str = ""


class BestLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market_type: str = ""
    selection: str = ""
    side: str = ""
    line_value: float | None = None
    best_odds_american: int = 0
    best_odds_decimal: float = 0.0
    implied_probability: float | None = None
    sportsbook_id: str | None = None
    sportsbook_key: str = ""
    timestamp: str = ""


class LinesClient(ServiceClient):
    service_name = "lines-service"

    async def current_lines(
        self,
        league: str = "NBA",
        game_id: str | None = None,
        market_type: str | None = None,
        date: str | None = None,
        limit: int = 200,
    ) -> list[LineSnapshot]:
        params: dict[str, Any] = {"league": league, "limit": limit}
        if game_id:
            params["game_id"] = game_id
        if market_type:
            params["market_type"] = market_type
        if date:
            params["date"] = date
        data = await self.get_data("/api/v1/lines/current", "current lines", params)
        return [LineSnapshot.model_validate(item) for item in data]

    async def game_lines(self, game_external_id: str, market_type: str | None = None) -> list[LineSnapshot]:
        params: dict[str, Any] = {}
        if market_type:
            params["market_type"] = market_type
        data = await self.get_data(f"/api/v1/lines/game/{game_external_id}", f"lines for {game_external_id}", params)
        return [LineSnapshot.model_validate(item) for item in data]

    async def best_lines(self, game_external_id: str, market_type: str | None = None) -> list[BestLine]:
        params: dict[str, Any] = {}
        if market_type:
            params["market_type"] = market_type
        data = await self.get_data(
            f"/api/v1/lines/game/{game_external_id}/best", f"best lines for {game_external_id}", params
        )
        return [BestLine.model_validate(item) for item in data]

    async def closing_lines(self, game_external_id: str, market_type: str | None = None) -> list[LineSnapshot]:
        params: dict[str, Any] = {}
        if market_type:
            params["market_type"] = market_type
        data = await self.get_data(
            f"/api/v1/lines/game/{game_external_id}/closing", f"closing lines for {game_external_id}", params
        )
        return [LineSnapshot.model_validate(item) for item in data]

    async def health(self) -> bool:
        return await self.is_healthy("/api/v1/lines/health")
