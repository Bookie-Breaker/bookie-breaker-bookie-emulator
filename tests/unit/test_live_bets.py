"""Phase 7 Wave 2: live (in-game) bet placement.

Covers the placement validation matrix (status vocabulary from the
statistics-service game_status_enum), live-line preference at odds capture
with its freshest-available fallback, the persisted is_live flag, and the
ledger filter condition.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bookie_emulator.api.errors import UnprocessableError
from bookie_emulator.api.schemas import PlaceBetRequest
from bookie_emulator.clients.lines import BestLine, LineSnapshot
from bookie_emulator.clients.statistics import Game, TeamRef
from bookie_emulator.config import Settings
from bookie_emulator.db.repository import LedgerFilters, _filter_conditions
from bookie_emulator.services.bets import BetService

EXT_ID = "odds-ext-1"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_game(status: str = "IN_PROGRESS", start_hours_ago: float = 1.0, scheduled_start: str | None = None) -> Game:
    if scheduled_start is None:
        scheduled_start = iso(datetime.now(tz=UTC) - timedelta(hours=start_hours_ago))
    return Game(
        id=str(uuid.uuid4()),
        league="NBA",
        status=status,
        home_team=TeamRef(id="team-home", name="Los Angeles Lakers", abbreviation="LAL"),
        away_team=TeamRef(id="team-away", name="Boston Celtics", abbreviation="BOS"),
        scheduled_start=scheduled_start,
        season=2026,
    )


def best_line(**overrides: Any) -> BestLine:
    defaults: dict[str, Any] = {
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "best_odds_american": -105,
        "best_odds_decimal": 1.952,
        "sportsbook_id": None,
        "sportsbook_key": "pinnacle",
        "is_live": False,
        "timestamp": "2026-07-19T12:00:00Z",
    }
    defaults.update(overrides)
    return BestLine(**defaults)


def snapshot(**overrides: Any) -> LineSnapshot:
    defaults: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "game_id": EXT_ID,
        "sportsbook_id": None,
        "sportsbook_key": "draftkings",
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "odds_american": -110,
        "odds_decimal": 1.909,
        "is_live": False,
        "timestamp": "2026-07-19T12:00:00Z",
    }
    defaults.update(overrides)
    return LineSnapshot(**defaults)


def make_request(**overrides: Any) -> PlaceBetRequest:
    defaults: dict[str, Any] = {
        "game_id": uuid.uuid4(),
        "game_external_id": EXT_ID,
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "predicted_probability": 0.5712,
        "edge_percentage": 4.2,
        "stake": 1.0,
    }
    defaults.update(overrides)
    return PlaceBetRequest(**defaults)


class StubStatistics:
    def __init__(self, game: Game) -> None:
        self.game = game

    async def get_game(self, game_id: str) -> Game:
        return self.game


class StubLines:
    def __init__(self, best: list[BestLine] | None = None, snapshots: list[LineSnapshot] | None = None) -> None:
        self.best = best or []
        self.snapshots = snapshots or []

    async def best_lines(self, game_external_id: str, market_type: str | None = None) -> list[BestLine]:
        return self.best

    async def game_lines(self, game_external_id: str, market_type: str | None = None) -> list[LineSnapshot]:
        return self.snapshots


class StubReconciler:
    async def resolve(self, game: Game) -> str | None:
        return EXT_ID


class StubRepo:
    """Captures the insert values dict; bankroll checks always pass."""

    def __init__(self) -> None:
        self.values: dict[str, Any] | None = None

    async def insert_idempotent(self, values: dict[str, Any]) -> tuple[Any, bool]:
        self.values = values
        return values, True

    async def total_profit(self) -> float:
        return 0.0

    async def open_exposure(self) -> float:
        return 0.0


def make_service(game: Game, lines: StubLines | None = None) -> tuple[BetService, StubRepo]:
    repo = StubRepo()
    lines = lines or StubLines(best=[best_line()])
    service = BetService(
        StubStatistics(game),  # type: ignore[arg-type]
        lines,  # type: ignore[arg-type]
        StubReconciler(),  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        Settings(_env_file=None),
    )
    return service, repo


class TestLivePlacementValidation:
    """Status matrix: SCHEDULED / IN_PROGRESS / FINAL (plus abandoned states)."""

    async def test_live_bet_on_in_progress_game_places(self) -> None:
        service, repo = make_service(make_game(status="IN_PROGRESS"))
        await service.place_bet(make_request(is_live=True), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["is_live"] is True

    async def test_live_bet_on_scheduled_game_still_allowed(self) -> None:
        """A live-flagged pregame placement is harmless (spec'd behavior)."""
        future = iso(datetime.now(tz=UTC) + timedelta(hours=6))
        service, repo = make_service(make_game(status="SCHEDULED", scheduled_start=future))
        await service.place_bet(make_request(is_live=True), idempotency_key="key-1")
        assert repo.values is not None and repo.values["is_live"] is True

    async def test_live_bet_on_final_game_422(self) -> None:
        service, _ = make_service(make_game(status="FINAL"))
        with pytest.raises(UnprocessableError, match="has ended"):
            await service.place_bet(make_request(is_live=True), idempotency_key="key-1")

    @pytest.mark.parametrize("status", ["POSTPONED", "CANCELLED", "SUSPENDED"])
    async def test_live_bet_on_abandoned_game_422(self, status: str) -> None:
        service, _ = make_service(make_game(status=status))
        with pytest.raises(UnprocessableError, match="not open for betting"):
            await service.place_bet(make_request(is_live=True), idempotency_key="key-1")

    async def test_default_bet_on_in_progress_game_still_rejected(self) -> None:
        """Pregame placements keep today's rule: SCHEDULED and not started."""
        service, _ = make_service(make_game(status="IN_PROGRESS"))
        with pytest.raises(UnprocessableError, match="already started"):
            await service.place_bet(make_request(), idempotency_key="key-1")

    async def test_default_bet_on_started_scheduled_game_still_rejected(self) -> None:
        service, _ = make_service(make_game(status="SCHEDULED", start_hours_ago=0.5))
        with pytest.raises(UnprocessableError, match="already started"):
            await service.place_bet(make_request(), idempotency_key="key-1")

    async def test_live_placement_skips_not_started_check(self) -> None:
        """IN_PROGRESS implies the start time has passed; live must not 422 on it."""
        service, repo = make_service(make_game(status="IN_PROGRESS", start_hours_ago=2.0))
        await service.place_bet(make_request(is_live=True), idempotency_key="key-1")
        assert repo.values is not None


class TestPersistedValues:
    async def test_pregame_values_carry_is_live_false(self) -> None:
        future = iso(datetime.now(tz=UTC) + timedelta(hours=6))
        service, repo = make_service(make_game(status="SCHEDULED", scheduled_start=future))
        await service.place_bet(make_request(), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["is_live"] is False

    async def test_live_values_carry_is_live_and_start_time(self) -> None:
        service, repo = make_service(make_game(status="IN_PROGRESS"))
        await service.place_bet(make_request(is_live=True), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["is_live"] is True
        # the (past) start time survives so the fallback grading poller,
        # which gates on game_start_at < cutoff, sweeps live bets normally
        assert repo.values["game_start_at"] is not None
        assert repo.values["game_start_at"] < datetime.now(tz=UTC)

    async def test_live_unparseable_start_falls_back_to_placement_time(self) -> None:
        """A NULL game_start_at would hide the bet from the fallback poller."""
        service, repo = make_service(make_game(status="IN_PROGRESS", scheduled_start="not-a-timestamp"))
        await service.place_bet(make_request(is_live=True), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["game_start_at"] is not None

    def test_request_defaults_to_pregame(self) -> None:
        assert make_request().is_live is False


class TestLiveOddsPreference:
    async def _capture(self, request: PlaceBetRequest, lines: StubLines) -> dict[str, Any]:
        service, repo = make_service(make_game(status="IN_PROGRESS"), lines)
        await service.place_bet(request, idempotency_key="key-1")
        assert repo.values is not None
        return repo.values

    async def test_live_best_line_preferred_over_pregame(self) -> None:
        lines = StubLines(
            best=[
                best_line(best_odds_american=-105, best_odds_decimal=1.952),
                best_line(
                    selection="Los Angeles Lakers -4.5",
                    line_value=-4.5,
                    best_odds_american=-115,
                    best_odds_decimal=1.87,
                    sportsbook_key="draftkings",
                    is_live=True,
                    timestamp="2026-07-19T18:00:00Z",
                ),
            ]
        )
        values = await self._capture(make_request(is_live=True), lines)
        assert values["line_value"] == -4.5
        assert values["odds_american"] == -115
        assert values["sportsbook_key"] == "draftkings"

    async def test_freshest_live_line_wins_among_live(self) -> None:
        lines = StubLines(
            best=[
                best_line(line_value=-5.5, is_live=True, timestamp="2026-07-19T18:30:00Z"),
                best_line(line_value=-4.5, is_live=True, timestamp="2026-07-19T18:00:00Z"),
            ]
        )
        values = await self._capture(make_request(is_live=True), lines)
        assert values["line_value"] == -5.5

    async def test_no_live_lines_falls_back_to_freshest(self) -> None:
        lines = StubLines(
            best=[
                best_line(line_value=-3.0, timestamp="2026-07-19T10:00:00Z"),
                best_line(line_value=-3.5, timestamp="2026-07-19T12:00:00Z"),
            ]
        )
        values = await self._capture(make_request(is_live=True), lines)
        assert values["line_value"] == -3.5

    async def test_pregame_placement_ignores_live_lines(self) -> None:
        """Default placements keep the historical first-match behavior."""
        future = iso(datetime.now(tz=UTC) + timedelta(hours=6))
        lines = StubLines(
            best=[
                best_line(line_value=-3.5),
                best_line(line_value=-4.5, is_live=True, timestamp="2026-07-19T18:00:00Z"),
            ]
        )
        service, repo = make_service(make_game(status="SCHEDULED", scheduled_start=future), lines)
        await service.place_bet(make_request(), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["line_value"] == -3.5

    async def test_pinned_live_snapshot_preferred(self) -> None:
        lines = StubLines(
            snapshots=[
                snapshot(odds_american=-110, odds_decimal=1.909),
                snapshot(
                    line_value=-4.5,
                    odds_american=-118,
                    odds_decimal=1.847,
                    is_live=True,
                    timestamp="2026-07-19T18:00:00Z",
                ),
            ]
        )
        values = await self._capture(make_request(is_live=True, sportsbook_key="draftkings"), lines)
        assert values["line_value"] == -4.5
        assert values["odds_american"] == -118

    async def test_pinned_no_live_falls_back_to_freshest(self) -> None:
        lines = StubLines(
            snapshots=[
                snapshot(line_value=-3.0, timestamp="2026-07-19T10:00:00Z"),
                snapshot(line_value=-3.5, timestamp="2026-07-19T12:00:00Z"),
            ]
        )
        values = await self._capture(make_request(is_live=True, sportsbook_key="draftkings"), lines)
        assert values["line_value"] == -3.5

    async def test_no_matching_line_still_422(self) -> None:
        service, _ = make_service(make_game(status="IN_PROGRESS"), StubLines(best=[]))
        with pytest.raises(UnprocessableError, match="No SPREAD HOME line"):
            await service.place_bet(make_request(is_live=True), idempotency_key="key-1")


class TestLedgerFilter:
    """?is_live= mirrors the Wave 1 is_parlay filter plumbing."""

    def test_is_live_condition_present_when_set(self) -> None:
        conditions = _filter_conditions(LedgerFilters(is_live=True))
        assert len(conditions) == 1
        assert "is_live" in str(conditions[0])

    def test_is_live_condition_absent_by_default(self) -> None:
        assert _filter_conditions(LedgerFilters()) == []
