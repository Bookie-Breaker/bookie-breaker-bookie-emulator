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


class SoccerPlayerBoxScore(BaseModel):
    model_config = ConfigDict(extra="ignore")

    player_id: str
    player_name: str
    position: str = ""
    minutes: int = 0
    goals: int = 0
    assists: int = 0
    shots: int = 0
    shots_on_target: int = 0
    yellow_cards: int = 0
    red_cards: int = 0


class BasketballPlayerBoxScore(BaseModel):
    model_config = ConfigDict(extra="ignore")

    player_id: str
    player_name: str
    position: str = ""
    minutes: int = 0
    points: int = 0
    rebounds: int = 0
    assists: int = 0
    three_pointers_made: int = 0


class BaseballPlayerBoxScore(BaseModel):
    model_config = ConfigDict(extra="ignore")

    player_id: str
    player_name: str
    position: str = ""
    at_bats: int = 0
    hits: int = 0
    total_bases: int = 0
    home_runs: int = 0
    strikeouts_pitching: int = 0


PlayerBoxScoreLine = SoccerPlayerBoxScore | BasketballPlayerBoxScore | BaseballPlayerBoxScore


class TeamBoxScore(BaseModel):
    """One team's side of a box score; exactly one player array is populated
    per sport (players: basketball, soccer_players, baseball_players)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    abbreviation: str = ""
    score: int = 0
    players: list[BasketballPlayerBoxScore] = []
    soccer_players: list[SoccerPlayerBoxScore] = []
    baseball_players: list[BaseballPlayerBoxScore] = []


class BoxScore(BaseModel):
    model_config = ConfigDict(extra="ignore")

    game_id: str
    sport: str  # "SOCCER" | "BASKETBALL" | "BASEBALL"
    status: str = ""
    home_team: TeamBoxScore
    away_team: TeamBoxScore


class StatisticsClient(ServiceClient):
    service_name = "statistics-service"

    async def get_game(self, game_id: str) -> Game:
        data = await self.get_data(f"/api/v1/stats/games/{game_id}", f"game {game_id}")
        return Game.model_validate(data)

    async def get_box_score(self, game_id: str) -> BoxScore:
        """Per-player box score for prop grading. Raises NotFoundError while
        the box score is not yet available (grading retries via the poller)."""
        data = await self.get_data(f"/api/v1/stats/games/{game_id}/box-score", f"box score for game {game_id}")
        return BoxScore.model_validate(data)

    async def health(self) -> bool:
        return await self.is_healthy("/api/v1/stats/health")
