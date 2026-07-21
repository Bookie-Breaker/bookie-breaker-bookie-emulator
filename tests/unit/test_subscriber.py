"""events:game.completed subscriber: payload dispatch, bad-payload
swallowing, and the reconnect/stop lifecycle exposed to the health endpoint."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bookie_emulator.events.subscriber import CHANNEL, GameCompletedSubscriber


class FakeGrader:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def grade_game(self, game_id: str, **kwargs: Any) -> int:
        if self.error is not None:
            raise self.error
        self.calls.append({"game_id": game_id, **kwargs})
        return 2


class FakePubSub:
    def __init__(self, messages: list[dict[str, Any]], error: Exception | None = None, block: bool = False) -> None:
        self.messages = messages
        self.error = error
        self.block = block
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        for message in self.messages:
            yield message
        if self.error is not None:
            raise self.error
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self, pubsub: FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self, ignore_subscribe_messages: bool = True) -> FakePubSub:
        return self._pubsub


def payload(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "game_id": "game-1",
        "home_score": 110,
        "away_score": "100",  # ints may arrive as strings; the handler coerces
        "total": 210,
        "margin": 10,
        "home_team": "LAL",
        "away_team": "BOS",
        "regulation_home_score": None,
        "regulation_away_score": None,
    }
    body.update(overrides)
    return json.dumps(body)


async def run_until(subscriber: GameCompletedSubscriber, condition: Any) -> asyncio.Task[None]:
    task = asyncio.create_task(subscriber.run())
    for _ in range(200):
        if condition():
            return task
        await asyncio.sleep(0.005)
    task.cancel()
    raise AssertionError("condition never became true")


class TestHandle:
    async def test_valid_payload_dispatches_to_grader(self) -> None:
        grader = FakeGrader()
        pubsub = FakePubSub([{"type": "message", "data": payload()}], block=True)
        subscriber = GameCompletedSubscriber(FakeRedis(pubsub), grader)  # type: ignore[arg-type]

        task = await run_until(subscriber, lambda: grader.calls)
        assert subscriber.status == "running"
        assert pubsub.subscribed == [CHANNEL]
        call = grader.calls[0]
        assert call["game_id"] == "game-1"
        assert call["home_score"] == 110
        assert call["away_score"] == 100
        assert call["home_team"] == "LAL"
        assert call["regulation_home_score"] is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert subscriber.status == "stopped"

    async def test_non_message_types_are_ignored(self) -> None:
        grader = FakeGrader()
        pubsub = FakePubSub([{"type": "subscribe", "data": 1}, {"type": "message", "data": payload()}], block=True)
        subscriber = GameCompletedSubscriber(FakeRedis(pubsub), grader)  # type: ignore[arg-type]

        task = await run_until(subscriber, lambda: grader.calls)
        assert len(grader.calls) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.parametrize("raw", ["not json", json.dumps({"home_score": 1})])
    async def test_bad_payloads_are_swallowed(self, raw: str) -> None:
        grader = FakeGrader()
        pubsub = FakePubSub([{"type": "message", "data": raw}, {"type": "message", "data": payload()}], block=True)
        subscriber = GameCompletedSubscriber(FakeRedis(pubsub), grader)  # type: ignore[arg-type]

        # the good payload after the bad one proves the loop survived
        task = await run_until(subscriber, lambda: grader.calls)
        assert len(grader.calls) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_cancellation_during_handling_propagates(self) -> None:
        # shutdown must not be swallowed by the bad-payload guard
        grader = FakeGrader(error=asyncio.CancelledError())
        subscriber = GameCompletedSubscriber(
            FakeRedis(FakePubSub([])),  # type: ignore[arg-type]
            grader,  # type: ignore[arg-type]
        )
        with pytest.raises(asyncio.CancelledError):
            await subscriber._handle(payload())

    async def test_grader_failure_is_swallowed(self) -> None:
        grader = FakeGrader(error=RuntimeError("grading broke"))
        pubsub = FakePubSub([{"type": "message", "data": payload()}], block=True)
        subscriber = GameCompletedSubscriber(FakeRedis(pubsub), grader)  # type: ignore[arg-type]

        task = await run_until(subscriber, lambda: subscriber.status == "running")
        assert grader.calls == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestLifecycle:
    def test_starts_stopped(self) -> None:
        subscriber = GameCompletedSubscriber(
            FakeRedis(FakePubSub([])),  # type: ignore[arg-type]
            FakeGrader(),  # type: ignore[arg-type]
        )
        assert subscriber.status == "stopped"

    async def test_transport_failure_reconnects_with_backoff(self) -> None:
        grader = FakeGrader()
        pubsub = FakePubSub([{"type": "message", "data": payload()}], error=ConnectionError("redis gone"))
        subscriber = GameCompletedSubscriber(FakeRedis(pubsub), grader)  # type: ignore[arg-type]

        task = await run_until(subscriber, lambda: subscriber.status == "reconnecting")
        assert len(grader.calls) == 1  # the message before the failure was handled
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pubsub.closed is True  # the pubsub is closed on the way out
