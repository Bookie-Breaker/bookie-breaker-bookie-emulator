"""Game reconciliation matching tests with in-memory fakes."""

from typing import Any

from bookie_emulator.clients.lines import LineSnapshot
from bookie_emulator.clients.reconcile import GameReconciler
from bookie_emulator.clients.statistics import Game, TeamRef


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


class FakeLines:
    def __init__(self, snapshots: list[LineSnapshot]) -> None:
        self._snapshots = snapshots
        self.calls: list[dict[str, Any]] = []

    async def current_lines(self, **kwargs: Any) -> list[LineSnapshot]:
        self.calls.append(kwargs)
        return self._snapshots


def make_game() -> Game:
    return Game(
        id="stats-uuid-1",
        league="NBA",
        status="SCHEDULED",
        home_team=TeamRef(id="h", name="Los Angeles Lakers"),
        away_team=TeamRef(id="a", name="Boston Celtics"),
        scheduled_start="2026-01-15T19:30:00Z",
    )


def snapshot(game_id: str, side: str, selection: str) -> LineSnapshot:
    return LineSnapshot(id="l1", game_id=game_id, side=side, selection=selection, market_type="MONEYLINE")


class TestGameReconciler:
    async def test_matches_home_team_name(self) -> None:
        lines = FakeLines([snapshot("odds-42", "HOME", "Los Angeles Lakers")])
        reconciler = GameReconciler(lines, FakeRedis())  # type: ignore[arg-type]
        assert await reconciler.resolve(make_game()) == "odds-42"

    async def test_matches_away_side_too(self) -> None:
        lines = FakeLines([snapshot("odds-42", "AWAY", "Boston Celtics")])
        reconciler = GameReconciler(lines, FakeRedis())  # type: ignore[arg-type]
        assert await reconciler.resolve(make_game()) == "odds-42"

    async def test_no_match_returns_none(self) -> None:
        lines = FakeLines([snapshot("odds-99", "HOME", "Denver Nuggets")])
        reconciler = GameReconciler(lines, FakeRedis())  # type: ignore[arg-type]
        assert await reconciler.resolve(make_game()) is None

    async def test_result_is_cached_under_emu_prefix(self) -> None:
        redis = FakeRedis()
        lines = FakeLines([snapshot("odds-42", "HOME", "Los Angeles Lakers")])
        reconciler = GameReconciler(lines, redis)  # type: ignore[arg-type]
        await reconciler.resolve(make_game())
        await reconciler.resolve(make_game())
        assert len(lines.calls) == 1
        assert redis.store["emu:gamemap:stats-uuid-1"] == "odds-42"

    async def test_case_insensitive(self) -> None:
        lines = FakeLines([snapshot("odds-42", "HOME", "LOS ANGELES LAKERS -3.5")])
        reconciler = GameReconciler(lines, FakeRedis())  # type: ignore[arg-type]
        assert await reconciler.resolve(make_game()) == "odds-42"

    async def test_lines_failure_returns_none(self) -> None:
        class FailingLines:
            async def current_lines(self, **kwargs: Any) -> list[LineSnapshot]:
                raise RuntimeError("boom")

        reconciler = GameReconciler(FailingLines(), FakeRedis())  # type: ignore[arg-type]
        assert await reconciler.resolve(make_game()) is None
