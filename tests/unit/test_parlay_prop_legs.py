"""Phase 7 Wave 4: PLAYER_PROP parlay legs.

Placement validation (stat/prop terms, side consistency, the widened
(game, market, player, stat) duplicate key), prop-aware odds matching,
leg grading through the shared resolve_player_prop core, and parent
settlement that waits on box scores.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from bookie_emulator.api.errors import NotFoundError, UnprocessableError
from bookie_emulator.api.schemas import ParlayLegRequest
from bookie_emulator.clients.lines import BestLine, LineSnapshot
from bookie_emulator.clients.statistics import BoxScore, SoccerPlayerBoxScore, TeamBoxScore
from bookie_emulator.config import Settings
from bookie_emulator.core.prop_grading import resolve_player_prop
from bookie_emulator.db.repository import PaperBetRecord, ParlayLegRecord
from bookie_emulator.services.bets import BetService, validate_parlay_legs
from bookie_emulator.services.grader import GraderService

GAME_ID = uuid.uuid4()
OTHER_GAME_ID = uuid.uuid4()


def team_leg(**overrides: Any) -> ParlayLegRequest:
    defaults: dict[str, Any] = {
        "game_id": GAME_ID,
        "market_type": "MONEYLINE",
        "selection": "Manchester City",
        "side": "HOME",
    }
    defaults.update(overrides)
    return ParlayLegRequest(**defaults)


def prop_leg(**overrides: Any) -> ParlayLegRequest:
    defaults: dict[str, Any] = {
        "game_id": GAME_ID,
        "market_type": "PLAYER_PROP",
        "selection": "Erling Haaland Over 2.5 Shots",
        "side": "OVER",
        "player_external_id": "erling-haaland",
        "stat_type": "player_shots",
        "prop_type": "OVER_UNDER",
    }
    defaults.update(overrides)
    return ParlayLegRequest(**defaults)


def goalscorer_leg(**overrides: Any) -> ParlayLegRequest:
    defaults: dict[str, Any] = {
        "selection": "Erling Haaland Anytime Goalscorer",
        "side": "YES",
        "stat_type": "player_goal_scorer_anytime",
        "prop_type": "YES_NO",
    }
    defaults.update(overrides)
    return prop_leg(**defaults)


class TestParlayLegRequestStructure:
    """Structural side rules stay on the schema (400 VALIDATION_ERROR)."""

    def test_prop_leg_accepted_with_stat_and_prop_type(self) -> None:
        leg = prop_leg()
        assert leg.player_external_id == "erling-haaland"
        assert leg.stat_type == "player_shots"
        assert leg.prop_type == "OVER_UNDER"

    def test_yes_side_accepted_on_player_prop(self) -> None:
        assert goalscorer_leg().side == "YES"

    @pytest.mark.parametrize("side", ["YES", "NO"])
    @pytest.mark.parametrize("market_type", ["SPREAD", "TOTAL", "MONEYLINE"])
    def test_yes_no_rejected_on_team_markets(self, side: str, market_type: str) -> None:
        with pytest.raises(ValidationError, match="only valid for prop legs"):
            team_leg(market_type=market_type, side=side)

    def test_draw_still_moneyline_only(self) -> None:
        with pytest.raises(ValidationError, match="only valid for MONEYLINE"):
            team_leg(market_type="SPREAD", side="DRAW")


class TestValidateParlayPropLegs:
    """Business rules raise UnprocessableError (pinned contract: 422)."""

    def test_prop_leg_accepted_alongside_team_leg(self) -> None:
        validate_parlay_legs([team_leg(game_id=OTHER_GAME_ID), prop_leg()])

    def test_yes_no_prop_leg_accepted(self) -> None:
        validate_parlay_legs([team_leg(game_id=OTHER_GAME_ID), goalscorer_leg()])

    def test_missing_stat_type_422(self) -> None:
        with pytest.raises(UnprocessableError, match="requires stat_type and prop_type"):
            validate_parlay_legs([team_leg(game_id=OTHER_GAME_ID), prop_leg(stat_type=None)])

    def test_missing_prop_type_422(self) -> None:
        with pytest.raises(UnprocessableError, match="requires stat_type and prop_type"):
            validate_parlay_legs([team_leg(game_id=OTHER_GAME_ID), prop_leg(prop_type=None)])

    @pytest.mark.parametrize("side", ["YES", "NO"])
    def test_yes_no_side_on_over_under_prop_422(self, side: str) -> None:
        with pytest.raises(UnprocessableError, match="not valid for a OVER_UNDER prop"):
            validate_parlay_legs([team_leg(game_id=OTHER_GAME_ID), prop_leg(side=side)])

    @pytest.mark.parametrize("side", ["OVER", "UNDER"])
    def test_over_under_side_on_yes_no_prop_422(self, side: str) -> None:
        with pytest.raises(UnprocessableError, match="not valid for a YES_NO prop"):
            validate_parlay_legs([team_leg(game_id=OTHER_GAME_ID), goalscorer_leg(side=side)])

    @pytest.mark.parametrize("market_type", ["TEAM_PROP", "GAME_PROP"])
    def test_team_and_game_props_still_rejected(self, market_type: str) -> None:
        legs = [team_leg(game_id=OTHER_GAME_ID), prop_leg(market_type=market_type)]
        with pytest.raises(UnprocessableError, match="only SPREAD, TOTAL, MONEYLINE, and PLAYER_PROP"):
            validate_parlay_legs(legs)

    def test_same_player_same_stat_twice_rejected(self) -> None:
        with pytest.raises(UnprocessableError, match="duplicate or opposite-side"):
            validate_parlay_legs([prop_leg(), prop_leg()])

    def test_same_player_same_stat_opposite_sides_rejected(self) -> None:
        legs = [prop_leg(), prop_leg(side="UNDER", selection="Erling Haaland Under 2.5 Shots")]
        with pytest.raises(UnprocessableError, match="duplicate or opposite-side"):
            validate_parlay_legs(legs)

    def test_two_players_props_on_one_game_coexist(self) -> None:
        validate_parlay_legs(
            [
                prop_leg(),
                prop_leg(player_external_id="kylian-mbappe", selection="Kylian Mbappe Over 3.5 Shots"),
            ]
        )

    def test_same_player_different_stats_coexist(self) -> None:
        validate_parlay_legs([prop_leg(), goalscorer_leg()])

    def test_team_leg_and_prop_leg_on_one_game_coexist(self) -> None:
        validate_parlay_legs([team_leg(), prop_leg()])

    def test_team_market_duplicate_rule_unchanged(self) -> None:
        with pytest.raises(UnprocessableError, match="duplicate or opposite-side"):
            validate_parlay_legs([team_leg(), team_leg(side="AWAY", selection="Paris SG")])


def best_line(**overrides: Any) -> BestLine:
    defaults: dict[str, Any] = {
        "market_type": "PLAYER_PROP",
        "selection": "Erling Haaland Over 2.5 Shots",
        "side": "OVER",
        "line_value": 2.5,
        "best_odds_american": -115,
        "best_odds_decimal": 1.87,
        "sportsbook_key": "draftkings",
        "timestamp": "2026-07-19T12:00:00Z",
    }
    defaults.update(overrides)
    return BestLine(**defaults)


def snapshot(**overrides: Any) -> LineSnapshot:
    defaults: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "game_id": "odds-api-game-1",
        "sportsbook_key": "draftkings",
        "market_type": "PLAYER_PROP",
        "selection": "Erling Haaland Over 2.5 Shots",
        "side": "OVER",
        "line_value": 2.5,
        "odds_american": -110,
        "odds_decimal": 1.909,
        "timestamp": "2026-07-19T12:00:00Z",
    }
    defaults.update(overrides)
    return LineSnapshot(**defaults)


def make_bet_service() -> BetService:
    # odds matching is pure; the service dependencies are unused
    return BetService(None, None, None, None, Settings())  # type: ignore[arg-type]


class TestPropLegOddsMatching:
    """Prop lines are per-player: selection matching disambiguates where a
    side-only match (any OVER) could capture another player's price."""

    def test_best_line_matches_on_selection_not_side(self) -> None:
        service = make_bet_service()
        lines = [
            best_line(selection="Kylian Mbappe Over 3.5 Shots", line_value=3.5, best_odds_decimal=2.1),
            best_line(),
        ]
        odds = service._pick_best_line(prop_leg(), lines)
        assert odds["odds_decimal"] == pytest.approx(1.87)
        assert odds["line_value"] == pytest.approx(2.5)

    def test_best_line_side_only_match_is_not_enough_for_props(self) -> None:
        service = make_bet_service()
        lines = [best_line(selection="Kylian Mbappe Over 3.5 Shots", line_value=3.5)]
        with pytest.raises(UnprocessableError, match="No PLAYER_PROP OVER line found"):
            service._pick_best_line(prop_leg(), lines)

    def test_team_market_side_matching_unchanged(self) -> None:
        service = make_bet_service()
        lines = [best_line(market_type="MONEYLINE", selection="Manchester City", side="HOME", line_value=None)]
        odds = service._pick_best_line(team_leg(), lines)
        assert odds["odds_decimal"] == pytest.approx(1.87)

    def test_pinned_line_requires_selection_match_for_props(self) -> None:
        service = make_bet_service()
        request = prop_leg(sportsbook_key="draftkings")
        wrong_player = [snapshot(selection="Kylian Mbappe Over 3.5 Shots", line_value=3.5)]
        with pytest.raises(UnprocessableError, match="No PLAYER_PROP OVER line found at draftkings"):
            service._pick_pinned_line(request, wrong_player)
        odds = service._pick_pinned_line(request, [*wrong_player, snapshot()])
        assert odds["line_value"] == pytest.approx(2.5)


