"""Bet placement: validation, odds capture, bankroll check, idempotent insert."""

import logging
import uuid
from datetime import datetime
from typing import Any

from bookie_emulator.api.errors import NotFoundError, UnprocessableError
from bookie_emulator.api.schemas import PlaceBetRequest, edge_percent_to_fraction
from bookie_emulator.clients.lines import BestLine, LinesClient, LineSnapshot
from bookie_emulator.clients.reconcile import GameReconciler
from bookie_emulator.clients.statistics import Game, StatisticsClient
from bookie_emulator.config import Settings
from bookie_emulator.db.repository import PaperBetRecord, PaperBetRepository, utc_now

logger = logging.getLogger(__name__)


def _parse_start(scheduled_start: str) -> datetime | None:
    try:
        return datetime.fromisoformat(scheduled_start.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class BetService:
    def __init__(
        self,
        statistics: StatisticsClient,
        lines: LinesClient,
        reconciler: GameReconciler,
        repo: PaperBetRepository,
        settings: Settings,
    ) -> None:
        self._statistics = statistics
        self._lines = lines
        self._reconciler = reconciler
        self._repo = repo
        self._settings = settings

    async def place_bet(self, request: PlaceBetRequest, idempotency_key: str) -> tuple[PaperBetRecord, bool]:
        """Place a paper bet. Returns (record, created); created=False on replay."""
        if request.stake <= 0:
            raise UnprocessableError("Stake must be positive")

        game = await self._fetch_game(request.game_id)
        start_at = _parse_start(game.scheduled_start)
        if game.status != "SCHEDULED" or (start_at is not None and start_at <= utc_now()):
            raise UnprocessableError(f"Game {request.game_id} has already started or is not open for betting")

        external_id = request.game_external_id or await self._reconciler.resolve(game)
        if external_id is None:
            raise UnprocessableError(
                f"No market data exists for game {request.game_id}: it could not be matched to a lines-service game"
            )

        odds = await self._capture_odds(request, external_id)
        await self._check_bankroll(request.stake)

        kelly = request.kelly_fraction if request.kelly_fraction is not None else self._settings.kelly_fraction
        values: dict[str, Any] = {
            "game_id": request.game_id,
            "game_external_id": external_id,
            "league": game.league,
            "market_type": request.market_type,
            "selection": request.selection,
            "side": request.side,
            "line_value": odds["line_value"],
            "sportsbook_id": odds["sportsbook_id"],
            "sportsbook_key": odds["sportsbook_key"],
            "odds_american": odds["odds_american"],
            "odds_decimal": odds["odds_decimal"],
            "stake": request.stake,
            "predicted_probability": request.predicted_probability,
            "edge_at_placement": edge_percent_to_fraction(request.edge_percentage),
            "kelly_fraction": kelly,
            "reasoning": request.reasoning,
            "prediction_id": request.prediction_id,
            "edge_id": request.edge_id,
            "idempotency_key": idempotency_key,
            "game_start_at": start_at,
        }
        return await self._repo.insert_idempotent(values)

    async def _fetch_game(self, game_id: uuid.UUID) -> Game:
        try:
            return await self._statistics.get_game(str(game_id))
        except NotFoundError as exc:
            raise UnprocessableError(f"Game {game_id} not found in statistics-service") from exc

    async def _capture_odds(self, request: PlaceBetRequest, external_id: str) -> dict[str, Any]:
        """Capture current odds from lines-service; DependencyError propagates as 502."""
        try:
            if request.sportsbook_key:
                return self._pick_pinned_line(request, await self._lines.game_lines(external_id))
            return self._pick_best_line(request, await self._lines.best_lines(external_id, request.market_type))
        except NotFoundError as exc:
            raise UnprocessableError(f"No market data exists for game {request.game_id} in lines-service") from exc

    def _pick_pinned_line(self, request: PlaceBetRequest, snapshots: list[LineSnapshot]) -> dict[str, Any]:
        for snapshot in snapshots:
            if (
                snapshot.sportsbook_key == request.sportsbook_key
                and snapshot.market_type == request.market_type
                and snapshot.side == request.side
            ):
                return {
                    "line_value": snapshot.line_value,
                    "odds_american": snapshot.odds_american,
                    "odds_decimal": snapshot.odds_decimal,
                    "sportsbook_id": _parse_uuid(snapshot.sportsbook_id),
                    "sportsbook_key": snapshot.sportsbook_key,
                }
        raise UnprocessableError(
            f"No {request.market_type} {request.side} line found at {request.sportsbook_key} for game {request.game_id}"
        )

    def _pick_best_line(self, request: PlaceBetRequest, best_lines: list[BestLine]) -> dict[str, Any]:
        for line in best_lines:
            if line.market_type == request.market_type and (
                line.side == request.side or line.selection == request.selection
            ):
                return {
                    "line_value": line.line_value,
                    "odds_american": line.best_odds_american,
                    "odds_decimal": line.best_odds_decimal,
                    "sportsbook_id": _parse_uuid(line.sportsbook_id),
                    "sportsbook_key": line.sportsbook_key,
                }
        raise UnprocessableError(f"No {request.market_type} {request.side} line found for game {request.game_id}")

    async def _check_bankroll(self, stake: float) -> None:
        bankroll = self._settings.starting_bankroll_units + await self._repo.total_profit()
        available = bankroll - await self._repo.open_exposure()
        if stake > available:
            raise UnprocessableError(
                f"Stake {stake} exceeds available bankroll",
                details={"available_units": round(available, 4), "bankroll_units": round(bankroll, 4)},
            )
