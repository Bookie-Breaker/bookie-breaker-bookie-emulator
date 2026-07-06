"""League-aware settlement: sport mapping, three-way detection, score selection."""

import pytest

from bookie_emulator.clients.statistics import GameResult
from bookie_emulator.core.settlement import SPORT_BY_LEAGUE, is_three_way_moneyline_league, settlement_scores


class TestSportByLeague:
    @pytest.mark.parametrize(
        ("league", "sport"),
        [
            ("NFL", "FOOTBALL"),
            ("NCAA_FB", "FOOTBALL"),
            ("NBA", "BASKETBALL"),
            ("NCAA_BB", "BASKETBALL"),
            ("MLB", "BASEBALL"),
            ("NCAA_BSB", "BASEBALL"),
            ("FIFA_WC", "SOCCER"),
            ("EPL", "SOCCER"),
            ("NHL", "HOCKEY"),
            ("NCAA_HKY", "HOCKEY"),
        ],
    )
    def test_mapping(self, league: str, sport: str) -> None:
        assert SPORT_BY_LEAGUE[league] == sport

    def test_covers_every_league_enum_value(self) -> None:
        from bookie_emulator.db.tables import _ENUM_VALUES

        assert set(SPORT_BY_LEAGUE) == set(_ENUM_VALUES["league_enum"])


class TestIsThreeWayMoneylineLeague:
    def test_soccer_leagues_are_three_way(self) -> None:
        assert is_three_way_moneyline_league("FIFA_WC")
        assert is_three_way_moneyline_league("EPL")

    def test_other_leagues_are_two_way(self) -> None:
        for league in ("NBA", "NFL", "MLB", "NCAA_BB", "NCAA_FB", "NCAA_BSB", "NHL", "NCAA_HKY"):
            assert not is_three_way_moneyline_league(league)

    def test_unknown_league_is_two_way(self) -> None:
        assert not is_three_way_moneyline_league("XFL")


class TestSettlementScores:
    def test_soccer_uses_regulation_scores_when_present(self) -> None:
        payload = {"home_score": 3, "away_score": 2, "regulation_home_score": 2, "regulation_away_score": 2}
        assert settlement_scores(payload, "FIFA_WC") == (2, 2)
        assert settlement_scores(payload, "EPL") == (2, 2)

    def test_soccer_falls_back_to_final_when_regulation_absent(self) -> None:
        assert settlement_scores({"home_score": 2, "away_score": 2}, "FIFA_WC") == (2, 2)

    def test_soccer_falls_back_when_regulation_fields_are_none(self) -> None:
        payload = {"home_score": 1, "away_score": 0, "regulation_home_score": None, "regulation_away_score": None}
        assert settlement_scores(payload, "FIFA_WC") == (1, 0)

    def test_soccer_partial_regulation_fields_fall_back_to_final(self) -> None:
        payload = {"home_score": 3, "away_score": 2, "regulation_home_score": 2}
        assert settlement_scores(payload, "FIFA_WC") == (3, 2)

    def test_non_soccer_ignores_regulation_fields(self) -> None:
        payload = {"home_score": 112, "away_score": 104, "regulation_home_score": 100, "regulation_away_score": 100}
        for league in ("NBA", "NFL", "MLB", "NHL", "NCAA_HKY"):
            assert settlement_scores(payload, league) == (112, 104)

    def test_unknown_league_uses_final_scores(self) -> None:
        payload = {"home_score": 5, "away_score": 4, "regulation_home_score": 4, "regulation_away_score": 4}
        assert settlement_scores(payload, "XFL") == (5, 4)

    def test_accepts_game_result_objects(self) -> None:
        result = GameResult(id="r1", home_score=3, away_score=2, regulation_home_score=2, regulation_away_score=2)
        assert settlement_scores(result, "FIFA_WC") == (2, 2)
        assert settlement_scores(result, "NHL") == (3, 2)

    def test_game_result_without_regulation_defaults_to_final(self) -> None:
        result = GameResult(id="r1", home_score=1, away_score=1)
        assert settlement_scores(result, "EPL") == (1, 1)
