"""Phase 7 Wave 3 pure prop grading: slugs, extractors, and the grade matrix."""

import pytest

from bookie_emulator.clients.statistics import (
    BaseballPlayerBoxScore,
    BasketballPlayerBoxScore,
    BoxScore,
    SoccerPlayerBoxScore,
    TeamBoxScore,
)
from bookie_emulator.core.prop_grading import (
    STAT_EXTRACTORS,
    find_player_line,
    grade_player_prop,
    resolve_player_prop,
    slugify_player_name,
)


class TestSlugifyPlayerName:
    @pytest.mark.parametrize(
        ("name", "slug"),
        [
            ("Kylian Mbappé", "kylian-mbappe"),
            ("Erling Håland", "erling-haland"),
            ("N'Golo Kanté", "n-golo-kante"),
            ("Shai Gilgeous-Alexander", "shai-gilgeous-alexander"),
            ("Michael Porter Jr.", "michael-porter-jr"),
            ("  Luka   Dončić  ", "luka-doncic"),
            ("O'Neal", "o-neal"),
            ("ÁÉÍÓÚ üñç", "aeiou-unc"),
        ],
    )
    def test_folds_diacritics_and_punctuation(self, name: str, slug: str) -> None:
        assert slugify_player_name(name) == slug

    def test_idempotent_on_existing_slugs(self) -> None:
        assert slugify_player_name("kylian-mbappe") == "kylian-mbappe"


def soccer_player(**overrides: object) -> SoccerPlayerBoxScore:
    defaults: dict[str, object] = {
        "player_id": "p-1",
        "player_name": "Erling Haaland",
        "position": "F",
        "minutes": 90,
        "goals": 1,
        "assists": 2,
        "shots": 3,
        "shots_on_target": 2,
        "yellow_cards": 0,
        "red_cards": 0,
    }
    defaults.update(overrides)
    return SoccerPlayerBoxScore.model_validate(defaults)


def basketball_player(**overrides: object) -> BasketballPlayerBoxScore:
    defaults: dict[str, object] = {
        "player_id": "p-2",
        "player_name": "Luka Doncic",
        "position": "G",
        "minutes": 38,
        "points": 31,
        "rebounds": 9,
        "assists": 11,
        "three_pointers_made": 4,
    }
    defaults.update(overrides)
    return BasketballPlayerBoxScore.model_validate(defaults)


def baseball_player(**overrides: object) -> BaseballPlayerBoxScore:
    defaults: dict[str, object] = {
        "player_id": "p-3",
        "player_name": "Shohei Ohtani",
        "position": "DH",
        "at_bats": 4,
        "hits": 2,
        "total_bases": 5,
        "home_runs": 1,
        "strikeouts_pitching": 9,
    }
    defaults.update(overrides)
    return BaseballPlayerBoxScore.model_validate(defaults)


class TestStatExtractors:
    def test_soccer_fields(self) -> None:
        player = soccer_player()
        assert STAT_EXTRACTORS["player_goal_scorer_anytime"](player) == 1.0
        assert STAT_EXTRACTORS["player_shots"](player) == 3.0
        assert STAT_EXTRACTORS["player_shots_on_target"](player) == 2.0

    def test_basketball_fields(self) -> None:
        player = basketball_player()
        assert STAT_EXTRACTORS["player_points"](player) == 31.0
        assert STAT_EXTRACTORS["player_rebounds"](player) == 9.0
        assert STAT_EXTRACTORS["player_assists"](player) == 11.0
        assert STAT_EXTRACTORS["player_threes"](player) == 4.0

    def test_pra_sums_points_rebounds_assists(self) -> None:
        assert STAT_EXTRACTORS["player_points_rebounds_assists"](basketball_player()) == 51.0

    def test_baseball_fields(self) -> None:
        player = baseball_player()
        assert STAT_EXTRACTORS["batter_hits"](player) == 2.0
        assert STAT_EXTRACTORS["batter_total_bases"](player) == 5.0
        assert STAT_EXTRACTORS["batter_home_runs"](player) == 1.0
        assert STAT_EXTRACTORS["pitcher_strikeouts"](player) == 9.0

    def test_extractors_accept_plain_dicts(self) -> None:
        assert STAT_EXTRACTORS["player_shots"]({"shots": 4}) == 4.0
        assert STAT_EXTRACTORS["player_points_rebounds_assists"]({"points": 10, "rebounds": 5, "assists": 5}) == 20.0

    def test_wrong_sport_field_raises(self) -> None:
        with pytest.raises(AttributeError):
            STAT_EXTRACTORS["player_points"](soccer_player())


