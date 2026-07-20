"""Phase 7 Wave 3 grader routing: prop bets split from score markets and
grade from stubbed box scores; failures leave props OPEN, never VOID."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from bookie_emulator.api.errors import NotFoundError, UnprocessableError
from bookie_emulator.clients.statistics import BoxScore, Game, SoccerPlayerBoxScore, TeamBoxScore
from bookie_emulator.config import Settings
from bookie_emulator.db.repository import PaperBetRecord
from bookie_emulator.services.grader import GraderService

GAME_ID = uuid.uuid4()


def make_bet(**overrides: Any) -> PaperBetRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "game_id": GAME_ID,
        "game_external_id": "odds-api-game-1",
        "league": "FIFA_WC",
        "market_type": "PLAYER_PROP",
        "selection": "Erling Haaland Over 2.5 Shots",
        "side": "OVER",
        "line_value": 2.5,
        "sportsbook_id": None,
        "sportsbook_key": "draftkings",
        "odds_american": -110,
        "odds_decimal": 1.909,
        "stake": 1.0,
        "predicted_probability": 0.56,
        "edge_at_placement": 0.04,
        "kelly_fraction": 0.08,
        "reasoning": None,
        "prediction_id": None,
        "edge_id": None,
        "idempotency_key": str(uuid.uuid4()),
        "game_start_at": datetime(2026, 7, 10, 19, 0, tzinfo=UTC),
        "status": "OPEN",
        "placed_at": datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        "graded_at": None,
        "player_external_id": "erling-haaland",
        "stat_type": "player_shots",
        "prop_type": "OVER_UNDER",
    }
    defaults.update(overrides)
    return PaperBetRecord(**defaults)


def make_box(players: list[SoccerPlayerBoxScore] | None = None) -> BoxScore:
    if players is None:
        players = [
            SoccerPlayerBoxScore(
                player_id="stats-uuid-1",
                player_name="Erling Haaland",
                position="F",
                minutes=90,
                goals=1,
                shots=3,
                shots_on_target=2,
            )
        ]
    return BoxScore(
        game_id=str(GAME_ID),
        sport="SOCCER",
        status="FINAL",
        home_team=TeamBoxScore(id="t-home", abbreviation="MCI", score=2, soccer_players=players),
        away_team=TeamBoxScore(id="t-away", abbreviation="PSG", score=1),
    )


class FakeRepo:
    def __init__(self, bets: list[PaperBetRecord]) -> None:
        self.bets = bets
        self.applied: list[tuple[uuid.UUID, str, dict[str, Any]]] = []

    async def open_bets_for_game(self, game_id: uuid.UUID) -> list[PaperBetRecord]:
        return self.bets

    async def open_parlay_legs_for_game(self, game_id: uuid.UUID) -> list[Any]:
        return []

    async def apply_grade(
        self,
        bet_id: uuid.UUID,
        status: str,
        grade_values: dict[str, Any],
        starting_bankroll: float,
        force: bool = False,
    ) -> bool:
        self.applied.append((bet_id, status, grade_values))
        return True

    async def get_with_grade(self, bet_id: uuid.UUID) -> tuple[PaperBetRecord, None] | None:
        for bet in self.bets:
            if bet.id == bet_id:
                return bet, None
        return None


class FakeStatistics:
    def __init__(self, box: BoxScore | None = None, box_error: Exception | None = None, game: Game | None = None):
        self.box = box
        self.box_error = box_error
        self.game = game
        self.box_calls = 0

    async def get_box_score(self, game_id: str) -> BoxScore:
        self.box_calls += 1
        if self.box_error is not None:
            raise self.box_error
        assert self.box is not None
        return self.box

    async def get_game(self, game_id: str) -> Game:
        assert self.game is not None
        return self.game


class FakeLines:
    def __init__(self) -> None:
        self.closing_calls = 0

    async def closing_lines(self, game_external_id: str) -> list[Any]:
        self.closing_calls += 1
        return []


def make_grader(repo: FakeRepo, statistics: FakeStatistics, lines: FakeLines | None = None) -> GraderService:
    return GraderService(statistics, lines or FakeLines(), repo, Settings())  # type: ignore[arg-type]


class TestGradeGamePropRouting:
    async def test_matched_player_prop_grades_won_with_stat_value(self) -> None:
        repo = FakeRepo([make_bet()])
        grader = make_grader(repo, FakeStatistics(box=make_box()))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 1
        assert len(repo.applied) == 1
        bet_id, status, grade_values = repo.applied[0]
        assert status == "WON"
        assert grade_values["actual_stat_value"] == 3.0
        assert grade_values["stat_type"] == "player_shots"
        assert grade_values["actual_result"] == "Erling Haaland landed 3 shots, over 2.5"
        assert grade_values["actual_home_score"] == 2
        assert grade_values["clv"] is None  # CLV for props is deferred in v1
        assert grade_values["closing_odds"] is None

    async def test_yes_no_prop_grades_from_goals(self) -> None:
        bet = make_bet(
            selection="Erling Haaland Anytime Goalscorer",
            side="YES",
            line_value=None,
            stat_type="player_goal_scorer_anytime",
            prop_type="YES_NO",
        )
        repo = FakeRepo([bet])
        grader = make_grader(repo, FakeStatistics(box=make_box()))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 1
        _, status, grade_values = repo.applied[0]
        assert status == "WON"
        assert grade_values["actual_stat_value"] == 1.0

    async def test_unmatched_player_stays_open(self) -> None:
        repo = FakeRepo([make_bet(player_external_id="lionel-messi")])
        grader = make_grader(repo, FakeStatistics(box=make_box()))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 0
        assert repo.applied == []

    async def test_box_score_404_stays_open(self) -> None:
        repo = FakeRepo([make_bet()])
        grader = make_grader(repo, FakeStatistics(box_error=NotFoundError("box score not found")))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 0
        assert repo.applied == []

    async def test_box_score_failure_stays_open(self) -> None:
        repo = FakeRepo([make_bet()])
        grader = make_grader(repo, FakeStatistics(box_error=RuntimeError("stats down")))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 0
        assert repo.applied == []

    async def test_team_and_game_props_skipped_without_box_fetch(self) -> None:
        bets = [
            make_bet(market_type="TEAM_PROP", stat_type="team_total_goals"),
            make_bet(market_type="GAME_PROP", stat_type="game_total_cards"),
        ]
        repo = FakeRepo(bets)
        statistics = FakeStatistics(box=make_box())
        grader = make_grader(repo, statistics)

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 0
        assert repo.applied == []
        assert statistics.box_calls == 0  # no PLAYER_PROP bets: no box-score fetch

    async def test_unknown_stat_type_stays_open(self) -> None:
        repo = FakeRepo([make_bet(stat_type="player_tackles")])
        grader = make_grader(repo, FakeStatistics(box=make_box()))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 0
        assert repo.applied == []

    async def test_wrong_sport_stat_type_stays_open(self) -> None:
        # a basketball stat_type against a soccer box score must not grade
        repo = FakeRepo([make_bet(stat_type="player_points")])
        grader = make_grader(repo, FakeStatistics(box=make_box()))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 0
        assert repo.applied == []

    async def test_score_bets_still_grade_when_box_score_is_down(self) -> None:
        score_bet = make_bet(market_type="MONEYLINE", side="HOME", line_value=None, stat_type=None, prop_type=None)
        repo = FakeRepo([score_bet, make_bet()])
        lines = FakeLines()
        grader = make_grader(repo, FakeStatistics(box_error=NotFoundError("box score not found")), lines)

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 1
        assert len(repo.applied) == 1
        assert repo.applied[0][0] == score_bet.id
        assert repo.applied[0][1] == "WON"
        assert lines.closing_calls == 1  # closing lines fetched for score bets only


def make_final_game() -> Game:
    return Game.model_validate(
        {
            "id": str(GAME_ID),
            "league": "FIFA_WC",
            "status": "FINAL",
            "home_team": {"id": "t-home", "name": "Manchester City", "abbreviation": "MCI"},
            "away_team": {"id": "t-away", "name": "Paris SG", "abbreviation": "PSG"},
            "scheduled_start": "2026-07-10T19:00:00Z",
            "season": 2026,
            "result": {
                "id": str(uuid.uuid4()),
                "home_score": 2,
                "away_score": 1,
                "total_score": 3,
                "margin": 1,
                "overtime": False,
                "completed_at": "2026-07-10T21:00:00Z",
            },
        }
    )


class TestManualPropGrading:
    async def test_manual_grade_prop_happy_path(self) -> None:
        bet = make_bet()
        repo = FakeRepo([bet])
        grader = make_grader(repo, FakeStatistics(box=make_box(), game=make_final_game()))

        refreshed, _ = await grader.grade_manual(bet.id)
        assert refreshed.id == bet.id
        assert len(repo.applied) == 1
        assert repo.applied[0][1] == "WON"
        assert repo.applied[0][2]["actual_stat_value"] == 3.0

    async def test_manual_grade_missing_box_score_422(self) -> None:
        bet = make_bet()
        repo = FakeRepo([bet])
        grader = make_grader(repo, FakeStatistics(box_error=NotFoundError("nope"), game=make_final_game()))

        with pytest.raises(UnprocessableError, match="not available yet"):
            await grader.grade_manual(bet.id)
        assert repo.applied == []

    async def test_manual_grade_unmatched_player_422(self) -> None:
        bet = make_bet(player_external_id="lionel-messi")
        repo = FakeRepo([bet])
        grader = make_grader(repo, FakeStatistics(box=make_box(), game=make_final_game()))

        with pytest.raises(UnprocessableError, match="no box-score player matches"):
            await grader.grade_manual(bet.id)
        assert repo.applied == []

    async def test_manual_grade_team_prop_422(self) -> None:
        bet = make_bet(market_type="TEAM_PROP", stat_type="team_total_goals")
        repo = FakeRepo([bet])
        grader = make_grader(repo, FakeStatistics(box=make_box(), game=make_final_game()))

        with pytest.raises(UnprocessableError, match="not implemented in v1"):
            await grader.grade_manual(bet.id)

    async def test_manual_force_regrades_prop(self) -> None:
        bet = make_bet(status="WON", graded_at=datetime(2026, 7, 11, 0, 0, tzinfo=UTC))
        repo = FakeRepo([bet])
        grader = make_grader(repo, FakeStatistics(box=make_box(), game=make_final_game()))

        await grader.grade_manual(bet.id, force=True)
        assert len(repo.applied) == 1
