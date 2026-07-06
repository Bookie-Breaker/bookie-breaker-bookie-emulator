"""SQLAlchemy Core table definitions matching schemas/database-schemas/bookie-emulator.md.

The enum types (league_enum, market_type_enum, bet_result_enum) live in the
``public`` schema and are owned by infra-ops init-db scripts, so they are
referenced with create_type=False. DDL itself is applied by Alembic.
"""

import uuid
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData(schema="emulator")


# Values mirror infra-ops init-db/02-create-enums.sql; declared here so
# SQLAlchemy can bind and validate parameters (the types are NOT created by
# this service).
_ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "league_enum": ("NFL", "NBA", "MLB", "NCAA_FB", "NCAA_BB", "NCAA_BSB", "FIFA_WC", "EPL", "NHL", "NCAA_HKY"),
    "market_type_enum": ("SPREAD", "TOTAL", "MONEYLINE", "PLAYER_PROP", "TEAM_PROP", "GAME_PROP", "FUTURE", "LIVE"),
    "bet_result_enum": ("OPEN", "WON", "LOST", "PUSH", "VOID"),
}


def _enum(name: str) -> "postgresql.ENUM":
    return postgresql.ENUM(*_ENUM_VALUES[name], name=name, schema="public", create_type=False)


def _uuid_pk() -> Any:
    return Column(
        "id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


paper_bets = Table(
    "paper_bets",
    metadata,
    _uuid_pk(),
    Column("game_id", UUID(as_uuid=True), nullable=False),
    Column("game_external_id", Text, nullable=False),
    Column("league", _enum("league_enum"), nullable=False),
    Column("market_type", _enum("market_type_enum"), nullable=False),
    Column("selection", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("line_value", Numeric(8, 2)),
    Column("sportsbook_id", UUID(as_uuid=True)),
    Column("sportsbook_key", Text, nullable=False),
    Column("odds_american", Integer, nullable=False),
    Column("odds_decimal", Numeric(8, 4), nullable=False),
    Column("stake", Numeric(10, 4), nullable=False),
    Column("predicted_probability", Numeric(6, 5), nullable=False),
    Column("edge_at_placement", Numeric(6, 5), nullable=False),
    Column("kelly_fraction", Numeric(6, 5), nullable=False),
    Column("reasoning", Text),
    Column("prediction_id", UUID(as_uuid=True)),
    Column("edge_id", UUID(as_uuid=True)),
    Column("idempotency_key", Text, nullable=False),
    Column("game_start_at", TIMESTAMP(timezone=True)),
    Column("status", _enum("bet_result_enum"), nullable=False, server_default=text("'OPEN'")),
    Column("placed_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")),
    Column("graded_at", TIMESTAMP(timezone=True)),
    UniqueConstraint("idempotency_key", name="uq_paper_bets_idempotency_key"),
    CheckConstraint("side IN ('HOME', 'AWAY', 'DRAW', 'OVER', 'UNDER')", name="chk_paper_bets_side"),
    CheckConstraint("stake > 0", name="chk_paper_bets_stake_positive"),
    CheckConstraint(
        "predicted_probability > 0 AND predicted_probability < 1",
        name="chk_paper_bets_predicted_probability_range",
    ),
    CheckConstraint("edge_at_placement > 0", name="chk_paper_bets_edge_positive"),
    CheckConstraint("kelly_fraction >= 0 AND kelly_fraction <= 1", name="chk_paper_bets_kelly_range"),
    Index("idx_paper_bets_open", "game_id", postgresql_where=text("status = 'OPEN'")),
    Index("idx_paper_bets_placed", text("placed_at DESC")),
    Index("idx_paper_bets_league_market", "league", "market_type", "status"),
    Index("idx_paper_bets_game", "game_id", "status"),
)

bet_grades = Table(
    "bet_grades",
    metadata,
    _uuid_pk(),
    Column("bet_id", UUID(as_uuid=True), ForeignKey("paper_bets.id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("actual_result", Text, nullable=False),
    Column("actual_home_score", Integer),
    Column("actual_away_score", Integer),
    Column("actual_margin", Integer),
    Column("actual_total", Integer),
    Column("game_result_id", UUID(as_uuid=True)),
    Column("profit_loss", Numeric(10, 4), nullable=False),
    Column("closing_line_value", Numeric(8, 2)),
    Column("closing_odds", Integer),
    Column("clv", Numeric(6, 5)),
    Column("graded_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")),
    Index("idx_bet_grades_bet", "bet_id"),
    Index("idx_bet_grades_graded", text("graded_at DESC")),
)

bankroll_snapshots = Table(
    "bankroll_snapshots",
    metadata,
    _uuid_pk(),
    Column("balance", Numeric(12, 4), nullable=False),
    Column("total_wagered", Numeric(12, 4), nullable=False, server_default=text("0")),
    Column("total_profit_loss", Numeric(12, 4), nullable=False, server_default=text("0")),
    Column("open_bets_count", Integer, nullable=False, server_default=text("0")),
    Column("total_bets", Integer, nullable=False, server_default=text("0")),
    Column("total_wins", Integer, nullable=False, server_default=text("0")),
    Column("total_losses", Integer, nullable=False, server_default=text("0")),
    Column("avg_clv", Numeric(6, 5)),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")),
    Index("idx_bankroll_snapshots_time", text("snapshot_at DESC")),
)

# Created for schema completeness (Phase 3): performance endpoints aggregate
# live over paper_bets JOIN bet_grades; this table is not populated yet.
performance_summaries = Table(
    "performance_summaries",
    metadata,
    _uuid_pk(),
    Column("dimension", Text, nullable=False),
    Column("dimension_value", Text, nullable=False),
    Column("period_start", TIMESTAMP(timezone=True), nullable=False),
    Column("period_end", TIMESTAMP(timezone=True), nullable=False),
    Column("total_bets", Integer, nullable=False, server_default=text("0")),
    Column("wins", Integer, nullable=False, server_default=text("0")),
    Column("losses", Integer, nullable=False, server_default=text("0")),
    Column("pushes", Integer, nullable=False, server_default=text("0")),
    Column("total_wagered", Numeric(12, 4), nullable=False, server_default=text("0")),
    Column("total_profit", Numeric(12, 4), nullable=False, server_default=text("0")),
    Column("roi", Numeric(8, 5), nullable=False, server_default=text("0")),
    Column("avg_clv", Numeric(6, 5)),
    Column("avg_edge", Numeric(6, 5)),
    Column("brier_score", Numeric(6, 5)),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")),
    UniqueConstraint(
        "dimension", "dimension_value", "period_start", "period_end", name="uq_performance_summaries_dimension_period"
    ),
    Index("idx_performance_summaries_dimension", "dimension", "dimension_value", text("period_start DESC")),
    Index("idx_performance_summaries_period", text("period_start DESC"), "period_end"),
)