class TestGradePlayerProp:
    def test_over_wins_and_description(self) -> None:
        status, description = grade_player_prop("player_shots", "OVER_UNDER", "OVER", 2.5, 3.0, "Haaland")
        assert status == "WON"
        assert description == "Haaland landed 3 shots, over 2.5"

    def test_over_loses(self) -> None:
        status, description = grade_player_prop("player_shots", "OVER_UNDER", "OVER", 3.5, 3.0, "Haaland")
        assert status == "LOST"
        assert description == "Haaland landed 3 shots, under 3.5"

    def test_under_wins(self) -> None:
        assert grade_player_prop("player_points", "OVER_UNDER", "UNDER", 31.5, 31.0)[0] == "WON"

    def test_under_loses(self) -> None:
        assert grade_player_prop("player_points", "OVER_UNDER", "UNDER", 30.5, 31.0)[0] == "LOST"

    def test_integer_line_pushes_on_exact_equality(self) -> None:
        for side in ("OVER", "UNDER"):
            status, description = grade_player_prop("player_rebounds", "OVER_UNDER", side, 9.0, 9.0, "Doncic")
            assert status == "PUSH"
            assert description == "Doncic landed 9 rebounds, push on 9"

    def test_half_lines_never_push(self) -> None:
        assert grade_player_prop("player_rebounds", "OVER_UNDER", "OVER", 8.5, 9.0)[0] == "WON"
        assert grade_player_prop("player_rebounds", "OVER_UNDER", "UNDER", 9.5, 9.0)[0] == "WON"

    def test_yes_wins_on_any_positive_count(self) -> None:
        status, description = grade_player_prop("player_goal_scorer_anytime", "YES_NO", "YES", None, 1.0, "Haaland")
        assert status == "WON"
        assert description == "Haaland landed 1 goals, YES settles"

    def test_yes_loses_on_zero(self) -> None:
        assert grade_player_prop("player_goal_scorer_anytime", "YES_NO", "YES", None, 0.0)[0] == "LOST"

    def test_no_wins_on_zero(self) -> None:
        status, description = grade_player_prop("player_goal_scorer_anytime", "YES_NO", "NO", None, 0.0, "Kante")
        assert status == "WON"
        assert description == "Kante landed 0 goals, NO settles"

    def test_no_loses_on_positive(self) -> None:
        assert grade_player_prop("player_goal_scorer_anytime", "YES_NO", "NO", None, 2.0)[0] == "LOST"

    def test_zero_minutes_yes_no_grades_normally(self) -> None:
        """DNP-void semantics are deferred: a matched player with 0 minutes
        grades on the box score as-is, so NO wins and YES loses."""
        benched = soccer_player(minutes=0, goals=0, shots=0)
        actual = STAT_EXTRACTORS["player_goal_scorer_anytime"](benched)
        assert grade_player_prop("player_goal_scorer_anytime", "YES_NO", "NO", None, actual)[0] == "WON"
        assert grade_player_prop("player_goal_scorer_anytime", "YES_NO", "YES", None, actual)[0] == "LOST"

    def test_unknown_prop_type_raises(self) -> None:
        with pytest.raises(ValueError, match="prop type"):
            grade_player_prop("player_shots", "SPREADISH", "OVER", 2.5, 3.0)

    def test_mismatched_side_raises(self) -> None:
        with pytest.raises(ValueError, match="YES_NO"):
            grade_player_prop("player_goal_scorer_anytime", "YES_NO", "OVER", None, 1.0)
        with pytest.raises(ValueError, match="OVER_UNDER"):
            grade_player_prop("player_shots", "OVER_UNDER", "YES", 2.5, 3.0)

    def test_default_player_name(self) -> None:
        _, description = grade_player_prop("player_shots", "OVER_UNDER", "OVER", 2.5, 3.0)
        assert description.startswith("Player landed")


def soccer_box(**overrides: object) -> BoxScore:
    defaults: dict[str, object] = {
        "game_id": "game-1",
        "sport": "SOCCER",
        "status": "FINAL",
        "home_team": TeamBoxScore(
            id="t-home",
            abbreviation="MCI",
            score=3,
            soccer_players=[soccer_player()],
        ),
        "away_team": TeamBoxScore(
            id="t-away",
            abbreviation="PSG",
            score=1,
            soccer_players=[soccer_player(player_id="p-9", player_name="Kylian Mbappé", goals=0, shots=2)],
        ),
    }
    defaults.update(overrides)
    return BoxScore.model_validate(defaults)


class TestFindPlayerLine:
    def test_matches_home_team_by_slug(self) -> None:
        player = find_player_line(soccer_box(), "erling-haaland")
        assert player is not None
        assert player.player_id == "p-1"

    def test_matches_away_team_with_diacritics_folded(self) -> None:
        player = find_player_line(soccer_box(), "kylian-mbappe")
        assert player is not None
        assert player.player_id == "p-9"

    def test_no_match_returns_none(self) -> None:
        assert find_player_line(soccer_box(), "lionel-messi") is None

    def test_basketball_uses_players_array(self) -> None:
        box = BoxScore(
            game_id="game-2",
            sport="BASKETBALL",
            status="FINAL",
            home_team=TeamBoxScore(id="t1", abbreviation="DAL", score=110, players=[basketball_player()]),
            away_team=TeamBoxScore(id="t2", abbreviation="BOS", score=104),
        )
        player = find_player_line(box, "luka-doncic")
        assert player is not None
        assert player.player_id == "p-2"

    def test_baseball_uses_baseball_players_array(self) -> None:
        box = BoxScore(
            game_id="game-3",
            sport="BASEBALL",
            status="FINAL",
            home_team=TeamBoxScore(id="t1", abbreviation="LAD", score=5, baseball_players=[baseball_player()]),
            away_team=TeamBoxScore(id="t2", abbreviation="SD", score=2),
        )
        player = find_player_line(box, "shohei-ohtani")
        assert player is not None
        assert player.player_id == "p-3"

    def test_unknown_sport_matches_nothing(self) -> None:
        box = soccer_box(sport="CRICKET")
        assert find_player_line(box, "erling-haaland") is None


class TestResolvePlayerProp:
    def test_side_prop_type_mismatch_returns_reason_string(self) -> None:
        # grade_player_prop's ValueError surfaces as a stay-OPEN reason, not a raise
        resolved = resolve_player_prop(soccer_box(), "erling-haaland", "player_shots", "OVER_UNDER", "YES", 2.5)
        assert resolved == "Cannot grade side YES on an OVER_UNDER prop"

    def test_matched_player_resolves_to_grade_tuple(self) -> None:
        resolved = resolve_player_prop(soccer_box(), "erling-haaland", "player_shots", "OVER_UNDER", "OVER", 2.5)
        assert not isinstance(resolved, str)
        status, description, actual = resolved
        assert status == "WON"
        assert actual == 3.0
        assert "over 2.5" in description