def make_box(shots: int = 3, goals: int = 1) -> BoxScore:
    return BoxScore(
        game_id=str(GAME_ID),
        sport="SOCCER",
        status="FINAL",
        home_team=TeamBoxScore(
            id="t-home",
            abbreviation="MCI",
            score=2,
            soccer_players=[
                SoccerPlayerBoxScore(
                    player_id="stats-uuid-1",
                    player_name="Erling Haaland",
                    position="F",
                    minutes=90,
                    goals=goals,
                    shots=shots,
                    shots_on_target=2,
                )
            ],
        ),
        away_team=TeamBoxScore(id="t-away", abbreviation="PSG", score=1),
    )


class TestResolvePlayerProp:
    """The shared resolution core used by single bets and parlay legs."""

    def test_resolves_over_under_won(self) -> None:
        resolved = resolve_player_prop(make_box(), "erling-haaland", "player_shots", "OVER_UNDER", "OVER", 2.5)
        assert resolved == ("WON", "Erling Haaland landed 3 shots, over 2.5", 3.0)

    def test_resolves_yes_no_from_goals(self) -> None:
        resolved = resolve_player_prop(
            make_box(), "erling-haaland", "player_goal_scorer_anytime", "YES_NO", "YES", None
        )
        assert not isinstance(resolved, str)
        assert resolved[0] == "WON"
        assert resolved[2] == 1.0

    def test_unmatched_player_returns_reason(self) -> None:
        resolved = resolve_player_prop(make_box(), "lionel-messi", "player_shots", "OVER_UNDER", "OVER", 2.5)
        assert isinstance(resolved, str)
        assert "no box-score player matches" in resolved

    def test_missing_terms_return_reason(self) -> None:
        resolved = resolve_player_prop(make_box(), "erling-haaland", None, "OVER_UNDER", "OVER", 2.5)
        assert isinstance(resolved, str)
        assert "missing" in resolved

    def test_prop_type_inferred_from_side_when_absent(self) -> None:
        resolved = resolve_player_prop(make_box(), "erling-haaland", "player_goal_scorer_anytime", None, "YES", None)
        assert not isinstance(resolved, str)
        assert resolved[0] == "WON"


