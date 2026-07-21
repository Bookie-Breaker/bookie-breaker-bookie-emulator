"""BetService placement paths not exercised elsewhere: stake/game/market
validation errors, bankroll rejection, sportsbook id parsing, and the full
place_parlay flow (parent-row conventions per ADR-028)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bookie_emulator.api.errors import NotFoundError, UnprocessableError
from bookie_emulator.api.schemas import ParlayLegRequest, PlaceBetRequest, PlaceParlayRequest
from bookie_emulator.clients.lines import BestLine
from bookie_emulator.clients.statistics import Game, TeamRef
from bookie_emulator.config import Settings
from bookie_emulator.services.bets import BetService

EXT_ID = "odds-ext-1"
BOOK_UUID = "00000000-0000-4000-8000-00000000aaaa"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def future_start(hours: float = 6.0) -> str:
    return iso(datetime.now(tz=UTC) + timedelta(hours=hours))


def make_game(scheduled_start: str | None = None, league: str = "NBA") -> Game:
    return Game(
        id=str(uuid.uuid4()),
        league=league,
        status="SCHEDULED",
        home_team=TeamRef(id="team-home", name="Los Angeles Lakers", abbreviation="LAL"),
        away_team=TeamRef(id="team-away", name="Boston Celtics", abbreviation="BOS"),
        scheduled_start=scheduled_start or future_start(),
        season=2026,
    )


def best_line(**overrides: Any) -> BestLine:
    defaults: dict[str, Any] = {
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "best_odds_american": 100,
        "best_odds_decimal": 2.0,
        "sportsbook_id": BOOK_UUID,
        "sportsbook_key": "pinnacle",
        "timestamp": "2026-07-19T12:00:00Z",
    }
    defaults.update(overrides)
    return BestLine(**defaults)


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


def leg_request(**overrides: Any) -> ParlayLegRequest:
    defaults: dict[str, Any] = {
        "game_id": uuid.uuid4(),
        "game_external_id": EXT_ID,
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
    }
    defaults.update(overrides)
    return ParlayLegRequest(**defaults)


def parlay_request(legs: list[ParlayLegRequest] | None = None, **overrides: Any) -> PlaceParlayRequest:
    defaults: dict[str, Any] = {
        "legs": legs or [leg_request(), leg_request()],
        "predicted_probability": 0.28,
        "edge_percentage": 3.1,
        "stake": 1.0,
    }
    defaults.update(overrides)
    return PlaceParlayRequest(**defaults)


class StubStatistics:
    def __init__(self, game: Game | None = None, error: Exception | None = None) -> None:
        self.game = game
        self.error = error

    async def get_game(self, game_id: str) -> Game:
        if self.error is not None:
            raise self.error
        assert self.game is not None
        return self.game


class StubLines:
    def __init__(self, best: list[BestLine] | None = None, error: Exception | None = None) -> None:
        self.best = best if best is not None else [best_line()]
        self.error = error

    async def best_lines(self, game_external_id: str, market_type: str | None = None) -> list[BestLine]:
        if self.error is not None:
            raise self.error
        return self.best

    async def game_lines(self, game_external_id: str, market_type: str | None = None) -> list[Any]:
        return []


class StubReconciler:
    def __init__(self, external_id: str | None = EXT_ID) -> None:
        self.external_id = external_id

    async def resolve(self, game: Game) -> str | None:
        return self.external_id


class StubRepo:
    """Captures inserts; bankroll figures are configurable per test."""

    def __init__(self, profit: float = 0.0, exposure: float = 0.0) -> None:
        self.profit = profit
        self.exposure = exposure
        self.values: dict[str, Any] | None = None
        self.parent_values: dict[str, Any] | None = None
        self.leg_values: list[dict[str, Any]] | None = None

    async def insert_idempotent(self, values: dict[str, Any]) -> tuple[Any, bool]:
        self.values = values
        return values, True

    async def insert_parlay(
        self, parent_values: dict[str, Any], leg_values: list[dict[str, Any]]
    ) -> tuple[Any, list[Any], bool]:
        self.parent_values = parent_values
        self.leg_values = leg_values
        return parent_values, leg_values, True

    async def total_profit(self) -> float:
        return self.profit

    async def open_exposure(self) -> float:
        return self.exposure


def make_service(
    game: Game | None = None,
    lines: StubLines | None = None,
    reconciler: StubReconciler | None = None,
    repo: StubRepo | None = None,
    statistics: StubStatistics | None = None,
    settings: Settings | None = None,
) -> tuple[BetService, StubRepo]:
    repo = repo or StubRepo()
    service = BetService(
        statistics or StubStatistics(game or make_game()),  # type: ignore[arg-type]
        lines or StubLines(),  # type: ignore[arg-type]
        reconciler or StubReconciler(),  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        settings or Settings(_env_file=None),
    )
    return service, repo


class TestPlaceBetValidation:
    @pytest.mark.parametrize("stake", [0.0, -1.5])
    async def test_non_positive_stake_422(self, stake: float) -> None:
        service, _ = make_service()
        with pytest.raises(UnprocessableError, match="Stake must be positive"):
            await service.place_bet(make_request(stake=stake), idempotency_key="key-1")

    async def test_game_missing_in_statistics_422(self) -> None:
        service, _ = make_service(statistics=StubStatistics(error=NotFoundError("no game")))
        with pytest.raises(UnprocessableError, match="not found in statistics-service"):
            await service.place_bet(make_request(), idempotency_key="key-1")

    async def test_unreconciled_game_422(self) -> None:
        service, _ = make_service(reconciler=StubReconciler(external_id=None))
        with pytest.raises(UnprocessableError, match="could not be matched"):
            await service.place_bet(make_request(game_external_id=None), idempotency_key="key-1")

    async def test_lines_404_becomes_422(self) -> None:
        service, _ = make_service(lines=StubLines(error=NotFoundError("no lines")))
        with pytest.raises(UnprocessableError, match="No market data exists"):
            await service.place_bet(make_request(), idempotency_key="key-1")

    async def test_stake_over_available_bankroll_422_with_details(self) -> None:
        # bankroll 100 - 10 profit... starting 100 + (-40) profit - 55 exposure = 5 available
        repo = StubRepo(profit=-40.0, exposure=55.0)
        service, _ = make_service(repo=repo)
        with pytest.raises(UnprocessableError, match="exceeds available bankroll") as exc_info:
            await service.place_bet(make_request(stake=6.0), idempotency_key="key-1")
        assert exc_info.value.details == {"available_units": 5.0, "bankroll_units": 60.0}


class TestPlaceBetCapturedValues:
    async def test_valid_sportsbook_uuid_is_parsed(self) -> None:
        service, repo = make_service()
        await service.place_bet(make_request(), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["sportsbook_id"] == uuid.UUID(BOOK_UUID)
        assert repo.values["sportsbook_key"] == "pinnacle"
        assert repo.values["odds_american"] == 100
        assert repo.values["edge_at_placement"] == pytest.approx(0.042)

    async def test_unparseable_sportsbook_id_stored_as_null(self) -> None:
        service, repo = make_service(lines=StubLines(best=[best_line(sportsbook_id="not-a-uuid")]))
        await service.place_bet(make_request(), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["sportsbook_id"] is None

    async def test_kelly_defaults_to_settings_fraction(self) -> None:
        service, repo = make_service(settings=Settings(_env_file=None, kelly_fraction=0.5))
        await service.place_bet(make_request(), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["kelly_fraction"] == 0.5

    async def test_explicit_kelly_overrides_settings(self) -> None:
        service, repo = make_service(settings=Settings(_env_file=None, kelly_fraction=0.5))
        await service.place_bet(make_request(kelly_fraction=0.1), idempotency_key="key-1")
        assert repo.values is not None
        assert repo.values["kelly_fraction"] == 0.1


class TestPlaceParlay:
    @pytest.mark.parametrize("stake", [0.0, -2.0])
    async def test_non_positive_stake_422(self, stake: float) -> None:
        service, _ = make_service()
        with pytest.raises(UnprocessableError, match="Stake must be positive"):
            await service.place_parlay(parlay_request(stake=stake), idempotency_key="key-1")

    async def test_started_leg_game_422(self) -> None:
        started = iso(datetime.now(tz=UTC) - timedelta(hours=1))
        service, _ = make_service(game=make_game(scheduled_start=started))
        with pytest.raises(UnprocessableError, match=r"\(leg 0\) has already started"):
            await service.place_parlay(parlay_request(), idempotency_key="key-1")

    async def test_unreconciled_leg_game_422(self) -> None:
        legs = [leg_request(game_external_id=None), leg_request()]
        service, _ = make_service(reconciler=StubReconciler(external_id=None))
        with pytest.raises(UnprocessableError, match=r"No market data exists for game .* \(leg 0\)"):
            await service.place_parlay(parlay_request(legs=legs), idempotency_key="key-1")

    async def test_stake_over_available_bankroll_422(self) -> None:
        repo = StubRepo(exposure=99.5)
        service, _ = make_service(repo=repo)
        with pytest.raises(UnprocessableError, match="exceeds available bankroll"):
            await service.place_parlay(parlay_request(stake=1.0), idempotency_key="key-1")

    async def test_parent_row_conventions_uniform_book(self) -> None:
        legs = [
            leg_request(selection="Los Angeles Lakers -3.5"),
            leg_request(market_type="TOTAL", side="OVER", selection="Over 220.5"),
        ]
        service, repo = make_service(
            lines=StubLines(
                best=[
                    best_line(),
                    best_line(market_type="TOTAL", side="OVER", selection="Over 220.5", line_value=220.5),
                ]
            )
        )
        parent, leg_rows, created = await service.place_parlay(parlay_request(legs=legs), idempotency_key="key-1")
        assert created is True
        assert repo.parent_values is not None and repo.leg_values is not None
        # parent conventions (ADR-028): NULL game refs, first-leg mirrors, combined price
        assert repo.parent_values["game_id"] is None
        assert repo.parent_values["side"] is None
        assert repo.parent_values["line_value"] is None
        assert repo.parent_values["is_parlay"] is True
        assert repo.parent_values["game_external_id"] == f"parlay:{EXT_ID}+1"
        assert repo.parent_values["market_type"] == "SPREAD"
        assert repo.parent_values["league"] == "NBA"
        assert repo.parent_values["selection"].startswith("2-leg parlay: Los Angeles Lakers -3.5 + Over 220.5")
        # both legs at 2.0 decimal: combined 4.0 decimal = +300 american
        assert repo.parent_values["odds_decimal"] == 4.0
        assert repo.parent_values["odds_american"] == 300
        assert repo.parent_values["sportsbook_key"] == "pinnacle"
        assert repo.parent_values["sportsbook_id"] == uuid.UUID(BOOK_UUID)
        assert repo.parent_values["idempotency_key"] == "key-1"
        assert repo.parent_values["game_start_at"] is not None
        assert [leg["leg_index"] for leg in repo.leg_values] == [0, 1]
        assert repo.leg_values[1]["market_type"] == "TOTAL"

    async def test_mixed_books_collapse_to_mixed_and_null_id(self) -> None:
        legs = [
            leg_request(selection="Los Angeles Lakers -3.5"),
            leg_request(market_type="TOTAL", side="OVER", selection="Over 220.5"),
        ]

        class AlternatingLines(StubLines):
            def __init__(self) -> None:
                super().__init__(best=[])
                self.calls = 0

            async def best_lines(self, game_external_id: str, market_type: str | None = None) -> list[BestLine]:
                self.calls += 1
                if self.calls == 1:
                    return [best_line()]
                return [best_line(market_type="TOTAL", side="OVER", selection="Over 220.5", sportsbook_key="fanduel")]

        service, repo = make_service(lines=AlternatingLines())
        await service.place_parlay(parlay_request(legs=legs), idempotency_key="key-1")
        assert repo.parent_values is not None
        assert repo.parent_values["sportsbook_key"] == "mixed"
        assert repo.parent_values["sportsbook_id"] is None

    async def test_long_selection_summary_is_truncated(self) -> None:
        long_name = "Los Angeles Lakers -3.5 " + "x" * 300
        legs = [
            leg_request(selection=long_name),
            leg_request(game_id=uuid.uuid4(), selection=long_name),
        ]
        service, repo = make_service(lines=StubLines(best=[best_line(selection=long_name)]))
        await service.place_parlay(parlay_request(legs=legs), idempotency_key="key-1")
        assert repo.parent_values is not None
        assert len(repo.parent_values["selection"]) == 500
        assert repo.parent_values["selection"].endswith("…")

    async def test_unparseable_starts_leave_game_start_null(self) -> None:
        service, repo = make_service(game=make_game(scheduled_start="not-a-timestamp"))
        await service.place_parlay(parlay_request(), idempotency_key="key-1")
        assert repo.parent_values is not None
        assert repo.parent_values["game_start_at"] is None

    async def test_earliest_leg_start_wins(self) -> None:
        early = datetime.now(tz=UTC) + timedelta(hours=2)

        class TwoGames(StubStatistics):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def get_game(self, game_id: str) -> Game:
                self.calls += 1
                hours = 2.0 if self.calls == 1 else 8.0
                return make_game(scheduled_start=future_start(hours))

        service, repo = make_service(statistics=TwoGames())
        await service.place_parlay(parlay_request(), idempotency_key="key-1")
        assert repo.parent_values is not None
        start_at = repo.parent_values["game_start_at"]
        assert abs((start_at - early).total_seconds()) < 60

    async def test_explicit_kelly_carries_to_parent(self) -> None:
        service, repo = make_service()
        await service.place_parlay(parlay_request(kelly_fraction=0.05), idempotency_key="key-1")
        assert repo.parent_values is not None
        assert repo.parent_values["kelly_fraction"] == 0.05
