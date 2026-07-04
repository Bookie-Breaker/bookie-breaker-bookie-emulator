"""Fallback grading poller for missed game.completed events (redis pub/sub is
fire-and-forget, so delivery is not guaranteed).

Every grading_poll_seconds it looks for open bets whose game started more
than grading_grace_seconds ago, asks statistics-service whether those games
are final, and grades them. Everything is exception-wrapped: a bad cycle
never kills the task.
"""

import asyncio
import logging
from datetime import timedelta

from bookie_emulator.clients.statistics import StatisticsClient
from bookie_emulator.db.repository import PaperBetRepository, utc_now
from bookie_emulator.services.grader import GraderService

logger = logging.getLogger(__name__)


class GradingPoller:
    def __init__(
        self,
        statistics: StatisticsClient,
        repo: PaperBetRepository,
        grader: GraderService,
        poll_seconds: int,
        grace_seconds: int,
    ) -> None:
        self._statistics = statistics
        self._repo = repo
        self._grader = grader
        self._poll_seconds = poll_seconds
        self._grace_seconds = grace_seconds

    async def run_once(self) -> int:
        """One polling cycle. Returns the number of bets graded."""
        cutoff = utc_now() - timedelta(seconds=self._grace_seconds)
        game_ids = await self._repo.open_game_ids_started_before(cutoff)
        graded = 0
        for game_id in game_ids:
            try:
                game = await self._statistics.get_game(str(game_id))
                if game.status != "FINAL" or game.result is None:
                    continue
                graded += await self._grader.grade_game(
                    str(game_id),
                    home_score=game.result.home_score,
                    away_score=game.result.away_score,
                    total=game.result.total_score,
                    margin=game.result.margin,
                    home_team=game.home_team.abbreviation or game.home_team.name,
                    away_team=game.away_team.abbreviation or game.away_team.name,
                    game_result_id=game.result.id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad game must not stop the sweep
                logger.warning("grading poll failed for game %s", game_id, exc_info=True)
        return graded

    async def run(self) -> None:
        """Poll forever; started as a lifespan task and cancelled on shutdown."""
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                graded = await self.run_once()
                if graded:
                    logger.info("grading poller graded %d bets", graded)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the poller must survive any cycle failure
                logger.warning("grading poll cycle failed", exc_info=True)
