"""Reconcile statistics-service game UUIDs with lines-service external ids.

The two services have disjoint id spaces: statistics-service uses
deterministic UUIDv5 ids derived from NBA game ids, while lines-service
stores the Odds API event id. No shared key exists yet, but lines-service
selections carry full team names identical to statistics-service team
names, so games are matched by (league, date, home team name). Mappings
are cached in Redis for 24h under ``emu:gamemap:{stats_game_uuid}``.

A docs note recommends statistics-service eventually populate
external_ids.odds_api so this name-matching becomes unnecessary.
"""

import logging
from datetime import datetime

import redis.asyncio as aioredis

from bookie_emulator.clients.lines import LinesClient
from bookie_emulator.clients.statistics import Game

logger = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    return " ".join(name.casefold().split())


def _game_date(scheduled_start: str) -> str | None:
    try:
        return datetime.fromisoformat(scheduled_start.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


class GameReconciler:
    def __init__(self, lines: LinesClient, redis_client: "aioredis.Redis", ttl_seconds: int = 86_400) -> None:
        self._lines = lines
        self._redis = redis_client
        self._ttl = ttl_seconds

    async def resolve(self, game: Game) -> str | None:
        """Return the lines-service external game id, or None if unmatched."""
        cache_key = f"emu:gamemap:{game.id}"
        cached = await self._redis.get(cache_key)
        if cached:
            return str(cached)

        date = _game_date(game.scheduled_start)
        try:
            snapshots = await self._lines.current_lines(
                league=game.league, market_type="MONEYLINE", date=date, limit=200
            )
        except Exception:  # noqa: BLE001 - reconciliation is best-effort
            logger.warning("lines-service unavailable during game reconciliation for %s", game.id, exc_info=True)
            return None

        home_name = _normalize(game.home_team.name)
        away_name = _normalize(game.away_team.name)
        for snapshot in snapshots:
            selection = _normalize(snapshot.selection)
            if snapshot.side == "HOME" and selection.startswith(home_name):
                await self._redis.set(cache_key, snapshot.game_id, ex=self._ttl)
                return snapshot.game_id
            if snapshot.side == "AWAY" and selection.startswith(away_name):
                await self._redis.set(cache_key, snapshot.game_id, ex=self._ttl)
                return snapshot.game_id

        logger.info("no lines-service game matched stats game %s (%s @ %s)", game.id, away_name, home_name)
        return None
