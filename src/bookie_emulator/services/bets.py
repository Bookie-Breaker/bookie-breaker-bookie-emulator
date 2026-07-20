"""Bet and parlay placement: validation, odds capture, bankroll check, idempotent insert."""

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar

from bookie_emulator.api.errors import NotFoundError, UnprocessableError
from bookie_emulator.api.schemas import (
    PROP_MARKETS,
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

# Parlay legs are team markets (ADR-028) plus PLAYER_PROP since Phase 7
# Wave 4; TEAM_PROP/GAME_PROP legs remain out of scope.
PARLAY_MARKETS: frozenset[str] = frozenset({"SPREAD", "TOTAL", "MONEYLINE", "PLAYER_PROP"})

# side vocabulary per prop_type (pinned contract: YES/NO <=> YES_NO,
# OVER/UNDER <=> OVER_UNDER)
_PROP_SIDES_BY_TYPE: dict[str, tuple[str, str]] = {
    "YES_NO": ("YES", "NO"),
    "OVER_UNDER": ("OVER", "UNDER"),
}

_PARLAY_SELECTION_MAX = 500

# statistics-service game_status_enum values a live (in-game) bet may be
# placed against: SCHEDULED (a live-flagged pregame placement is harmless)
# and IN_PROGRESS. FINAL is rejected as ended; POSTPONED/CANCELLED/SUSPENDED
# are rejected as not open for betting.
_LIVE_PLACEABLE_STATUSES: frozenset[str] = frozenset({"SCHEDULED", "IN_PROGRESS"})

_LineT = TypeVar("_LineT", LineSnapshot, BestLine)


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

    Legs may be team markets or PLAYER_PROP (Wave 4); TEAM_PROP/GAME_PROP
    are rejected. A PLAYER_PROP leg must carry stat_type + prop_type, and
    its side must match the prop_type vocabulary (YES/NO <=> YES_NO,
    OVER/UNDER <=> OVER_UNDER).

    No two legs may share a (game, market, player, stat) key: the same
    selection twice is a duplicate, and differing sides on one key are
    mutually exclusive (covering HOME/DRAW/AWAY combinations on three-way
    moneylines and OVER/UNDER or YES/NO on one player's stat). Team-market
    legs carry no player/stat, so a team leg and a prop leg on one game
    coexist, as do two different players' props on one game.

    Live parlays are OUT of scope in v1: legs carry no is_live flag (extra
    request fields are ignored by pydantic), and place_parlay keeps the
    pregame-only rule -- any leg whose game is not SCHEDULED, or has already
    started, is rejected with a 422 ("has already started or is not open for
    betting"). In-game legs therefore cannot enter a parlay by any path.
    """
    seen: set[tuple[uuid.UUID, str, str | None, str | None]] = set()
    for index, leg in enumerate(legs):
        if leg.market_type not in PARLAY_MARKETS:
            raise UnprocessableError(
                f"Parlay leg {index} has market {leg.market_type}: "
                "only SPREAD, TOTAL, MONEYLINE, and PLAYER_PROP legs are supported"
            )
        if leg.market_type == "PLAYER_PROP":
            if not leg.stat_type or not leg.prop_type:
                raise UnprocessableError(
                    f"Parlay leg {index} is a PLAYER_PROP and requires stat_type and prop_type (ADR-029)"
                )
            valid_sides = _PROP_SIDES_BY_TYPE[leg.prop_type]
            if leg.side not in valid_sides:
                raise UnprocessableError(
                    f"Parlay leg {index} side {leg.side} is not valid for a {leg.prop_type} prop: "
                    f"expected {valid_sides[0]} or {valid_sides[1]}"
                )
        key = (leg.game_id, leg.market_type, leg.player_external_id, leg.stat_type)
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
        if request.is_live:
            # live placements skip the not-started check: SCHEDULED and
            # IN_PROGRESS are both placeable, anything final/abandoned is not
            if game.status == "FINAL":
                raise UnprocessableError(f"Game {request.game_id} has ended and is not open for live betting")
            if game.status not in _LIVE_PLACEABLE_STATUSES:
                raise UnprocessableError(f"Game {request.game_id} is {game.status} and is not open for betting")
        elif game.status != "SCHEDULED" or (start_at is not None and start_at <= utc_now()):
            raise UnprocessableError(f"Game {request.game_id} has already started or is not open for betting")

        external_id = request.game_external_id or await self._reconciler.resolve(game)
        if external_id is None:
            raise UnprocessableError(
                f"No market data exists for game {request.game_id}: it could not be matched to a lines-service game"
            )

        odds = await self._capture_odds(request, external_id, prefer_live=request.is_live)
        await self._check_bankroll(request.stake)

        if request.is_live and start_at is None:
            # an unparseable scheduled_start falls back to placement time
            # (>= actual start for an in-progress game) so the fallback
            # grading poller, which gates on game_start_at, never skips the bet
            start_at = utc_now()

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
            "is_live": request.is_live,
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

        Live parlays are out of scope in v1: every leg's game must still be
        SCHEDULED and not started (422 otherwise), and odds capture never
        prefers live lines. See validate_parlay_legs.
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
                    "player_external_id": leg.player_external_id,
                    "stat_type": leg.stat_type,
                    "prop_type": leg.prop_type,
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

    async def _capture_odds(
        self, request: OddsSelection, external_id: str, prefer_live: bool = False
    ) -> dict[str, Any]:
        """Capture current odds from lines-service; DependencyError propagates as 502.

        With prefer_live=True (live single-bet placements), lines flagged
        is_live win over pregame lines; when no live line matches, the
        freshest matching line is used with a logged note.
        """
        try:
            if request.sportsbook_key:
                return self._pick_pinned_line(request, await self._lines.game_lines(external_id), prefer_live)
            return self._pick_best_line(
                request, await self._lines.best_lines(external_id, request.market_type), prefer_live
            )
        except NotFoundError as exc:
            raise UnprocessableError(f"No market data exists for game {request.game_id} in lines-service") from exc

    def _select_line(self, request: OddsSelection, matches: list[_LineT], prefer_live: bool) -> _LineT | None:
        """Pick one line among the matching candidates.

        Pregame placements keep the historical behavior (first match). Live
        placements prefer is_live lines (freshest first); with none available
        they fall back to the freshest match of any kind, with a logged note.
        """
        if not matches:
            return None
        if not prefer_live:
            return matches[0]
        live = [line for line in matches if line.is_live]
        if live:
            return max(live, key=lambda line: line.timestamp)
        logger.info(
            "no live %s %s line for game %s; falling back to freshest available line",
            request.market_type,
            request.side,
            request.game_id,
        )
        return max(matches, key=lambda line: line.timestamp)

    def _pick_pinned_line(
        self, request: OddsSelection, snapshots: list[LineSnapshot], prefer_live: bool = False
    ) -> dict[str, Any]:
        """Match a line at the pinned book by market + side.

        Prop markets additionally require an exact selection match: prop
        lines are per-player (the selection string carries the player), so a
        side-only match could capture another player's price.
        """
        matches = [
            snapshot
            for snapshot in snapshots
            if snapshot.sportsbook_key == request.sportsbook_key
            and snapshot.market_type == request.market_type
            and snapshot.side == request.side
            and (request.market_type not in PROP_MARKETS or snapshot.selection == request.selection)
        ]
        snapshot = self._select_line(request, matches, prefer_live)
        if snapshot is not None:
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

    def _pick_best_line(
        self, request: OddsSelection, best_lines: list[BestLine], prefer_live: bool = False
    ) -> dict[str, Any]:
        """Match a best-line entry by market + side-or-selection.

        Prop markets match on selection only: best-line entries for props
        are per-player, so the selection string (which carries the player)
        disambiguates where a side-only match (e.g. OVER) could capture
        another player's line. No player/stat narrowing is needed on
        OddsSelection because of this.
        """
        if request.market_type in PROP_MARKETS:
            matches = [
                line
                for line in best_lines
                if line.market_type == request.market_type and line.selection == request.selection
            ]
        else:
            matches = [
                line
                for line in best_lines
                if line.market_type == request.market_type
                and (line.side == request.side or line.selection == request.selection)
            ]
        line = self._select_line(request, matches, prefer_live)
        if line is not None:
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
