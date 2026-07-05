"""bet.graded payload shape and fire-and-forget publish semantics."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from bookie_emulator.db.repository import PaperBetRecord
from bookie_emulator.events.publisher import BET_GRADED_CHANNEL, bet_graded_payload, publish_bet_graded


def make_bet(**overrides: Any) -> PaperBetRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "game_id": uuid.uuid4(),
        "game_external_id": "odds-api-game-1",
        "league": "NBA",
        "market_type": "SPREAD",
        "selection": "LAL -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "sportsbook_id": None,
        "sportsbook_key": "draftkings",
        "odds_american": -110,
        "odds_decimal": 1.909,
        "stake": 1.5,
        "predicted_probability": 0.56,
        "edge_at_placement": 0.04,
        "kelly_fraction": 0.08,
        "reasoning": None,
        "prediction_id": None,
        "edge_id": None,
        "idempotency_key": "key-1",
        "game_start_at": datetime(2026, 3, 1, 19, 0, tzinfo=UTC),
        "status": "OPEN",
        "placed_at": datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        "graded_at": None,
    }
    defaults.update(overrides)
    return PaperBetRecord(**defaults)


def grade_values(profit_loss: float = 1.36, clv: float | None = 0.021) -> dict[str, Any]:
    return {"profit_loss": profit_loss, "clv": clv}


class TestBetGradedPayload:
    def test_payload_fields(self) -> None:
        bet = make_bet()
        payload = bet_graded_payload(bet, "WON", grade_values())
        assert payload["event"] == "bet.graded"
        assert payload["bet_id"] == str(bet.id)
        assert payload["game_id"] == str(bet.game_id)
        assert payload["league"] == "NBA"
        assert payload["market_type"] == "SPREAD"
        assert payload["selection"] == "LAL -3.5"
        assert payload["sportsbook"] == "draftkings"
        assert payload["result"] == "WIN"
        assert payload["stake"] == 1.5
        assert payload["profit_loss"] == 1.36
        assert payload["clv"] == 0.021

    def test_timestamp_is_zulu_iso(self) -> None:
        payload = bet_graded_payload(make_bet(), "LOST", grade_values(profit_loss=-1.5))
        timestamp = payload["timestamp"]
        assert isinstance(timestamp, str)
        assert timestamp.endswith("Z")
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

    @pytest.mark.parametrize(
        ("status", "result"),
        [("WON", "WIN"), ("LOST", "LOSS"), ("PUSH", "PUSH"), ("VOID", "VOID")],
    )
    def test_result_uses_api_vocabulary(self, status: str, result: str) -> None:
        payload = bet_graded_payload(make_bet(), status, grade_values(profit_loss=0.0, clv=None))
        assert payload["result"] == result

    def test_null_clv_round_trips_json(self) -> None:
        payload = bet_graded_payload(make_bet(), "PUSH", grade_values(profit_loss=0.0, clv=None))
        assert json.loads(json.dumps(payload))["clv"] is None


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.published.append((channel, message))
        return 1


class TestPublishBetGraded:
    async def test_publishes_to_channel(self) -> None:
        redis = FakeRedis()
        await publish_bet_graded(redis, make_bet(), "WON", grade_values())  # type: ignore[arg-type]
        assert len(redis.published) == 1
        channel, message = redis.published[0]
        assert channel == BET_GRADED_CHANNEL
        assert json.loads(message)["result"] == "WIN"

    async def test_publish_failure_is_swallowed(self) -> None:
        redis = FakeRedis(fail=True)
        await publish_bet_graded(redis, make_bet(), "WON", grade_values())  # type: ignore[arg-type]
