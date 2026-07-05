"""Publish bet.graded events per redis-schemas.md.

Fire-and-forget: publish failures are logged and never fail grading.
Payloads carry identifiers + metadata only; consumers refetch via REST.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from bookie_emulator.api.schemas import DB_TO_API_RESULT
from bookie_emulator.db.repository import PaperBetRecord

logger = logging.getLogger(__name__)

BET_GRADED_CHANNEL = "events:bet.graded"


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def bet_graded_payload(bet: PaperBetRecord, status: str, grade_values: dict[str, Any]) -> dict[str, object]:
    return {
        "event": "bet.graded",
        "timestamp": _utc_now_iso(),
        "bet_id": str(bet.id),
        "game_id": str(bet.game_id),
        "league": bet.league,
        "market_type": bet.market_type,
        "selection": bet.selection,
        "sportsbook": bet.sportsbook_key,
        # events speak the API vocabulary (WIN/LOSS/...), not the DB's WON/LOST,
        # so consumers reuse the mapping they already have for REST responses
        "result": DB_TO_API_RESULT.get(status, status),
        "stake": bet.stake,
        "profit_loss": grade_values["profit_loss"],
        "clv": grade_values["clv"],
    }


async def publish_bet_graded(
    redis_client: "aioredis.Redis", bet: PaperBetRecord, status: str, grade_values: dict[str, Any]
) -> None:
    payload = bet_graded_payload(bet, status, grade_values)
    try:
        await redis_client.publish(BET_GRADED_CHANNEL, json.dumps(payload))
    except Exception:  # noqa: BLE001 - pub/sub is best-effort by design
        logger.warning("failed to publish %s for bet %s", BET_GRADED_CHANNEL, bet.id, exc_info=True)