def make_parent(**overrides: Any) -> PaperBetRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "game_id": None,
        "game_external_id": "parlay:odds-api-game-1+1",
        "league": "FIFA_WC",
        "market_type": "MONEYLINE",
        "selection": "2-leg parlay: Manchester City + Erling Haaland Anytime Goalscorer",
        "side": None,
        "line_value": None,
        "sportsbook_id": None,
        "sportsbook_key": "mixed",
        "odds_american": 500,
        "odds_decimal": 6.0,
        "stake": 1.0,
        "predicted_probability": 0.2,
        "edge_at_placement": 0.03,
        "kelly_fraction": 0.08,
        "reasoning": None,
        "prediction_id": None,
        "edge_id": None,
        "idempotency_key": str(uuid.uuid4()),
        "game_start_at": datetime(2026, 7, 19, 19, 0, tzinfo=UTC),
        "status": "OPEN",
        "placed_at": datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        "graded_at": None,
        "is_parlay": True,
    }
    defaults.update(overrides)
    return PaperBetRecord(**defaults)


def make_leg(parent_id: uuid.UUID, leg_index: int, **overrides: Any) -> ParlayLegRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "bet_id": parent_id,
        "leg_index": leg_index,
        "game_id": GAME_ID,
        "game_external_id": "odds-api-game-1",
        "league": "FIFA_WC",
        "market_type": "MONEYLINE",
        "selection": "Manchester City",
        "side": "HOME",
        "line_value": None,
        "odds_american": 150,
        "odds_decimal": 2.5,
        "leg_status": "OPEN",
    }
    defaults.update(overrides)
    return ParlayLegRecord(**defaults)


