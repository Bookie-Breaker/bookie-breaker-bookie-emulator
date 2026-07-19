"""Bet and parlay placement: validation, odds capture, bankroll check, idempotent insert."""

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from bookie_emulator.api.errors import NotFoundError, UnprocessableError
from bookie_emulator.api.schemas import (
    ParlayLegRequest,
    PlaceBetRequest,
    PlaceParlayRequest,
    edge_percent_to_fraction,
)
from bookie_emulator.clients.lines import BestLine, LinesClient, LineSnapshot
from bookie_emulator.clients.reconcile import GameReconciler
from bookie_emulator.clients.statistics import Game, StatisticsClient
from bookie_emulator.config import Settings
from bookie_emulator.core.odds import decimal_to_american
from bookie_emulator.core.parlay import combined_decimal
from bookie_emulator.db.repository import PaperBetRecord, PaperBetRepository, ParlayLegRecord, utc_now

logger = logging.getLogger(__name__)

# v1 parlay legs are team markets only (ADR-028); props are Wave 2+
PARLAY_MARKETS: frozenset[str] = frozenset({"SPREAD", "TOTAL", "MONEYLINE"})

_PARLAY_SELECTION_MAX = 500


class OddsSelection(Protocol):
    """The slice of a bet or parlay-leg request the odds-capture helpers need."""

    @property
    def game_id(self) -> uuid.UUID: ...
    @property
    def market_type(self) -> str: ...
    @property
    def selection(self) -> str: ...
    @property
    def side(self) -> str: ...
    @property
    def sportsbook_key(self) -> str | None: ...


def validate_parlay_legs(legs: Sequence[ParlayLegRequest]) -> None:
    """Business-rule validation for parlay legs (pinned contract: 422s).

    v1 accepts team markets only. No two legs may share a (game, market):
    the same (game, market, side) twice is a duplicate, and differing sides
    on one market are mutually exclusive (which also covers HOME/DRAW/AWAY
    combinations on three-way moneylines).
    """
    seen: set[tuple[uuid.UUID, str]] = set()
    for index, leg in enumerate(legs):
        if leg.market_type not in PARLAY_MARKETS:
            raise UnprocessableError(
                f"Parlay leg {index} has market {leg.market_type}: "
                "only SPREAD, TOTAL, and MONEYLINE legs are supported in v1"
            )
        key = (leg.game_id, leg.market_type)
        if key in seen:
            raise UnprocessableError(
                f"Parlay leg {index} repeats game {leg.game_id} market {leg.market_type}: "
                "duplicate or opposite-side legs on one market are not allowed"
            )
        seen.add(key)


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
            "player_external_id": request.player_external_id,
            "stat_type": request.stat_type,
            "prop_type": request.prop_type,
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

    async def place_parlay(
        self, request: PlaceParlayRequest, idempotency_key: str
    ) -> tuple[PaperBetRecord, list[ParlayLegRecord], bool]:
        """Place a parlay: parent paper_bets row + parlay_legs rows in one
        transaction. Returns (parent, legs, created); created=False on replay.

        Parent-row conventions (ADR-028; is_parlay=TRUE is the discriminator):

        - game_id/side/line_value are NULL; game_start_at is the EARLIEST leg
          start so exposure windows stay meaningful
        - game_external_id is the deterministic label
          "parlay:{first_leg_ext}+{n-1}" (the enum-free column is NOT NULL)
        - market_type and league mirror the FIRST leg: market_type_enum has no
          PARLAY value, so analytics must group parlays via is_parlay
          (bet_class), never market_type
        - selection is a human summary ("2-leg parlay: ..."), capped at 500
        - sportsbook_key is the single book when all legs share one, else
          "mixed" (sportsbook_id only survives when uniform)
        - odds are the combined price: decimal = product of leg decimals
        """
        if request.stake <= 0:
            raise UnprocessableError("Stake must be positive")
        validate_parlay_legs(request.legs)

        leg_values: list[dict[str, Any]] = []
        starts: list[datetime] = []
        book_keys: list[str] = []
        book_ids: list[uuid.UUID | None] = []
        for index, leg in enumerate(request.legs):
            game = await self._fetch_game(leg.game_id)
            start_at = _parse_start(game.scheduled_start)
            if game.status != "SCHEDULED" or (start_at is not None and start_at <= utc_now()):
                raise UnprocessableError(
                    f"Game {leg.game_id} (leg {index}) has already started or is not open for betting"
                )
            external_id = leg.game_external_id or await self._reconciler.resolve(game)
            if external_id is None:
                raise UnprocessableError(
                    f"No market data exists for game {leg.game_id} (leg {index}): "
                    "it could not be matched to a lines-service game"
                )
            odds = await self._capture_odds(leg, external_id)
            if start_at is not None:
                starts.append(start_at)
            book_keys.append(odds["sportsbook_key"])
            book_ids.append(odds["sportsbook_id"])
            leg_values.append(
                {
                    "leg_index": index,
                    "game_id": leg.game_id,
                    "game_external_id": external_id,
                    "league": game.league,
                    "market_type": leg.market_type,
                    "selection": leg.selection,
                    "side": leg.side,
                    "line_value": odds["line_value"],
                    "odds_american": odds["odds_american"],
                    "odds_decimal": odds["odds_decimal"],
                }
            )

        await self._check_bankroll(request.stake)

        combined = round(combined_decimal([values["odds_decimal"] for values in leg_values]), 4)
        uniform_book = len(set(book_keys)) == 1
        selection = f"{len(leg_values)}-leg parlay: " + " + ".join(leg.selection for leg in request.legs)
        if len(selection) > _PARLAY_SELECTION_MAX:
            selection = selection[: _PARLAY_SELECTION_MAX - 1] + "…"
        kelly = request.kelly_fraction if request.kelly_fraction is not None else self._settings.kelly_fraction
        parent_values: dict[str, Any] = {
            "game_id": None,
            "game_external_id": f"parlay:{leg_values[0]['game_external_id']}+{len(leg_values) - 1}",
            "league": leg_values[0]["league"],
            "market_type": leg_values[0]["market_type"],
            "selection": selection,
            "side": None,
            "line_value": None,
            "sportsbook_id": book_ids[0] if uniform_book else None,
            "sportsbook_key": book_keys[0] if uniform_book else "mixed",
            "odds_american": decimal_to_american(combined),
            "odds_decimal": combined,
            "stake": request.stake,
            "predicted_probability": request.predicted_probability,
            "edge_at_placement": edge_percent_to_fraction(request.edge_percentage),
            "kelly_fraction": kelly,
            "reasoning": request.reasoning,
            "prediction_id": None,
            "edge_id": None,
            "idempotency_key": idempotency_key,
            "game_start_at": min(starts) if starts else None,
            "is_parlay": True,
        }
        return await self._repo.insert_parlay(parent_values, leg_values)

    async def _capture_odds(self, request: OddsSelection, external_id: str) -> dict[str, Any]:
        """Capture current odds from lines-service; DependencyError propagates as 502."""
        try:
            if request.sportsbook_key:
                return self._pick_pinned_line(request, await self._lines.game_lines(external_id))
            return self._pick_best_line(request, await self._lines.best_lines(external_id, request.market_type))
        except NotFoundError as exc:
            raise UnprocessableError(f"No market data exists for game {request.game_id} in lines-service") from exc

    def _pick_pinned_line(self, request: OddsSelection, snapshots: list[LineSnapshot]) -> dict[str, Any]:
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

    def _pick_best_line(self, request: OddsSelection, best_lines: list[BestLine]) -> dict[str, Any]:
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
