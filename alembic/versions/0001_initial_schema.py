"""Initial emulator schema: paper_bets, bet_grades, bankroll_snapshots,
performance_summaries.

DDL follows schemas/database-schemas/bookie-emulator.md verbatim. The
league_enum/market_type_enum/bet_result_enum types are owned by infra-ops
init-db scripts in the public schema and are referenced, not created.
performance_summaries is created for schema completeness but not populated
in Phase 3 (performance endpoints aggregate live).

Revision ID: 0001
Revises:
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, schema="public", create_type=False)


def upgrade() -> None:
    op.create_table(
        "paper_bets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_external_id", sa.Text(), nullable=False),
        sa.Column("league", _enum("league_enum"), nullable=False),
        sa.Column("market_type", _enum("market_type_enum"), nullable=False),
        sa.Column("selection", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("line_value", sa.Numeric(8, 2)),
        sa.Column("sportsbook_id", postgresql.UUID(as_uuid=True)),
        sa.Column("sportsbook_key", sa.Text(), nullable=False),
        sa.Column("odds_american", sa.Integer(), nullable=False),
        sa.Column("odds_decimal", sa.Numeric(8, 4), nullable=False),
        sa.Column("stake", sa.Numeric(10, 4), nullable=False),
        sa.Column("predicted_probability", sa.Numeric(6, 5), nullable=False),
        sa.Column("edge_at_placement", sa.Numeric(6, 5), nullable=False),
        sa.Column("kelly_fraction", sa.Numeric(6, 5), nullable=False),
        sa.Column("reasoning", sa.Text()),
        sa.Column("prediction_id", postgresql.UUID(as_uuid=True)),
        sa.Column("edge_id", postgresql.UUID(as_uuid=True)),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("game_start_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("status", _enum("bet_result_enum"), nullable=False, server_default=sa.text("'OPEN'")),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("graded_at", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("idempotency_key", name="uq_paper_bets_idempotency_key"),
        sa.CheckConstraint("side IN ('HOME', 'AWAY', 'OVER', 'UNDER')", name="chk_paper_bets_side"),
        sa.CheckConstraint("stake > 0", name="chk_paper_bets_stake_positive"),
        sa.CheckConstraint(
            "predicted_probability > 0 AND predicted_probability < 1",
            name="chk_paper_bets_predicted_probability_range",
        ),
        sa.CheckConstraint("edge_at_placement > 0", name="chk_paper_bets_edge_positive"),
        sa.CheckConstraint("kelly_fraction >= 0 AND kelly_fraction <= 1", name="chk_paper_bets_kelly_range"),
        schema="emulator",
    )
    op.create_index(
        "idx_paper_bets_open",
        "paper_bets",
        ["game_id"],
        schema="emulator",
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_index("idx_paper_bets_placed", "paper_bets", [sa.text("placed_at DESC")], schema="emulator")
    op.create_index(
        "idx_paper_bets_league_market", "paper_bets", ["league", "market_type", "status"], schema="emulator"
    )
    op.create_index("idx_paper_bets_game", "paper_bets", ["game_id", "status"], schema="emulator")

    op.create_table(
        "bet_grades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "bet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("emulator.paper_bets.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("actual_result", sa.Text(), nullable=False),
        sa.Column("actual_home_score", sa.Integer()),
        sa.Column("actual_away_score", sa.Integer()),
        sa.Column("actual_margin", sa.Integer()),
        sa.Column("actual_total", sa.Integer()),
        sa.Column("game_result_id", postgresql.UUID(as_uuid=True)),
        sa.Column("profit_loss", sa.Numeric(10, 4), nullable=False),
        sa.Column("closing_line_value", sa.Numeric(8, 2)),
        sa.Column("closing_odds", sa.Integer()),
        sa.Column("clv", sa.Numeric(6, 5)),
        sa.Column("graded_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        schema="emulator",
    )
    op.create_index("idx_bet_grades_bet", "bet_grades", ["bet_id"], schema="emulator")
    op.create_index("idx_bet_grades_graded", "bet_grades", [sa.text("graded_at DESC")], schema="emulator")

    op.create_table(
        "bankroll_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("balance", sa.Numeric(12, 4), nullable=False),
        sa.Column("total_wagered", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("total_profit_loss", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("open_bets_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_bets", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_wins", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_losses", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("avg_clv", sa.Numeric(6, 5)),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        schema="emulator",
    )
    op.create_index(
        "idx_bankroll_snapshots_time", "bankroll_snapshots", [sa.text("snapshot_at DESC")], schema="emulator"
    )

    op.create_table(
        "performance_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("dimension_value", sa.Text(), nullable=False),
        sa.Column("period_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("period_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("total_bets", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("wins", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("losses", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pushes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_wagered", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("total_profit", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("roi", sa.Numeric(8, 5), nullable=False, server_default=sa.text("0")),
        sa.Column("avg_clv", sa.Numeric(6, 5)),
        sa.Column("avg_edge", sa.Numeric(6, 5)),
        sa.Column("brier_score", sa.Numeric(6, 5)),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint(
            "dimension",
            "dimension_value",
            "period_start",
            "period_end",
            name="uq_performance_summaries_dimension_period",
        ),
        schema="emulator",
    )
    op.create_index(
        "idx_performance_summaries_dimension",
        "performance_summaries",
        ["dimension", "dimension_value", sa.text("period_start DESC")],
        schema="emulator",
    )
    op.create_index(
        "idx_performance_summaries_period",
        "performance_summaries",
        [sa.text("period_start DESC"), "period_end"],
        schema="emulator",
    )


def downgrade() -> None:
    op.drop_table("performance_summaries", schema="emulator")
    op.drop_table("bankroll_snapshots", schema="emulator")
    op.drop_table("bet_grades", schema="emulator")
    op.drop_table("paper_bets", schema="emulator")
