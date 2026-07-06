"""events:game.completed subscriber driving automated bet grading.

Runs as a lifespan-spawned asyncio task. The loop never crashes: every
payload error is logged and swallowed, and connection failures reconnect
with capped exponential backoff (1s -> 30s). The current lifecycle state
("running"/"reconnecting"/"stopped") is exposed for the health endpoint.
"""

import asyncio
import contextlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis

from bookie_emulator.services.grader import GraderService

logger = logging.getLogger(__name__)

CHANNEL = "events:game.completed"


class GameCompletedSubscriber:
    def __init__(self, redis_client: "aioredis.Redis", grader: GraderService) -> None:
        self._redis = redis_client
        self._grader = grader
        self._status = "stopped"

    @property
    def status(self) -> str:
        return self._status

    async def run(self) -> None:
        """Subscribe and dispatch forever; cancelled cleanly on shutdown."""
        backoff = 1.0
        while True:
            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(CHANNEL)
                self._status = "running"
                backoff = 1.0
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    await self._handle(message.get("data"))
            except asyncio.CancelledError:
                self._status = "stopped"
                raise
            except Exception:  # noqa: BLE001 - reconnect on any transport failure
                self._status = "reconnecting"
                logger.warning("subscriber lost %s; reconnecting in %.0fs", CHANNEL, backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def _handle(self, raw: Any) -> None:
        try:
            payload = json.loads(raw)
            game_id = str(payload["game_id"])
            graded = await self._grader.grade_game(
                game_id,
                home_score=int(payload["home_score"]),
                away_score=int(payload["away_score"]),
                total=payload.get("total"),
                margin=payload.get("margin"),
                home_team=payload.get("home_team"),
                away_team=payload.get("away_team"),
                # optional regulation-time scores (ADR-027): present only when
                # a soccer match went to extra time; absent means final = settlement
                regulation_home_score=payload.get("regulation_home_score"),
                regulation_away_score=payload.get("regulation_away_score"),
            )
            logger.info("game.completed for %s graded %d bets", game_id, graded)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad payload must never kill the subscriber
            logger.warning("failed to process %s payload: %r", CHANNEL, raw, exc_info=True)
