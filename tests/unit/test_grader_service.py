"""GraderService orchestration branches beyond the prop-routing suite:
manual-grading error paths, CLV capture, event publishing, regulation-score
settlement, and parlay-leg grading edge cases (ADR-028)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from bookie_emulator.api.errors import DuplicateResourceError, NotFoundError, UnprocessableError
from bookie_emulator.clients.lines import LineSnapshot
from bookie_emulator.clients.statistics import BoxScore, Game, SoccerPlayerBoxScore, TeamBoxScore
from bookie_emulator.config import Settings
from bookie_emulator.db.repository import PaperBetRecord, ParlayLegRecord
from bookie_emulator.events.publisher import BET_GRADED_CHANNEL
from bookie_emulator.services.grader import GraderService

GAME_ID = uuid.uuid4()
EXT_ID = "odds-api-game-1"


def make_bet(**overrides: Any) -> PaperBetRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "game_id": GAME_ID,
        "game_external_id": EXT_ID,
        "league": "NBA",
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "sportsbook_id": None,
        "sportsbook_key": "pinnacle",
        "odds_american": -110,
        "odds_decimal": 1.909,
        "stake": 1.0,
        "predicted_probability": 0.5712,
        "edge_at_placement": 0.042,
        "kelly_fraction": 0.25,
        "reasoning": None,
        "prediction_id": None,
        "edge_id": None,
        "idempotency_key": str(uuid.uuid4()),
        "game_start_at": datetime(2026, 7, 10, 19, 0, tzinfo=UTC),
        "status": "OPEN",
        "placed_at": datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        "graded_at": None,
    }
    defaults.update(overrides)
    return PaperBetRecord(**defaults)


def make_leg(**overrides: Any) -> ParlayLegRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "bet_id": uuid.uuid4(),
        "leg_index": 0,
        "game_id": GAME_ID,
        "game_external_id": EXT_ID,
        "league": "NBA",
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -3.5",
        "side": "HOME",
        "line_value": -3.5,
        "odds_american": 100,
        "odds_decimal": 2.0,
        "leg_status": "OPEN",
    }
    defaults.update(overrides)
    return ParlayLegRecord(**defaults)


def closing_snapshot(**overrides: Any) -> LineSnapshot:
    defaults: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "game_id": EXT_ID,
        "sportsbook_key": "pinnacle",
        "market_type": "SPREAD",
        "selection": "Los Angeles Lakers -4",
        "side": "HOME",
        "line_value": -4.0,
        "odds_american": -120,
        "odds_decimal": 1.833,
        "is_closing": True,
        "timestamp": "2026-07-10T21:00:00Z",
    }
    defaults.update(overrides)
    return LineSnapshot(**defaults)


def make_final_game(league: str = "NBA", home: int = 110, away: int = 100) -> Game:
    return Game.model_validate(
        {
            "id": str(GAME_ID),
            "league": league,
            "status": "FINAL",
            "home_team": {"id": "t-home", "name": "Los Angeles Lakers", "abbreviation": "LAL"},
            "away_team": {"id": "t-away", "name": "Boston Celtics", "abbreviation": "BOS"},
            "scheduled_start": "2026-07-10T19:00:00Z",
            "season": 2026,
            "result": {
                "id": str(uuid.uuid4()),
                "home_score": home,
                "away_score": away,
                "total_score": home + away,
                "margin": home - away,
                "overtime": False,
                "completed_at": "2026-07-10T22:00:00Z",
            },
        }
    )


class FakeRepo:
    def __init__(
        self,
        bets: list[PaperBetRecord] | None = None,
        legs: list[ParlayLegRecord] | None = None,
        by_id: dict[uuid.UUID, PaperBetRecord] | None = None,
        legs_by_bet: dict[uuid.UUID, list[ParlayLegRecord]] | None = None,
        open_legs: dict[uuid.UUID, int] | None = None,
    ) -> None:
        self.bets = bets or []
        self.legs = legs or []
        self.by_id = by_id or {}
        self.legs_by_bet = legs_by_bet or {}
        self.open_legs = open_legs or {}
        self.applied: list[tuple[uuid.UUID, str, dict[str, Any], bool]] = []
        self.graded_legs: list[tuple[uuid.UUID, str]] = []

    async def open_bets_for_game(self, game_id: uuid.UUID) -> list[PaperBetRecord]:
        return self.bets

    async def open_parlay_legs_for_game(self, game_id: uuid.UUID) -> list[ParlayLegRecord]:
        return self.legs

    async def apply_grade(
        self,
        bet_id: uuid.UUID,
        status: str,
        grade_values: dict[str, Any],
        starting_bankroll: float,
        force: bool = False,
    ) -> bool:
        self.applied.append((bet_id, status, grade_values, force))
        return True

    async def get_with_grade(self, bet_id: uuid.UUID) -> tuple[PaperBetRecord, None] | None:
        bet = self.by_id.get(bet_id)
        return (bet, None) if bet is not None else None

    async def legs_for_bet(self, bet_id: uuid.UUID) -> list[ParlayLegRecord]:
        return self.legs_by_bet.get(bet_id, [])

    async def grade_leg(self, leg_id: uuid.UUID, status: str) -> bool:
        self.graded_legs.append((leg_id, status))
        return True

    async def open_leg_count(self, bet_id: uuid.UUID) -> int:
        return self.open_legs.get(bet_id, 0)


class FakeStatistics:
    def __init__(self, game: Game | None = None, error: Exception | None = None, box: BoxScore | None = None) -> None:
        self.game = game
        self.error = error
        self.box = box

    async def get_game(self, game_id: str) -> Game:
        if self.error is not None:
            raise self.error
        assert self.game is not None
        return self.game

    async def get_box_score(self, game_id: str) -> BoxScore:
        assert self.box is not None
        return self.box


class FakeLines:
    def __init__(self, closing: list[LineSnapshot] | None = None, error: Exception | None = None) -> None:
        self.closing = closing or []
        self.error = error

    async def closing_lines(self, game_external_id: str) -> list[LineSnapshot]:
        if self.error is not None:
            raise self.error
        return self.closing


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, json.loads(payload)))


def make_grader(
    repo: FakeRepo,
    statistics: FakeStatistics | None = None,
    lines: FakeLines | None = None,
    redis_client: FakeRedis | None = None,
) -> GraderService:
    return GraderService(
        statistics or FakeStatistics(),  # type: ignore[arg-type]
        lines or FakeLines(),  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        Settings(_env_file=None),
        redis_client=redis_client,  # type: ignore[arg-type]
    )


class TestGradeManualErrors:
    async def test_unknown_bet_404(self) -> None:
        grader = make_grader(FakeRepo())
        with pytest.raises(NotFoundError, match="not found"):
            await grader.grade_manual(uuid.uuid4())

    async def test_already_graded_without_force_409(self) -> None:
        bet = make_bet(status="WON", graded_at=datetime(2026, 7, 11, 0, 0, tzinfo=UTC))
        grader = make_grader(FakeRepo(by_id={bet.id: bet}))
        with pytest.raises(DuplicateResourceError, match="pass force=true"):
            await grader.grade_manual(bet.id)

    async def test_parlay_with_open_legs_422(self) -> None:
        parent = make_bet(game_id=None, side=None, is_parlay=True)
        legs = [make_leg(bet_id=parent.id), make_leg(bet_id=parent.id, leg_index=1, leg_status="WON")]
        grader = make_grader(FakeRepo(by_id={parent.id: parent}, legs_by_bet={parent.id: legs}))
        with pytest.raises(UnprocessableError, match="still has 1 open legs") as exc_info:
            await grader.grade_manual(parent.id)
        assert "box scores" not in str(exc_info.value)

    async def test_parlay_with_open_prop_leg_hints_box_scores(self) -> None:
        parent = make_bet(game_id=None, side=None, is_parlay=True)
        leg = make_leg(bet_id=parent.id, market_type="PLAYER_PROP", side="OVER", prop_type="OVER_UNDER")
        grader = make_grader(FakeRepo(by_id={parent.id: parent}, legs_by_bet={parent.id: [leg]}))
        with pytest.raises(UnprocessableError, match="box scores"):
            await grader.grade_manual(parent.id)

    async def test_bet_without_game_reference_422(self) -> None:
        bet = make_bet(game_id=None)
        grader = make_grader(FakeRepo(by_id={bet.id: bet}))
        with pytest.raises(UnprocessableError, match="no game reference"):
            await grader.grade_manual(bet.id)

    async def test_game_missing_in_statistics_422(self) -> None:
        bet = make_bet()
        grader = make_grader(FakeRepo(by_id={bet.id: bet}), FakeStatistics(error=NotFoundError("gone")))
        with pytest.raises(UnprocessableError, match="not found in statistics-service"):
            await grader.grade_manual(bet.id)

    async def test_game_not_final_422(self) -> None:
        game = make_final_game()
        pending = game.model_copy(update={"status": "IN_PROGRESS", "result": None})
        bet = make_bet()
        grader = make_grader(FakeRepo(by_id={bet.id: bet}), FakeStatistics(game=pending))
        with pytest.raises(UnprocessableError, match="has not completed yet"):
            await grader.grade_manual(bet.id)


class TestGradeManualScoreBets:
    async def test_won_spread_with_matched_closing_line_carries_clv(self) -> None:
        bet = make_bet()
        repo = FakeRepo(by_id={bet.id: bet})
        lines = FakeLines(closing=[closing_snapshot()])
        grader = make_grader(repo, FakeStatistics(game=make_final_game()), lines)

        refreshed, _ = await grader.grade_manual(bet.id)
        assert refreshed.id == bet.id
        _, status, grade_values, force = repo.applied[0]
        assert status == "WON"
        assert force is False
        assert grade_values["closing_odds"] == -120
        assert grade_values["closing_line_value"] == -4.0
        # implied(-120) - implied(-110) = 120/220 - 110/210
        assert grade_values["clv"] == pytest.approx(120 / 220 - 110 / 210)
        assert grade_values["profit_loss"] == pytest.approx(0.909)

    async def test_zero_odds_closing_line_leaves_clv_null(self) -> None:
        bet = make_bet()
        repo = FakeRepo(by_id={bet.id: bet})
        lines = FakeLines(closing=[closing_snapshot(odds_american=0)])
        grader = make_grader(repo, FakeStatistics(game=make_final_game()), lines)

        await grader.grade_manual(bet.id)
        grade_values = repo.applied[0][2]
        assert grade_values["clv"] is None
        assert grade_values["closing_odds"] is None

    async def test_closing_lines_failure_grades_without_clv(self) -> None:
        bet = make_bet()
        repo = FakeRepo(by_id={bet.id: bet})
        lines = FakeLines(error=RuntimeError("lines-service down"))
        grader = make_grader(repo, FakeStatistics(game=make_final_game()), lines)

        await grader.grade_manual(bet.id)
        _, status, grade_values, _ = repo.applied[0]
        assert status == "WON"
        assert grade_values["clv"] is None

    async def test_grade_publishes_bet_graded_event(self) -> None:
        bet = make_bet()
        repo = FakeRepo(by_id={bet.id: bet})
        redis = FakeRedis()
        grader = make_grader(repo, FakeStatistics(game=make_final_game()), redis_client=redis)

        await grader.grade_manual(bet.id)
        assert len(redis.published) == 1
        channel, payload = redis.published[0]
        assert channel == BET_GRADED_CHANNEL
        assert payload["event"] == "bet.graded"
        assert payload["bet_id"] == str(bet.id)
        assert payload["result"] == "WIN"  # events speak the API vocabulary


class TestGradeManualParlaySettlement:
    async def test_decided_parlay_settles_and_returns_refreshed_parent(self) -> None:
        parent = make_bet(game_id=None, side=None, is_parlay=True, odds_american=300, odds_decimal=4.0, stake=2.0)
        legs = [make_leg(bet_id=parent.id, leg_status="WON"), make_leg(bet_id=parent.id, leg_index=1, leg_status="WON")]
        repo = FakeRepo(by_id={parent.id: parent}, legs_by_bet={parent.id: legs})
        grader = make_grader(repo)

        refreshed, _ = await grader.grade_manual(parent.id)
        assert refreshed.id == parent.id
        bet_id, status, grade_values, force = repo.applied[0]
        assert bet_id == parent.id
        assert status == "WON"
        assert force is False
        assert grade_values["profit_loss"] == pytest.approx(6.0)

    async def test_force_resettles_a_graded_parlay(self) -> None:
        parent = make_bet(
            game_id=None,
            side=None,
            is_parlay=True,
            status="LOST",
            graded_at=datetime(2026, 7, 11, 0, 0, tzinfo=UTC),
            odds_decimal=4.0,
        )
        legs = [make_leg(bet_id=parent.id, leg_status="WON"), make_leg(bet_id=parent.id, leg_index=1, leg_status="WON")]
        repo = FakeRepo(by_id={parent.id: parent}, legs_by_bet={parent.id: legs})
        grader = make_grader(repo)

        await grader.grade_manual(parent.id, force=True)
        assert repo.applied[0][1] == "WON"
        assert repo.applied[0][3] is True  # force forwarded into apply_grade


class TestGradeGameEdges:
    async def test_no_open_bets_or_legs_grades_nothing(self) -> None:
        grader = make_grader(FakeRepo())
        assert await grader.grade_game(str(GAME_ID), home_score=110, away_score=100) == 0

    async def test_prop_grade_publishes_bet_graded_event(self) -> None:
        bet = make_bet(
            league="FIFA_WC",
            market_type="PLAYER_PROP",
            selection="Erling Haaland Over 2.5 Shots",
            side="OVER",
            line_value=2.5,
            player_external_id="erling-haaland",
            stat_type="player_shots",
            prop_type="OVER_UNDER",
        )
        box = BoxScore(
            game_id=str(GAME_ID),
            sport="SOCCER",
            status="FINAL",
            home_team=TeamBoxScore(
                id="t-home",
                abbreviation="MCI",
                score=2,
                soccer_players=[
                    SoccerPlayerBoxScore(player_id="p-1", player_name="Erling Haaland", minutes=90, shots=3)
                ],
            ),
            away_team=TeamBoxScore(id="t-away", abbreviation="PSG", score=1),
        )
        repo = FakeRepo(bets=[bet])
        redis = FakeRedis()
        grader = make_grader(repo, FakeStatistics(box=box), redis_client=redis)

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 1
        channel, payload = redis.published[0]
        assert channel == BET_GRADED_CHANNEL
        assert payload["result"] == "WIN"
        assert payload["market_type"] == "PLAYER_PROP"


class TestRegulationScoreSettlement:
    async def test_soccer_draw_settles_on_regulation_scores(self) -> None:
        # 1-1 after 90 minutes, 2-1 after extra time: DRAW wins on regulation
        bet = make_bet(league="FIFA_WC", market_type="MONEYLINE", side="DRAW", line_value=None, selection="Draw")
        repo = FakeRepo(bets=[bet])
        grader = make_grader(repo)

        graded = await grader.grade_game(
            str(GAME_ID),
            home_score=2,
            away_score=1,
            total=3,
            margin=1,
            regulation_home_score=1,
            regulation_away_score=1,
        )
        assert graded == 1
        _, status, grade_values, _ = repo.applied[0]
        assert status == "WON"
        assert grade_values["actual_home_score"] == 1
        assert grade_values["actual_away_score"] == 1
        assert grade_values["actual_margin"] == 0
        assert grade_values["actual_total"] == 2


class TestParlayLegGrading:
    async def test_team_prop_leg_is_skipped(self) -> None:
        leg = make_leg(market_type="TEAM_PROP", side="OVER", stat_type="team_total_goals")
        repo = FakeRepo(legs=[leg])
        grader = make_grader(repo)

        assert await grader.grade_game(str(GAME_ID), home_score=110, away_score=100) == 0
        assert repo.graded_legs == []

    async def test_ungradeable_leg_row_is_skipped(self) -> None:
        # a sideless SPREAD row cannot come through the API; grade_bet's
        # ValueError guard keeps the sweep alive
        leg = make_leg(side=None)
        repo = FakeRepo(legs=[leg])
        grader = make_grader(repo)

        assert await grader.grade_game(str(GAME_ID), home_score=110, away_score=100) == 0
        assert repo.graded_legs == []

    async def test_leg_graded_but_parent_still_open_does_not_settle(self) -> None:
        parent_id = uuid.uuid4()
        leg = make_leg(bet_id=parent_id)
        repo = FakeRepo(legs=[leg], open_legs={parent_id: 1})
        grader = make_grader(repo)

        assert await grader.grade_game(str(GAME_ID), home_score=110, away_score=100) == 0
        assert repo.graded_legs == [(leg.id, "WON")]
        assert repo.applied == []

    async def test_vanished_parent_does_not_settle(self) -> None:
        leg = make_leg()
        repo = FakeRepo(legs=[leg])  # get_with_grade knows no parent
        grader = make_grader(repo)

        assert await grader.grade_game(str(GAME_ID), home_score=110, away_score=100) == 0
        assert repo.graded_legs == [(leg.id, "WON")]

    async def test_last_leg_settles_parent_and_publishes(self) -> None:
        parent = make_bet(game_id=None, side=None, is_parlay=True, odds_american=300, odds_decimal=4.0, stake=2.0)
        winning = make_leg(bet_id=parent.id)
        decided = make_leg(bet_id=parent.id, leg_index=1, leg_status="WON")
        repo = FakeRepo(
            legs=[winning],
            by_id={parent.id: parent},
            legs_by_bet={parent.id: [make_leg(bet_id=parent.id, leg_status="WON"), decided]},
        )
        redis = FakeRedis()
        grader = make_grader(repo, redis_client=redis)

        assert await grader.grade_game(str(GAME_ID), home_score=110, away_score=100) == 1
        bet_id, status, grade_values, _ = repo.applied[0]
        assert bet_id == parent.id
        assert status == "WON"
        # both legs at 2.0: repriced 4.0 decimal on a 2-unit stake
        assert grade_values["profit_loss"] == pytest.approx(6.0)
        assert grade_values["actual_home_score"] is None  # the parent spans games
        assert redis.published[0][1]["result"] == "WIN"

    async def test_score_bet_grade_publishes_via_game_path(self) -> None:
        bet = make_bet()
        repo = FakeRepo(bets=[bet])
        redis = FakeRedis()
        grader = make_grader(repo, redis_client=redis)

        assert await grader.grade_game(str(GAME_ID), home_score=110, away_score=100) == 1
        assert redis.published[0][0] == BET_GRADED_CHANNEL
