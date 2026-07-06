"""League-aware settlement rules: which scores settle a market and whether
moneylines grade three-way (ADR-026/ADR-027).

SPORT_BY_LEAGUE mirrors infra-ops init-db/02-create-enums.sql league/sport
values (local copy; no shared package per ADR-009). Soccer settles every
standard market on the 90-minute regulation score and grades moneylines
three-way (home/draw/away, no push).
"""

from collections.abc import Mapping
from typing import Any

SPORT_BY_LEAGUE: dict[str, str] = {
    "NFL": "FOOTBALL",
    "NCAA_FB": "FOOTBALL",
    "NBA": "BASKETBALL",
    "NCAA_BB": "BASKETBALL",
    "MLB": "BASEBALL",
    "NCAA_BSB": "BASEBALL",
    "FIFA_WC": "SOCCER",
    "EPL": "SOCCER",
    "NHL": "HOCKEY",
    "NCAA_HKY": "HOCKEY",
}

# NHL regulation-time three-way markets are deferred to the hockey wave
# (ADR-027); only soccer moneylines grade three-way in Phase 6.
_THREE_WAY_MONEYLINE_SPORTS = frozenset({"SOCCER"})


def is_three_way_moneyline_league(league: str) -> bool:
    """True when the league's moneylines are three-way (HOME/DRAW/AWAY, no push)."""
    return SPORT_BY_LEAGUE.get(league) in _THREE_WAY_MONEYLINE_SPORTS


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def settlement_scores(source: Any, league: str) -> tuple[int, int]:
    """Pick the settlement-relevant (home, away) scores for a league.

    SOCCER settles all standard markets on the regulation score when the
    optional ``regulation_home_score``/``regulation_away_score`` fields are
    present, falling back to the final score; every other sport settles on
    the final score. ``source`` is an ``events:game.completed`` payload
    mapping or an object with score attributes (e.g. a GameResult).
    """
    if SPORT_BY_LEAGUE.get(league) == "SOCCER":
        regulation_home = _field(source, "regulation_home_score")
        regulation_away = _field(source, "regulation_away_score")
        if regulation_home is not None and regulation_away is not None:
            return int(regulation_home), int(regulation_away)
    return int(_field(source, "home_score")), int(_field(source, "away_score"))
