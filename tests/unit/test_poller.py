"""Fallback grading poller: sweep composition, FINAL gating, per-game error
isolation, and the forever-loop's survival semantics."""

import asyncio
import uuid
from datetime import datetime
from typing import Any

import pytest

from bookie_emulator.clients.statistics import Game
from bookie_emulator.services.poller import GradingPoller

FINAL_GAME = uuid.uuid4()
PENDING_GAME = uuid.uuid4()
LEG_GAME = uuid.uuid4()


def make_game(game_id: uuid.UUID, status: str = "FINAL", with_result: bool = True) -> Game:
    payload: dict[str, Any] = {
        "id": str(game_id),
        "league": "FIFA_WC",
        "status": status,
        "home_team": {"id": "t-home", "name": "Manchester City", "abbreviation": "MCI"},
        "away_team": {"id": "t-away", "name": "Paris SG", "abbreviation": ""},
        "scheduled_start": "2026-07-10T19:00:00Z",
        "season": 2026,
    }
    if with_result:
        payload["result"] = {
            "id": str(uuid.uuid4()),
            "home_score": 2,
            "away_score": 1,
            "total_score": 3,
            "margin": 1,
            "overtime": True,
            "regulation_home_score": 1,
            "regulation_away_score": 1,
            "completed_at": "2026-07-10T21:00:00Z",
        }
    return Game.model_validate(payload)


class FakeRepo:
    def __init__(self, game_ids: list[uuid.UUID], leg_game_ids: list[uuid.UUID] | None = None) -> None:
        self.game_ids = game_ids
        self.leg_game_ids = leg_game_ids or []
        self.cutoffs: list[datetime] = []

    async def open_game_ids_started_before(self, cutoff: datetime) -> list[uuid.UUID]:
        self.cutoffs.append(cutoff)
        return self.game_ids

    async def open_parlay_leg_game_ids_started_before(self, cutoff: datetime) -> list[uuid.UUID]:
        return self.leg_game_ids


class FakeStatistics:
    def __init__(self, games: dict[uuid.UUID, Game], errors: dict[uuid.UUID, Exception] | None = None) -> None:
        self.games = games
        self.errors = errors or {}
        self.calls: list[str] = []

    async def get_game(self, game_id: str) -> Game:
        self.calls.append(game_id)
        key = uuid.UUID(game_id)
        if key in self.errors:
            raise self.errors[key]
        return self.games[key]


class FakeGrader:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def grade_game(self, game_id: str, **kwargs: Any) -> int:
        self.calls.append({"game_id": game_id, **kwargs})
        return 1


def make_poller(repo: FakeRepo, statistics: FakeStatistics, grader: FakeGrader, poll_seconds: int = 0) -> GradingPoller:
    return GradingPoller(
        statistics,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        grader,  # type: ignore[arg-type]
        poll_seconds=poll_seconds,
        grace_seconds=10_800,
    )


class TestRunOnce:
    async def test_grades_final_games_with_result_fields(self) -> None:
        repo = FakeRepo([FINAL_GAME])
        grader = FakeGrader()
        poller = make_poller(repo, FakeStatistics({FINAL_GAME: make_game(FINAL_GAME)}), grader)

        assert await poller.run_once() == 1
        call = grader.calls[0]
        assert call["game_id"] == str(FINAL_GAME)
        assert call["home_score"] == 2 and call["away_score"] == 1
        assert call["total"] == 3 and call["margin"] == 1
        assert call["home_team"] == "MCI"
        assert call["away_team"] == "Paris SG"  # blank abbreviation falls back to the name
        assert call["regulation_home_score"] == 1
        assert call["regulation_away_score"] == 1

    async def test_skips_games_not_final_or_without_result(self) -> None:
        games = {
            PENDING_GAME: make_game(PENDING_GAME, status="IN_PROGRESS"),
            FINAL_GAME: make_game(FINAL_GAME, with_result=False),
        }
        grader = FakeGrader()
        poller = make_poller(FakeRepo([PENDING_GAME, FINAL_GAME]), FakeStatistics(games), grader)

        assert await poller.run_once() == 0
        assert grader.calls == []

    async def test_unions_and_dedupes_parlay_leg_games(self) -> None:
        games = {FINAL_GAME: make_game(FINAL_GAME), LEG_GAME: make_game(LEG_GAME)}
        statistics = FakeStatistics(games)
        grader = FakeGrader()
        poller = make_poller(FakeRepo([FINAL_GAME], [FINAL_GAME, LEG_GAME]), statistics, grader)

        assert await poller.run_once() == 2
        assert statistics.calls == [str(FINAL_GAME), str(LEG_GAME)]  # FINAL_GAME fetched once

    async def test_one_bad_game_does_not_stop_the_sweep(self) -> None:
        statistics = FakeStatistics(
            {FINAL_GAME: make_game(FINAL_GAME)}, errors={PENDING_GAME: RuntimeError("stats down")}
        )
        grader = FakeGrader()
        poller = make_poller(FakeRepo([PENDING_GAME, FINAL_GAME]), statistics, grader)

        assert await poller.run_once() == 1
        assert grader.calls[0]["game_id"] == str(FINAL_GAME)

    async def test_cancellation_propagates(self) -> None:
        statistics = FakeStatistics({}, errors={FINAL_GAME: asyncio.CancelledError()})
        poller = make_poller(FakeRepo([FINAL_GAME]), statistics, FakeGrader())

        with pytest.raises(asyncio.CancelledError):
            await poller.run_once()


class TestRunLoop:
    async def test_loop_polls_until_cancelled(self) -> None:
        repo = FakeRepo([FINAL_GAME])
        grader = FakeGrader()
        poller = make_poller(repo, FakeStatistics({FINAL_GAME: make_game(FINAL_GAME)}), grader)

        task = asyncio.create_task(poller.run())
        while not grader.calls:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(grader.calls) >= 1

    async def test_cancellation_mid_cycle_stops_the_loop(self) -> None:
        class BlockingRepo(FakeRepo):
            def __init__(self) -> None:
                super().__init__([])
                self.entered = asyncio.Event()

            async def open_game_ids_started_before(self, cutoff: datetime) -> list[uuid.UUID]:
                self.entered.set()
                await asyncio.Event().wait()  # block until cancelled
                return []

        repo = BlockingRepo()
        poller = make_poller(repo, FakeStatistics({}), FakeGrader())

        task = asyncio.create_task(poller.run())
        await repo.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_loop_survives_a_failing_cycle(self) -> None:
        class ExplodingRepo(FakeRepo):
            def __init__(self) -> None:
                super().__init__([])
                self.attempts = 0

            async def open_game_ids_started_before(self, cutoff: datetime) -> list[uuid.UUID]:
                self.attempts += 1
                raise RuntimeError("db down")

        repo = ExplodingRepo()
        poller = make_poller(repo, FakeStatistics({}), FakeGrader())

        task = asyncio.create_task(poller.run())
        while repo.attempts < 2:  # a second attempt proves the loop survived
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
