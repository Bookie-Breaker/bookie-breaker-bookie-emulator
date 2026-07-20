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
from bookie_emulator.api.schemas import PROP_MARKETS
from bookie_emulator.clients.lines import LinesClient, LineSnapshot
from bookie_emulator.clients.statistics import BoxScore, StatisticsClient
from bookie_emulator.config import Settings
from bookie_emulator.core.clv import compute_clv, match_closing_line
from bookie_emulator.core.grading import GradeStatus, grade_bet, profit_loss
from bookie_emulator.core.parlay import leg_outcome_summary, settle_parlay
from bookie_emulator.core.prop_grading import resolve_player_prop
from bookie_emulator.core.settlement import is_three_way_moneyline_league, settlement_scores
from bookie_emulator.db.repository import BetGradeRecord, PaperBetRecord, PaperBetRepository, ParlayLegRecord
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
        """Grade all open bets and parlay legs on a game from final scores.

        Returns single bets graded plus parlay parents settled (legs whose
        parent still has other games open do not count until it settles).
        Prop bets and PLAYER_PROP parlay legs grade from the game's box
        score (fetched once per game, shared across both); while it is
        unavailable they stay OPEN and the fallback poller retries on later
        sweeps.
        """
        bets = await self._repo.open_bets_for_game(uuid.UUID(game_id))
        legs = await self._repo.open_parlay_legs_for_game(uuid.UUID(game_id))
        if not bets and not legs:
            return 0
        score_bets = [bet for bet in bets if bet.market_type not in PROP_MARKETS]
        prop_bets = [bet for bet in bets if bet.market_type in PROP_MARKETS]
        needs_box = any(bet.market_type == "PLAYER_PROP" for bet in prop_bets) or any(
            leg.market_type == "PLAYER_PROP" for leg in legs
        )
        box = await self._fetch_box_score(game_id) if needs_box else None
        closing_by_game: dict[str, list[LineSnapshot]] = {}
        graded = 0
        for bet in score_bets:
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
        graded += await self._grade_props(
            prop_bets,
            box,
            home_score=home_score,
            away_score=away_score,
            total=total,
            margin=margin,
            game_result_id=game_result_id,
        )
        settled_parents = await self._grade_parlay_legs(
            legs,
            box,
            home_score=home_score,
            away_score=away_score,
            home_team=home_team,
            away_team=away_team,
            regulation_home_score=regulation_home_score,
            regulation_away_score=regulation_away_score,
        )
        logger.info(
            "graded %d/%d open bets, %d parlay legs (%d parents settled) for game %s",
            graded,
            len(bets),
            len(legs),
            settled_parents,
            game_id,
        )
        return graded + settled_parents

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
        if bet.is_parlay:
            # per-leg manual grading is not supported in v1: legs settle via
            # their games (event or poller); this endpoint (re-)settles the
            # parent once every leg is decided
            open_legs = [leg for leg in await self._repo.legs_for_bet(bet_id) if leg.leg_status == "OPEN"]
            if open_legs:
                hint = (
                    " (player-prop legs settle from box scores, which may lag the final score)"
                    if any(leg.market_type == "PLAYER_PROP" for leg in open_legs)
                    else ""
                )
                raise UnprocessableError(
                    f"Parlay {bet_id} still has {len(open_legs)} open legs; "
                    f"legs settle from their games and cannot be graded manually{hint}"
                )
            await self._settle_parlay_if_decided(bet_id, force=force)
            refreshed = await self._repo.get_with_grade(bet_id)
            if refreshed is None:  # pragma: no cover - the bet cannot vanish mid-request
                raise NotFoundError(f"Bet {bet_id} not found")
            return refreshed
        if bet.game_id is None:
            raise UnprocessableError(f"Bet {bet_id} has no game reference and cannot be graded from a game result")

        try:
            game = await self._statistics.get_game(str(bet.game_id))
        except NotFoundError as exc:
            raise UnprocessableError(f"Game {bet.game_id} not found in statistics-service") from exc
        if game.status != "FINAL" or game.result is None:
            raise UnprocessableError(f"Game {bet.game_id} has not completed yet")

        if bet.market_type in PROP_MARKETS:
            # same resolution flow as the event/poller path, but failures
            # surface as 422s instead of leaving the bet silently OPEN
            if bet.market_type != "PLAYER_PROP":
                raise UnprocessableError(f"{bet.market_type} grading is not implemented in v1 (Wave 3 is PLAYER_PROP)")
            try:
                box = await self._statistics.get_box_score(str(bet.game_id))
            except NotFoundError as exc:
                raise UnprocessableError(f"Box score for game {bet.game_id} is not available yet") from exc
            resolved = resolve_player_prop(
                box, bet.player_external_id, bet.stat_type, bet.prop_type, bet.side, bet.line_value
            )
            if isinstance(resolved, str):
                raise UnprocessableError(f"Cannot grade prop bet {bet_id}: {resolved}")
            await self._apply_prop_grade(
                bet,
                resolved,
                home_score=game.result.home_score,
                away_score=game.result.away_score,
                total=game.result.total_score,
                margin=game.result.margin,
                game_result_id=game.result.id,
                force=force,
            )
        else:
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

    async def _grade_props(
        self,
        prop_bets: list[PaperBetRecord],
        box: BoxScore | None,
        home_score: int,
        away_score: int,
        total: int | None,
        margin: int | None,
        game_result_id: str | None,
    ) -> int:
        """Grade PLAYER_PROP bets for one game from its box score (fetched
        once by grade_game and shared with parlay-leg grading).

        TEAM_PROP/GAME_PROP are skipped with a log (v1 implements PLAYER_PROP
        only). A missing box score (404 or upstream failure) leaves every
        player prop OPEN: the fallback poller retries on later sweeps.
        Unmatched players also stay OPEN (safer than VOID -- box scores may
        lag or names may mismatch; grade_manual with force can settle later).
        """
        player_props = [bet for bet in prop_bets if bet.market_type == "PLAYER_PROP"]
        for bet in prop_bets:
            if bet.market_type != "PLAYER_PROP":
                logger.info("skipping bet %s: %s grading is not implemented in v1", bet.id, bet.market_type)
        if not player_props or box is None:
            return 0
        graded = 0
        for bet in player_props:
            resolved = resolve_player_prop(
                box, bet.player_external_id, bet.stat_type, bet.prop_type, bet.side, bet.line_value
            )
            if isinstance(resolved, str):
                logger.warning("player prop bet %s stays OPEN: %s", bet.id, resolved)
                continue
            applied = await self._apply_prop_grade(
                bet,
                resolved,
                home_score=home_score,
                away_score=away_score,
                total=total,
                margin=margin,
                game_result_id=game_result_id,
                force=False,
            )
            graded += 1 if applied else 0
        return graded

    async def _apply_prop_grade(
        self,
        bet: PaperBetRecord,
        resolved: tuple[GradeStatus, str, float],
        home_score: int,
        away_score: int,
        total: int | None,
        margin: int | None,
        game_result_id: str | None,
        force: bool,
    ) -> bool:
        """Persist a resolved prop grade via the shared apply_grade transaction.

        Score fields carry the game's FINAL scores (box-score stats include
        any extra time, so regulation-score settlement does not apply). CLV
        is None for props in v1: closing prop lines exist upstream, but
        matching player+stat+line is deferred.
        """
        status, description, actual = resolved
        grade_values: dict[str, Any] = {
            "actual_result": description,
            "actual_home_score": home_score,
            "actual_away_score": away_score,
            "actual_margin": _or_default(margin, home_score - away_score),
            "actual_total": _or_default(total, home_score + away_score),
            "game_result_id": uuid.UUID(game_result_id) if game_result_id else None,
            "profit_loss": profit_loss(status, bet.stake, bet.odds_decimal),
            "closing_line_value": None,
            "closing_odds": None,
            "clv": None,
            "actual_stat_value": actual,
            "stat_type": bet.stat_type,
        }
        applied = await self._repo.apply_grade(
            bet.id, status, grade_values, self._settings.starting_bankroll_units, force=force
        )
        if applied and self._redis is not None:
            await publish_bet_graded(self._redis, bet, status, grade_values)
        return applied

    async def _fetch_box_score(self, game_id: str) -> BoxScore | None:
        try:
            return await self._statistics.get_box_score(game_id)
        except NotFoundError:
            logger.info("box score for game %s not available yet; player props stay OPEN for retry", game_id)
            return None
        except Exception:  # noqa: BLE001 - props stay OPEN; the poller retries
            logger.warning("box score fetch failed for game %s; player props stay OPEN", game_id, exc_info=True)
            return None

    async def _grade_parlay_legs(
        self,
        legs: list[ParlayLegRecord],
        box: BoxScore | None,
        home_score: int,
        away_score: int,
        home_team: str | None,
        away_team: str | None,
        regulation_home_score: int | None = None,
        regulation_away_score: int | None = None,
    ) -> int:
        """Grade OPEN parlay legs for one game, then settle any parent left
        with no OPEN legs. Returns the number of parents settled.

        Score-market legs grade via the same market paths as single bets
        (grade_bet's ValueError guard is defensive against rows written
        outside the API). PLAYER_PROP legs (Wave 4) grade from the game's
        box score -- fetched once by grade_game and shared with single-bet
        prop grading -- via the same resolve_player_prop core. A missing box
        score or unmatched player leaves the leg OPEN: the parent keeps
        waiting, and the fallback poller retries the game on later sweeps
        (its sweep unions OPEN parlay-leg games). TEAM_PROP/GAME_PROP legs
        cannot be placed and are skipped with a log."""
        parents: dict[uuid.UUID, None] = {}  # insertion-ordered set
        for leg in legs:
            if leg.market_type in PROP_MARKETS:
                if leg.market_type != "PLAYER_PROP":
                    logger.info(
                        "skipping parlay leg %s (bet %s): %s grading is not implemented in v1",
                        leg.id,
                        leg.bet_id,
                        leg.market_type,
                    )
                    continue
                if box is None:
                    continue  # stays OPEN; the poller retries once the box score lands
                resolved = resolve_player_prop(
                    box, leg.player_external_id, leg.stat_type, leg.prop_type, leg.side, leg.line_value
                )
                if isinstance(resolved, str):
                    logger.warning("player prop parlay leg %s stays OPEN: %s", leg.id, resolved)
                    continue
                status = resolved[0]
            else:
                settle_home, settle_away = settlement_scores(
                    {
                        "home_score": home_score,
                        "away_score": away_score,
                        "regulation_home_score": regulation_home_score,
                        "regulation_away_score": regulation_away_score,
                    },
                    leg.league,
                )
                try:
                    status, _ = grade_bet(
                        leg.market_type,
                        leg.side,
                        leg.line_value,
                        settle_home,
                        settle_away,
                        home_team,
                        away_team,
                        three_way_moneyline=is_three_way_moneyline_league(leg.league)
                        and leg.market_type == "MONEYLINE",
                    )
                except ValueError:
                    logger.warning("cannot grade parlay leg %s (bet %s)", leg.id, leg.bet_id, exc_info=True)
                    continue
            if await self._repo.grade_leg(leg.id, status):
                parents[leg.bet_id] = None
        settled = 0
        for parent_id in parents:
            settled += 1 if await self._settle_parlay_if_decided(parent_id) else 0
        return settled

    async def _settle_parlay_if_decided(self, bet_id: uuid.UUID, force: bool = False) -> bool:
        """Settle a parlay parent once no legs remain OPEN (ADR-028).

        WON re-prices over the surviving legs (PUSH/VOID legs contribute 1.0);
        all legs pushed/voided settles the parent as PUSH (stake refund). The
        grade row carries no scores (the parent spans games) and no
        closing/CLV: CLV for parlays is deferred in v1.
        """
        if await self._repo.open_leg_count(bet_id) > 0:
            return False
        found = await self._repo.get_with_grade(bet_id)
        if found is None:
            return False
        parent, _ = found
        legs = await self._repo.legs_for_bet(bet_id)
        settled = settle_parlay([leg.leg_status for leg in legs], [leg.odds_decimal for leg in legs])
        if settled is None:  # pragma: no cover - a leg reopened between the two reads
            return False
        status, repriced = settled
        grade_values: dict[str, Any] = {
            "actual_result": leg_outcome_summary([leg.leg_status for leg in legs], status, repriced),
            "actual_home_score": None,
            "actual_away_score": None,
            "actual_margin": None,
            "actual_total": None,
            "game_result_id": None,
            "profit_loss": profit_loss(status, parent.stake, repriced),
            "closing_line_value": None,
            "closing_odds": None,
            "clv": None,
        }
        applied = await self._repo.apply_grade(
            parent.id, status, grade_values, self._settings.starting_bankroll_units, force=force
        )
        if applied and self._redis is not None:
            await publish_bet_graded(self._redis, parent, status, grade_values)
        return applied

    async def _fetch_closing(self, game_external_id: str) -> list[LineSnapshot]:
        try:
            return await self._lines.closing_lines(game_external_id)
        except Exception:  # noqa: BLE001 - CLV is best-effort; grade without it
            logger.warning("closing lines unavailable for %s; grading without CLV", game_external_id, exc_info=True)
            return []