def make_prop_leg_record(parent_id: uuid.UUID, leg_index: int, **overrides: Any) -> ParlayLegRecord:
    defaults: dict[str, Any] = {
        "market_type": "PLAYER_PROP",
        "selection": "Erling Haaland Anytime Goalscorer",
        "side": "YES",
        "player_external_id": "erling-haaland",
        "stat_type": "player_goal_scorer_anytime",
        "prop_type": "YES_NO",
        "odds_american": 140,
        "odds_decimal": 2.4,
    }
    defaults.update(overrides)
    return make_leg(parent_id, leg_index, **defaults)


class FakeParlayRepo:
    """In-memory legs with claim-and-grade semantics for grader routing tests."""

    def __init__(self, parent: PaperBetRecord, legs: list[ParlayLegRecord]) -> None:
        self.parent = parent
        self.legs = {leg.id: leg for leg in legs}
        self.applied: list[tuple[uuid.UUID, str, dict[str, Any]]] = []

    async def open_bets_for_game(self, game_id: uuid.UUID) -> list[PaperBetRecord]:
        return []

    async def open_parlay_legs_for_game(self, game_id: uuid.UUID) -> list[ParlayLegRecord]:
        return [leg for leg in self.legs.values() if leg.game_id == game_id and leg.leg_status == "OPEN"]

    async def grade_leg(self, leg_id: uuid.UUID, status: str) -> bool:
        leg = self.legs[leg_id]
        if leg.leg_status != "OPEN":
            return False
        self.legs[leg_id] = replace(leg, leg_status=status)
        return True

    async def open_leg_count(self, bet_id: uuid.UUID) -> int:
        return sum(1 for leg in self.legs.values() if leg.bet_id == bet_id and leg.leg_status == "OPEN")

    async def legs_for_bet(self, bet_id: uuid.UUID) -> list[ParlayLegRecord]:
        legs = [leg for leg in self.legs.values() if leg.bet_id == bet_id]
        return sorted(legs, key=lambda leg: leg.leg_index)

    async def get_with_grade(self, bet_id: uuid.UUID) -> tuple[PaperBetRecord, None] | None:
        if bet_id == self.parent.id:
            return self.parent, None
        return None

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


class FakeStatistics:
    def __init__(self, box: BoxScore | None = None, box_error: Exception | None = None) -> None:
        self.box = box
        self.box_error = box_error
        self.box_calls = 0

    async def get_box_score(self, game_id: str) -> BoxScore:
        self.box_calls += 1
        if self.box_error is not None:
            raise self.box_error
        assert self.box is not None
        return self.box


class FakeLines:
    async def closing_lines(self, game_external_id: str) -> list[Any]:
        return []


def make_grader(repo: FakeParlayRepo, statistics: FakeStatistics) -> GraderService:
    return GraderService(statistics, FakeLines(), repo, Settings())  # type: ignore[arg-type]


