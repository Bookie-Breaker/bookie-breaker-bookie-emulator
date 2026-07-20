"""Pure player-prop grading: box-score stats + bet terms -> WON/LOST/PUSH.

Three pinned pieces (Phase 7 Wave 3):

- ``slugify_player_name``: the Odds API player identity. lines-service prop
  lines carry a NAME-derived slug (lowercase, NFKD-folded, hyphenated, e.g.
  "kylian-mbappe"); box scores carry stats-service UUIDs plus display names.
  Matching slugifies the box-score ``player_name`` with the identical
  function and requires an exact slug match.
- ``STAT_EXTRACTORS``: the canonical stat_type -> box-score field mapping
  (ADR-029 vocabulary). One place to pin which field settles which market.
- ``grade_player_prop``: OVER_UNDER and YES_NO settlement with descriptions.

DNP semantics (deferred): real books void player props when the player did
not play. v1 grades on the box score as-is -- a matched player with zero
minutes/at_bats grades normally (an OVER loses, a NO wins). Unmatched
players never grade here: the caller keeps the bet OPEN (box scores may lag
or names may mismatch), and a manual force-grade can VOID later.
"""

import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

from bookie_emulator.clients.statistics import BoxScore, PlayerBoxScoreLine, TeamBoxScore
from bookie_emulator.core.grading import GradeStatus

_SLUG_RUN = re.compile(r"[^a-z0-9]+")


