"""Route handlers over stubbed services: envelope/pagination plumbing,
status-code semantics (201/200 replays, 404s, error envelope mapping), and
query-parameter translation into repository filters."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bookie_emulator.api.envelope import RequestIDMiddleware
from bookie_emulator.api.errors import DuplicateResourceError, register_error_handlers
from bookie_emulator.api.pagination import Cursor, encode_cursor
from bookie_emulator.api.routes import bankroll, bets, health, parlays, performance
from bookie_emulator.api.schemas import (
    BankrollConfigData,
    BankrollData,
    BankrollHistoryData,
    HealthData,
    HealthStats,
)
from bookie_emulator.config import Settings
from bookie_emulator.db.repository import (
    BetGradeRecord,
    LedgerFilters,
    PaperBetRecord,
    ParlayLegRecord,
)

GAME_ID = uuid.uuid4()
PLACED_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
GRADED_AT = datetime(2026, 7, 10, 22, 0, tzinfo=UTC)
UNIT_SIZE = 100.0


def make_bet(**overrides: Any) -> PaperBetRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "game_id": GAME_ID,
        "game_external_id": "odds-ext-1",
        "league": "NBA",
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "sportsbook_id": None,
        "sportsbook_key": "pinnacle",
        "odds_american": -110,
        "odds_decimal": 1.909,
        "stake": 1.5,
        "predicted_probability": 0.5712,
        "edge_at_placement": 0.042,
        "kelly_fraction": 0.25,
        "reasoning": None,
        "prediction_id": None,
        "edge_id": None,
        "idempotency_key": str(uuid.uuid4()),
        "game_start_at": datetime(2026, 7, 10, 19, 0, tzinfo=UTC),
        "status": "OPEN",
        "placed_at": PLACED_AT,
        "graded_at": None,
    }
    defaults.update(overrides)
    return PaperBetRecord(**defaults)


def make_grade(bet: PaperBetRecord, **overrides: Any) -> BetGradeRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "bet_id": bet.id,
        "actual_result": "Lakers won by 10",
        "actual_home_score": 110,
        "actual_away_score": 100,
        "actual_margin": 10,
        "actual_total": 210,
        "game_result_id": uuid.uuid4(),
        "profit_loss": 1.364,
        "closing_line_value": -4.0,
        "closing_odds": -120,
        "clv": 0.0216,
        "graded_at": GRADED_AT,
    }
    defaults.update(overrides)
    return BetGradeRecord(**defaults)


def make_leg(bet_id: uuid.UUID, **overrides: Any) -> ParlayLegRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "bet_id": bet_id,
        "leg_index": 0,
        "game_id": GAME_ID,
        "game_external_id": "odds-ext-1",
        "league": "NBA",
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "odds_american": 100,
        "odds_decimal": 2.0,
        "leg_status": "WON",
    }
    defaults.update(overrides)
    return ParlayLegRecord(**defaults)


class StubBetService:
    def __init__(self, bet: PaperBetRecord | None = None, created: bool = True, error: Exception | None = None):
        self.bet = bet or make_bet()
        self.created = created
        self.error = error
        self.keys: list[str] = []

    async def place_bet(self, request: Any, idempotency_key: str) -> tuple[PaperBetRecord, bool]:
        if self.error is not None:
            raise self.error
        self.keys.append(idempotency_key)
        return self.bet, self.created

    async def place_parlay(self, request: Any, idempotency_key: str) -> tuple[PaperBetRecord, list[Any], bool]:
        if self.error is not None:
            raise self.error
        self.keys.append(idempotency_key)
        return self.bet, [make_leg(self.bet.id)], self.created


class StubRepo:
    def __init__(
        self,
        rows: list[tuple[PaperBetRecord, BetGradeRecord | None]] | None = None,
        has_more: bool = False,
        found: tuple[PaperBetRecord, BetGradeRecord | None] | None = None,
        legs: list[ParlayLegRecord] | None = None,
        graded: list[tuple[PaperBetRecord, BetGradeRecord]] | None = None,
    ) -> None:
        self.rows = rows or []
        self.has_more = has_more
        self.found = found
        self.legs = legs or []
        self.graded = graded or []
        self.filters: LedgerFilters | None = None
        self.cursor: Cursor | None = None
        self.limit: int | None = None

    async def list_ledger(
        self, filters: LedgerFilters, limit: int, cursor: Cursor | None
    ) -> tuple[list[tuple[PaperBetRecord, BetGradeRecord | None]], bool]:
        self.filters, self.limit, self.cursor = filters, limit, cursor
        return self.rows, self.has_more

    async def get_with_grade(self, bet_id: uuid.UUID) -> tuple[PaperBetRecord, BetGradeRecord | None] | None:
        return self.found

    async def legs_for_bet(self, bet_id: uuid.UUID) -> list[ParlayLegRecord]:
        return self.legs

    async def graded_bets(self, filters: LedgerFilters) -> list[tuple[PaperBetRecord, BetGradeRecord]]:
        self.filters = filters
        return self.graded


GradeResult = tuple[PaperBetRecord, BetGradeRecord | None]


class StubGrader:
    def __init__(self, result: GradeResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[uuid.UUID, bool]] = []

    async def grade_manual(self, bet_id: uuid.UUID, force: bool = False) -> GradeResult:
        self.calls.append((bet_id, force))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class StubBankrollService:
    def __init__(self) -> None:
        self.history_args: tuple[Any, ...] | None = None

    async def current(self) -> BankrollData:
        return BankrollData(
            bankroll_units=112.5,
            bankroll_dollars=11250.0,
            unit_size_dollars=UNIT_SIZE,
            starting_bankroll_units=100.0,
            total_profit_units=12.5,
            open_bets_count=2,
            open_bets_exposure_units=3.0,
            config=BankrollConfigData(
                max_bet_units=3.0, max_daily_exposure_units=10.0, kelly_fraction=0.25, kelly_enabled=True
            ),
            snapshot_at=GRADED_AT,
        )

    async def history(self, date_from: Any, date_to: Any, interval: str) -> BankrollHistoryData:
        self.history_args = (date_from, date_to, interval)
        return BankrollHistoryData(interval=interval, snapshots=[])


class StubHealthService:
    async def health(self) -> HealthData:
        return HealthData(
            status="healthy",
            version="0.0.0-test",
            uptime_seconds=12,
            dependencies={"postgres": "healthy"},
            subscriber="running",
            stats=HealthStats(open_bets=1, bets_today=2, graded_today=3),
        )


def make_client(raise_server_exceptions: bool = True, **state: Any) -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    register_error_handlers(app)
    for router in (bets.router, parlays.router, performance.router, bankroll.router, health.router):
        app.include_router(router, prefix="/api/v1/emulator")
    app.state.settings = state.pop("settings", Settings(_env_file=None))
    for key, value in state.items():
        setattr(app.state, key, value)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def bet_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "game_id": str(GAME_ID),
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "predicted_probability": 0.5712,
        "edge_percentage": 4.2,
        "stake": 1.5,
    }
    body.update(overrides)
    return body


def parlay_body() -> dict[str, Any]:
    leg = {
        "game_id": str(GAME_ID),
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
    }
    return {
        "legs": [leg, {**leg, "game_id": str(uuid.uuid4())}],
        "predicted_probability": 0.28,
        "edge_percentage": 3.1,
        "stake": 1.0,
    }


class TestPlaceBetRoute:
    def test_created_bet_returns_201_envelope(self) -> None:
        service = StubBetService()
        client = make_client(bet_service=service)
        response = client.post("/api/v1/emulator/bets", json=bet_body(), headers={"X-Idempotency-Key": "key-123"})
        assert response.status_code == 201
        body = response.json()
        assert body["data"]["id"] == str(service.bet.id)
        assert body["data"]["stake_dollars"] == 150.0
        assert body["data"]["result"] == "PENDING"
        assert body["data"]["edge_percentage"] == 4.2
        assert body["meta"]["request_id"]
        assert service.keys == ["key-123"]

    def test_idempotent_replay_returns_200(self) -> None:
        client = make_client(bet_service=StubBetService(created=False))
        response = client.post("/api/v1/emulator/bets", json=bet_body(), headers={"X-Idempotency-Key": "key-123"})
        assert response.status_code == 200

    def test_missing_idempotency_header_is_a_validation_error(self) -> None:
        client = make_client(bet_service=StubBetService())
        response = client.post("/api/v1/emulator/bets", json=bet_body())
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert any("x-idempotency-key" in loc for e in error["details"]["errors"] for loc in e["loc"])

    def test_unexpected_failure_maps_to_internal_error_envelope(self) -> None:
        client = make_client(raise_server_exceptions=False, bet_service=StubBetService(error=RuntimeError("boom")))
        response = client.post("/api/v1/emulator/bets", json=bet_body(), headers={"X-Idempotency-Key": "key-123"})
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"

    def test_request_id_header_is_echoed(self) -> None:
        client = make_client(bet_service=StubBetService())
        response = client.post(
            "/api/v1/emulator/bets",
            json=bet_body(),
            headers={"X-Idempotency-Key": "key-123", "X-Request-ID": "req-42"},
        )
        assert response.headers["X-Request-ID"] == "req-42"
        assert response.json()["meta"]["request_id"] == "req-42"


class TestLedgerRoute:
    def test_filters_are_translated_to_db_vocabulary(self) -> None:
        repo = StubRepo()
        client = make_client(bet_repo=repo)
        response = client.get(
            "/api/v1/emulator/bets",
            params={
                "league": "NBA",
                "market_type": "SPREAD",
                "result": "WIN",
                "status": "graded",
                "min_edge": 4.2,
                "is_parlay": "false",
                "is_live": "true",
                "date_from": "2026-07-01",
                "limit": 10,
            },
        )
        assert response.status_code == 200
        assert repo.filters is not None
        assert repo.filters.result == "WON"  # API WIN -> DB WON
        assert repo.filters.min_edge == 0.042  # percent -> fraction
        assert repo.filters.status == "graded"
        assert repo.filters.is_parlay is False
        assert repo.filters.is_live is True
        assert repo.filters.date_from == datetime(2026, 7, 1, tzinfo=UTC)  # naive dates become UTC
        assert repo.limit == 10

    def test_page_with_more_rows_returns_next_cursor(self) -> None:
        bet = make_bet()
        repo = StubRepo(rows=[(bet, None)], has_more=True)
        client = make_client(bet_repo=repo)
        body = client.get("/api/v1/emulator/bets").json()
        pagination = body["meta"]["pagination"]
        assert pagination["has_more"] is True
        assert pagination["next_cursor"] == encode_cursor(Cursor(placed_at=bet.placed_at, id=bet.id))
        assert [row["id"] for row in body["data"]] == [str(bet.id)]

    def test_final_page_has_no_cursor(self) -> None:
        repo = StubRepo(rows=[(make_bet(), None)], has_more=False)
        client = make_client(bet_repo=repo)
        pagination = client.get("/api/v1/emulator/bets").json()["meta"]["pagination"]
        assert pagination["has_more"] is False
        assert pagination["next_cursor"] is None

    def test_inbound_cursor_is_decoded_for_the_repo(self) -> None:
        bet = make_bet()
        repo = StubRepo()
        client = make_client(bet_repo=repo)
        cursor = encode_cursor(Cursor(placed_at=bet.placed_at, id=bet.id))
        assert client.get("/api/v1/emulator/bets", params={"cursor": cursor}).status_code == 200
        assert repo.cursor == Cursor(placed_at=bet.placed_at, id=bet.id)

    def test_graded_row_carries_profit_and_clv(self) -> None:
        bet = make_bet(status="WON", graded_at=GRADED_AT)
        repo = StubRepo(rows=[(bet, make_grade(bet))])
        client = make_client(bet_repo=repo)
        row = client.get("/api/v1/emulator/bets").json()["data"][0]
        assert row["result"] == "WIN"
        assert row["profit_loss"] == 1.364
        assert row["profit_loss_dollars"] == 136.4
        assert row["clv"] == 0.0216


class TestBetDetailRoute:
    def test_graded_bet_detail_includes_grade_block(self) -> None:
        bet = make_bet(status="WON", graded_at=GRADED_AT)
        grade = make_grade(bet)
        client = make_client(bet_repo=StubRepo(found=(bet, grade)))
        data = client.get(f"/api/v1/emulator/bets/{bet.id}").json()["data"]
        assert data["closing_odds_american"] == -120
        assert data["grade"]["result"] == "WIN"
        assert data["grade"]["result_description"] == "Lakers won by 10"
        assert data["grade"]["actual_home_score"] == 110

    def test_open_bet_detail_has_null_grade(self) -> None:
        bet = make_bet()
        client = make_client(bet_repo=StubRepo(found=(bet, None)))
        data = client.get(f"/api/v1/emulator/bets/{bet.id}").json()["data"]
        assert data["grade"] is None
        assert data["closing_line_value"] is None

    def test_unknown_bet_404_envelope(self) -> None:
        client = make_client(bet_repo=StubRepo(found=None))
        response = client.get(f"/api/v1/emulator/bets/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


class TestManualGradeRoute:
    def test_grades_and_returns_detail(self) -> None:
        bet = make_bet(status="WON", graded_at=GRADED_AT)
        grader = StubGrader(result=(bet, make_grade(bet)))
        client = make_client(grader=grader)
        response = client.post(f"/api/v1/emulator/bets/{bet.id}/grade", json={})
        assert response.status_code == 200
        assert response.json()["data"]["result"] == "WIN"
        assert grader.calls == [(bet.id, False)]

    def test_force_flag_is_forwarded(self) -> None:
        bet = make_bet(status="WON", graded_at=GRADED_AT)
        grader = StubGrader(result=(bet, make_grade(bet)))
        client = make_client(grader=grader)
        client.post(f"/api/v1/emulator/bets/{bet.id}/grade", json={"force": True})
        assert grader.calls == [(bet.id, True)]

    def test_duplicate_grade_maps_to_409(self) -> None:
        grader = StubGrader(error=DuplicateResourceError("already graded"))
        client = make_client(grader=grader)
        response = client.post(f"/api/v1/emulator/bets/{uuid.uuid4()}/grade", json={})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_RESOURCE"


class TestParlayRoutes:
    def test_place_parlay_returns_201_with_legs(self) -> None:
        parent = make_bet(game_id=None, side=None, is_parlay=True, odds_american=300, odds_decimal=4.0)
        client = make_client(bet_service=StubBetService(bet=parent))
        response = client.post("/api/v1/emulator/parlays", json=parlay_body(), headers={"X-Idempotency-Key": "key-9"})
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["combined_odds_decimal"] == 4.0
        assert data["combined_odds_american"] == 300
        assert [leg["leg_status"] for leg in data["legs"]] == ["WIN"]

    def test_parlay_replay_returns_200(self) -> None:
        parent = make_bet(game_id=None, side=None, is_parlay=True)
        client = make_client(bet_service=StubBetService(bet=parent, created=False))
        response = client.post("/api/v1/emulator/parlays", json=parlay_body(), headers={"X-Idempotency-Key": "key-9"})
        assert response.status_code == 200

    def test_get_parlay_returns_detail_with_legs(self) -> None:
        parent = make_bet(game_id=None, side=None, is_parlay=True)
        repo = StubRepo(found=(parent, None), legs=[make_leg(parent.id), make_leg(parent.id, leg_index=1)])
        client = make_client(bet_repo=repo)
        data = client.get(f"/api/v1/emulator/parlays/{parent.id}").json()["data"]
        assert data["is_parlay"] is True
        assert [leg["leg_index"] for leg in data["legs"]] == [0, 1]

    def test_get_parlay_on_single_bet_404(self) -> None:
        single = make_bet()
        client = make_client(bet_repo=StubRepo(found=(single, None)))
        response = client.get(f"/api/v1/emulator/parlays/{single.id}")
        assert response.status_code == 404
        assert "Parlay" in response.json()["error"]["message"]

    def test_get_unknown_parlay_404(self) -> None:
        client = make_client(bet_repo=StubRepo(found=None))
        assert client.get(f"/api/v1/emulator/parlays/{uuid.uuid4()}").status_code == 404


def graded_pair(profit: float, status: str, probability: float = 0.55) -> tuple[PaperBetRecord, BetGradeRecord]:
    bet = make_bet(status=status, graded_at=GRADED_AT, stake=1.0, predicted_probability=probability)
    return bet, make_grade(bet, profit_loss=profit)


class TestPerformanceRoutes:
    def test_aggregates_graded_bets(self) -> None:
        repo = StubRepo(graded=[graded_pair(0.909, "WON"), graded_pair(-1.0, "LOST")])
        client = make_client(bet_repo=repo)
        data = client.get("/api/v1/emulator/performance").json()["data"]
        assert data["total_bets"] == 2
        assert data["total_wins"] == 1
        assert data["total_losses"] == 1
        assert data["win_rate"] == 0.5
        assert data["total_wagered_units"] == 2.0
        assert data["total_profit_units"] == pytest.approx(-0.091)
        assert data["total_profit_dollars"] == pytest.approx(-9.1)
        assert data["period"]["window"] == "all_time"
        assert data["period"]["from"] == PLACED_AT.isoformat().replace("+00:00", "Z")

    def test_rolling_window_sets_graded_from_filter(self) -> None:
        repo = StubRepo(graded=[])
        client = make_client(bet_repo=repo)
        data = client.get("/api/v1/emulator/performance", params={"window": "weekly"}).json()["data"]
        assert repo.filters is not None and repo.filters.graded_from is not None
        assert (datetime.now(tz=UTC) - repo.filters.graded_from).days == 7
        assert data["total_bets"] == 0
        assert data["brier_score"] is None

    def test_calibration_returns_all_requested_bins(self) -> None:
        repo = StubRepo(graded=[graded_pair(0.909, "WON", probability=0.62)])
        client = make_client(bet_repo=repo)
        data = client.get("/api/v1/emulator/performance/calibration", params={"bins": 5}).json()["data"]
        assert data["n_bins"] == 5
        assert len(data["bins"]) == 5
        assert data["total_graded"] == 1
        occupied = [b for b in data["bins"] if b["bet_count"]]
        assert occupied[0]["avg_predicted_probability"] == 0.62
        assert occupied[0]["actual_win_rate"] == 1.0

    def test_breakdown_groups_by_market_type(self) -> None:
        repo = StubRepo(graded=[graded_pair(0.909, "WON"), graded_pair(-1.0, "LOST")])
        client = make_client(bet_repo=repo)
        data = client.get("/api/v1/emulator/performance/breakdown", params={"group_by": "market_type"}).json()["data"]
        assert data["group_by"] == "market_type"
        assert data["breakdowns"][0]["group"] == "SPREAD"
        assert data["breakdowns"][0]["total_bets"] == 2


class TestBankrollRoutes:
    def test_current_bankroll_envelope(self) -> None:
        client = make_client(bankroll_service=StubBankrollService())
        data = client.get("/api/v1/emulator/bankroll").json()["data"]
        assert data["bankroll_units"] == 112.5
        assert data["config"]["kelly_enabled"] is True

    def test_history_passes_utc_range_and_interval(self) -> None:
        service = StubBankrollService()
        client = make_client(bankroll_service=service)
        data = client.get(
            "/api/v1/emulator/bankroll/history",
            params={"date_from": "2026-06-01", "date_to": "2026-06-30", "interval": "weekly"},
        ).json()["data"]
        assert data["interval"] == "weekly"
        assert service.history_args == (
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 30, tzinfo=UTC),
            "weekly",
        )


class TestHealthRoute:
    def test_health_returns_service_report(self) -> None:
        client = make_client(health_service=StubHealthService())
        response = client.get("/api/v1/emulator/health")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "healthy"
        assert data["subscriber"] == "running"
        assert data["stats"]["graded_today"] == 3
