"""Typed async client for the statistics-service REST API (port 8002)."""

from pydantic import BaseModel, ConfigDict

from bookie_emulator.clients.base import ServiceClient


class TeamRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    abbreviation: str = ""


class GameResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    home_score: int
    away_score: int
    total_score: int = 0
    margin: int = 0
    overtime: bool = False
    # 90-minute regulation scores, populated only for soccer matches that
    # went to extra time (ADR-027); soccer markets settle on these
    regulation_home_score: int | None = None
    regulation_away_score: int | None = None
    completed_at: str = ""


class Game(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    league: str
    status: str
    home_team: TeamRef
    away_team: TeamRef
    scheduled_start: str = ""
    season: int = 0
    home_score: int | None = None
    away_score: int | None = None
    result: GameResult | None = None


class StatisticsClient(ServiceClient):
    service_name = "statistics-service"

    async def get_game(self, game_id: str) -> Game:
        data = await self.get_data(f"/api/v1/stats/games/{game_id}", f"game {game_id}")
        return Game.model_validate(data)

    async def health(self) -> bool:
        return await self.is_healthy("/api/v1/stats/health")