def slugify_player_name(name: str) -> str:
    """Odds API-style player slug: NFKD fold to ASCII, lowercase, collapse
    every non-alphanumeric run to a single hyphen ("Kylian Mbappé" ->
    "kylian-mbappe"). Must stay byte-identical to the lines-service slug."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return _SLUG_RUN.sub("-", folded.lower()).strip("-")


def _stat(line: Any, name: str) -> float:
    value = line[name] if isinstance(line, Mapping) else getattr(line, name)
    return float(value)


def _field(name: str) -> Callable[[Any], float]:
    return lambda line: _stat(line, name)


def _pra(line: Any) -> float:
    return _stat(line, "points") + _stat(line, "rebounds") + _stat(line, "assists")


# Canonical stat_type -> box-score field mapping. Extractors accept either a
# player box-score model or a plain dict with the same field names.
STAT_EXTRACTORS: dict[str, Callable[[Any], float]] = {
    # soccer
    "player_goal_scorer_anytime": _field("goals"),
    "player_shots": _field("shots"),
    "player_shots_on_target": _field("shots_on_target"),
    # basketball
    "player_points": _field("points"),
    "player_rebounds": _field("rebounds"),
    "player_assists": _field("assists"),
    "player_threes": _field("three_pointers_made"),
    "player_points_rebounds_assists": _pra,
    # baseball
    "batter_hits": _field("hits"),
    "batter_total_bases": _field("total_bases"),
    "batter_home_runs": _field("home_runs"),
    "pitcher_strikeouts": _field("strikeouts_pitching"),
}

_STAT_NOUNS: dict[str, str] = {
    "player_goal_scorer_anytime": "goals",
    "player_shots": "shots",
    "player_shots_on_target": "shots on target",
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "three-pointers",
    "player_points_rebounds_assists": "points+rebounds+assists",
    "batter_hits": "hits",
    "batter_total_bases": "total bases",
    "batter_home_runs": "home runs",
    "pitcher_strikeouts": "strikeouts",
}


def stat_noun(stat_type: str) -> str:
    return _STAT_NOUNS.get(stat_type, stat_type)


# which TeamBoxScore player array carries each sport's lines
_PLAYER_ARRAYS_BY_SPORT: dict[str, str] = {
    "SOCCER": "soccer_players",
    "BASKETBALL": "players",
    "BASEBALL": "baseball_players",
}


def team_players(team: TeamBoxScore, sport: str) -> list[PlayerBoxScoreLine]:
    attr = _PLAYER_ARRAYS_BY_SPORT.get(sport)
    if attr is None:
        return []
    players: list[PlayerBoxScoreLine] = getattr(team, attr)
    return players


def find_player_line(box: BoxScore, player_slug: str) -> PlayerBoxScoreLine | None:
    """Find one player's box-score line across both teams by exact slug match.

    ``player_slug`` is the bet's ``player_external_id`` (the Odds API name
    slug); each box-score ``player_name`` is slugified identically. None
    means no match -- the caller must keep the bet OPEN, never VOID it.
    """
    for team in (box.home_team, box.away_team):
        for player in team_players(team, box.sport):
            if slugify_player_name(player.player_name) == player_slug:
                return player
    return None


def resolve_player_prop(
    box: BoxScore,
    player_slug: str | None,
    stat_type: str | None,
    prop_type: str | None,
    side: str | None,
    line_value: float | None,
) -> tuple[GradeStatus, str, float] | str:
    """Resolve one player-prop selection against a box score.

    The shared resolution core for single PLAYER_PROP bets and PLAYER_PROP
    parlay legs (Phase 7 Wave 4): callers pass the prop terms rather than a
    record type. Returns (status, description, actual_stat_value), or a
    reason string when the selection cannot be graded -- the caller decides
    whether that stays OPEN with a log (event/poller paths) or raises a 422
    (manual grading).

    prop_type may be None on rows predating Wave 0 placement validation; it
    is inferred from the side vocabulary.
    """
    if side is None or not player_slug or not stat_type:
        return "bet is missing side/player_external_id/stat_type"
    extractor = STAT_EXTRACTORS.get(stat_type)
    if extractor is None:
        return f"unknown stat_type {stat_type!r}"
    player = find_player_line(box, player_slug)
    if player is None:
        return f"no box-score player matches slug {player_slug!r} for game {box.game_id}"
    try:
        actual = extractor(player)
    except (AttributeError, KeyError, TypeError):
        return f"stat_type {stat_type!r} does not apply to a {box.sport} box score"
    resolved_prop_type = prop_type or ("YES_NO" if side in ("YES", "NO") else "OVER_UNDER")
    try:
        status, description = grade_player_prop(
            stat_type, resolved_prop_type, side, line_value, actual, player_name=player.player_name
        )
    except ValueError as exc:
        return str(exc)
    return status, description, actual


def grade_player_prop(
    stat_type: str,
    prop_type: str,
    side: str,
    line_value: float | None,
    actual: float,
    player_name: str | None = None,
) -> tuple[GradeStatus, str]:
    """Grade a settled player prop and describe the outcome.

    Returns (status, description), e.g. ("WON", "Haaland landed 3 shots,
    over 2.5").

    - OVER_UNDER: actual > line wins OVER, actual < line wins UNDER, exact
      equality is a PUSH (half-lines therefore never push; integer lines can).
    - YES_NO: YES wins iff actual > 0; NO wins iff actual == 0. No push path.
    """
    who = player_name or "Player"
    noun = stat_noun(stat_type)
    if prop_type == "YES_NO":
        if side not in ("YES", "NO"):
            raise ValueError(f"Cannot grade side {side} on a YES_NO prop")
        happened = actual > 0
        status: GradeStatus = "WON" if (side == "YES") == happened else "LOST"
        return status, f"{who} landed {actual:g} {noun}, {'YES' if happened else 'NO'} settles"
    if prop_type == "OVER_UNDER":
        if side not in ("OVER", "UNDER"):
            raise ValueError(f"Cannot grade side {side} on an OVER_UNDER prop")
        line = line_value if line_value is not None else 0.0
        over_result = actual - line
        if over_result > 0:
            status = "WON" if side == "OVER" else "LOST"
            relation = "over"
        elif over_result < 0:
            status = "WON" if side == "UNDER" else "LOST"
            relation = "under"
        else:
            status = "PUSH"
            relation = "push on"
        return status, f"{who} landed {actual:g} {noun}, {relation} {line:g}"
    raise ValueError(f"Cannot grade prop type {prop_type}")
