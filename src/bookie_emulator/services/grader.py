"""Bet grading orchestration for the event, poller, and manual paths.

All paths converge on PaperBetRepository.apply_grade, which claims the bet
and writes the grade row plus a bankroll snapshot in one transaction. CLV
is best-effort: closing-line lookups that fail never block grading.
"""

import logging
import uuid
from typing import Any

import redis.asyncio as aioredis

from bookie_emulator.api.errors import DuplicateResourceError, NotFoundError, UnprocessableError
from bookie_emulator.clients.lines import LinesClient, LineSnapshot
from bookie_emulator.clients.statistics import StatisticsClient
from bookie_emulator.config import Settings
from bookie_emulator.core.clv import compute_clv, match_closing_line
from bookie_emulator.core.grading import grade_bet, profit_loss
from bookie_emulator.core.settlement import is_three_way_moneyline_league, settlement_scores
from bookie_emulator.db.repository import BetGradeRecord, PaperBetRecord, PaperBetRepository
from bookie_emulator.events.publisher import publish_bet_graded

logger = logging.getLogger(__name__)


def _or_default(value: int | None, default: int) -> int:
    return value if value is not None else default


class GraderService:
    def __init__(
        self,
        statistics: StatisticsClient,
        lines: LinesClient,
        repo: PaperBetRepository,
        settings: Settings,
        redis_client: "aioredis.Redis | None" = None,
    ) -> None:
        self._statistics = statistics
        self._lines = lines
        self._repo = repo
        self._settings = settings
        self._redis = redis_client

    async def grade_game(
        self,
        game_id: str,
        home_score: int,
        away_score: int,
        total: int | None = None,
        margin: int | None = None,
        home_team: str | None = None,
        away_team: str | None = None,
        game_result_id: str | None = None,
        regulation_home_score: int | None = None,
        regulation_away_score: int | None = None,
    ) -> int:
        """Grade all open bets on a game from final scores. Returns bets graded."""
        bets = await self._repo.open_bets_for_game(uuid.UUID(game_id))
        if not bets:
            return 0
        closing_by_game: dict[str, list[LineSnapshot]] = {}
        graded = 0
        for bet in bets:
            if bet.game_external_id not in closing_by_game:
                closing_by_game[bet.game_external_id] = await self._fetch_closing(bet.game_external_id)
            applied = await self._grade_one(
                bet,
                home_score=home_score,
                away_score=away_score,
                total=total,
                margin=margin,
                home_team=home_team,
                away_team=away_team,
                game_result_id=game_result_id,
                closing_lines=closing_by_game[bet.game_external_id],
                force=False,
                regulation_home_score=regulation_home_score,
                regulation_away_score=regulation_away_score,
            )
            graded += 1 if applied else 0
        logger.info("graded %d/%d open bets for game %s", graded, len(bets), game_id)
        return graded

    async def grade_manual(
        self, bet_id: uuid.UUID, force: bool = False
    ) -> tuple[PaperBetRecord, BetGradeRecord | None]:
        """Manually (re-)grade a bet from the statistics-service game result."""
        found = await self._repo.get_with_grade(bet_id)
        if found is None:
            raise NotFoundError(f"Bet {bet_id} not found")
        bet, _ = found
        if bet.status != "OPEN" and not force:
            raise DuplicateResourceError(f"Bet {bet_id} is already graded; pass force=true to re-grade")
        if bet.is_parlay or bet.game_id is None:
            raise UnprocessableError(f"Bet {bet_id} is a parlay parent; it settles from its legs, not a single game")

        try:
            game = await self._statistics.get_game(str(bet.game_id))
        except NotFoundError as exc:
            raise UnprocessableError(f"Game {bet.game_id} not found in statistics-service") from exc
        if game.status != "FINAL" or game.result is None:
            raise UnprocessableError(f"Game {bet.game_id} has not completed yet")

        await self._grade_one(
            bet,
            home_score=game.result.home_score,
            away_score=game.result.away_score,
            total=game.result.total_score,
            margin=game.result.margin,
            home_team=game.home_team.abbreviation or game.home_team.name,
            away_team=game.away_team.abbreviation or game.away_team.name,
            game_result_id=game.result.id,
            closing_lines=await self._fetch_closing(bet.game_external_id),
            force=force,
            regulation_home_score=game.result.regulation_home_score,
            regulation_away_score=game.result.regulation_away_score,
        )
        refreshed = await self._repo.get_with_grade(bet_id)
        if refreshed is None:  # pragma: no cover - the bet cannot vanish mid-request
            raise NotFoundError(f"Bet {bet_id} not found")
        return refreshed

    async def _grade_one(
        self,
        bet: PaperBetRecord,
        home_score: int,
        away_score: int,
        total: int | None,
        margin: int | None,
        home_team: str | None,
        away_team: str | None,
        game_result_id: str | None,
        closing_lines: list[LineSnapshot],
        force: bool,
        regulation_home_score: int | None = None,
        regulation_away_score: int | None = None,
    ) -> bool:
        settle_home, settle_away = settlement_scores(
            {
                "home_score": home_score,
                "away_score": away_score,
                "regulation_home_score": regulation_home_score,
                "regulation_away_score": regulation_away_score,
            },
            bet.league,
        )
        regulation_used = (settle_home, settle_away) != (home_score, away_score)
        status, description = grade_bet(
            bet.market_type,
            bet.side,
            bet.line_value,
            settle_home,
            settle_away,
            home_team,
            away_team,
            three_way_moneyline=is_three_way_moneyline_league(bet.league) and bet.market_type == "MONEYLINE",
        )
        if regulation_used:
            margin_value = settle_home - settle_away
            total_value = settle_home + settle_away
        else:
            margin_value = _or_default(margin, home_score - away_score)
            total_value = _or_default(total, home_score + away_score)
        grade_values: dict[str, Any] = {
            "actual_result": description,
            "actual_home_score": settle_home,
            "actual_away_score": settle_away,
            "actual_margin": margin_value,
            "actual_total": total_value,
            "game_result_id": uuid.UUID(game_result_id) if game_result_id else None,
            "profit_loss": profit_loss(status, bet.stake, bet.odds_decimal),
            "closing_line_value": None,
            "closing_odds": None,
            "clv": None,
        }
        closing = match_closing_line(closing_lines, bet.market_type, bet.side, bet.sportsbook_key, bet.line_value)
        if closing is not None and closing.odds_american != 0:
            grade_values["closing_line_value"] = closing.line_value
            grade_values["closing_odds"] = closing.odds_american
            grade_values["clv"] = compute_clv(bet.odds_american, closing.odds_american)
        applied = await self._repo.apply_grade(
            bet.id, status, grade_values, self._settings.starting_bankroll_units, force=force
        )
        # apply_grade's transaction has committed once it returns; a force
        # re-grade republishes, which consumers treat as latest-state.
        if applied and self._redis is not None:
            await publish_bet_graded(self._redis, bet, status, grade_values)
        return applied

    async def _fetch_closing(self, game_external_id: str) -> list[LineSnapshot]:
        try:
            return await self._lines.closing_lines(game_external_id)
        except Exception:  # noqa: BLE001 - CLV is best-effort; grade without it
            logger.warning("closing lines unavailable for %s; grading without CLV", game_external_id, exc_info=True)
            return []