class TestParlayPropLegGrading:
    async def test_prop_leg_wins_and_parent_settles(self) -> None:
        parent = make_parent()
        team = make_leg(parent.id, 0, game_id=OTHER_GAME_ID, leg_status="WON")
        prop = make_prop_leg_record(parent.id, 1)
        repo = FakeParlayRepo(parent, [team, prop])
        grader = make_grader(repo, FakeStatistics(box=make_box()))

        settled = await grader.grade_game(str(GAME_ID), home_score=2, away_score=1)
        assert settled == 1
        assert repo.legs[prop.id].leg_status == "WON"
        assert len(repo.applied) == 1
        bet_id, status, grade_values = repo.applied[0]
        assert bet_id == parent.id
        assert status == "WON"
        # both legs WON: repriced decimal 2.5 * 2.4 = 6.0 -> profit 5.0
        assert grade_values["profit_loss"] == pytest.approx(5.0)

    async def test_prop_leg_loses_and_parent_loses(self) -> None:
        parent = make_parent()
        team = make_leg(parent.id, 0, game_id=OTHER_GAME_ID, leg_status="WON")
        prop = make_prop_leg_record(parent.id, 1)
        repo = FakeParlayRepo(parent, [team, prop])
        grader = make_grader(repo, FakeStatistics(box=make_box(goals=0)))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 1
        assert repo.legs[prop.id].leg_status == "LOST"
        assert repo.applied[0][1] == "LOST"

    async def test_unmatched_player_leaves_leg_open_and_parent_pending(self) -> None:
        parent = make_parent()
        team = make_leg(parent.id, 0, game_id=OTHER_GAME_ID, leg_status="WON")
        prop = make_prop_leg_record(parent.id, 1, player_external_id="lionel-messi")
        repo = FakeParlayRepo(parent, [team, prop])
        grader = make_grader(repo, FakeStatistics(box=make_box()))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 0
        assert repo.legs[prop.id].leg_status == "OPEN"
        assert repo.applied == []

    async def test_missing_box_score_grades_team_leg_but_parent_waits(self) -> None:
        """A mixed parlay on one game: the score settles the team leg while
        the prop leg (and therefore the parent) waits on the box score."""
        parent = make_parent()
        team = make_leg(parent.id, 0)
        prop = make_prop_leg_record(parent.id, 1)
        repo = FakeParlayRepo(parent, [team, prop])
        grader = make_grader(repo, FakeStatistics(box_error=NotFoundError("box score not found")))
        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1, home_team="MCI") == 0
        assert repo.legs[team.id].leg_status == "WON"
        assert repo.legs[prop.id].leg_status == "OPEN"
        assert repo.applied == []

        # the box score lands; a later sweep settles the prop leg and parent
        settled_grader = make_grader(repo, FakeStatistics(box=make_box()))
        assert await settled_grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 1
        assert repo.legs[prop.id].leg_status == "WON"
        assert repo.applied[0][1] == "WON"

    async def test_pushed_team_leg_reprices_over_won_prop_leg(self) -> None:
        parent = make_parent()
        team = make_leg(parent.id, 0, game_id=OTHER_GAME_ID, leg_status="PUSH")
        prop = make_prop_leg_record(parent.id, 1)
        repo = FakeParlayRepo(parent, [team, prop])
        grader = make_grader(repo, FakeStatistics(box=make_box()))

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 1
        _, status, grade_values = repo.applied[0]
        assert status == "WON"
        # the pushed team leg drops out: payout re-prices to the prop leg
        assert grade_values["profit_loss"] == pytest.approx(1.0 * (2.4 - 1.0))
        assert "re-priced 2.40" in grade_values["actual_result"]

    async def test_manual_parent_grade_hints_at_box_score_wait(self) -> None:
        parent = make_parent()
        team = make_leg(parent.id, 0, game_id=OTHER_GAME_ID, leg_status="WON")
        prop = make_prop_leg_record(parent.id, 1)
        repo = FakeParlayRepo(parent, [team, prop])
        grader = make_grader(repo, FakeStatistics())

        with pytest.raises(UnprocessableError, match="box scores") as exc_info:
            await grader.grade_manual(parent.id)
        assert "open legs" in str(exc_info.value)

    async def test_manual_parent_grade_message_has_no_hint_without_prop_legs(self) -> None:
        parent = make_parent()
        team_a = make_leg(parent.id, 0, game_id=OTHER_GAME_ID, leg_status="WON")
        team_b = make_leg(parent.id, 1)
        repo = FakeParlayRepo(parent, [team_a, team_b])
        grader = make_grader(repo, FakeStatistics())

        with pytest.raises(UnprocessableError, match="open legs") as exc_info:
            await grader.grade_manual(parent.id)
        assert "box scores" not in str(exc_info.value)

    async def test_box_score_fetched_once_for_props_and_legs(self) -> None:
        parent = make_parent()
        prop = make_prop_leg_record(parent.id, 0)
        other_prop = make_prop_leg_record(
            parent.id,
            1,
            selection="Erling Haaland Over 2.5 Shots",
            side="OVER",
            line_value=2.5,
            stat_type="player_shots",
            prop_type="OVER_UNDER",
        )
        repo = FakeParlayRepo(parent, [prop, other_prop])
        statistics = FakeStatistics(box=make_box())
        grader = make_grader(repo, statistics)

        assert await grader.grade_game(str(GAME_ID), home_score=2, away_score=1) == 1
        assert statistics.box_calls == 1
        assert repo.legs[prop.id].leg_status == "WON"
        assert repo.legs[other_prop.id].leg_status == "WON"
