"""Props and parlays foundation: side vocabulary + prop columns + parlay legs.

Phase 7 Wave 0 (ADR-028, ADR-029, ADR-027 amendment):

- side becomes nullable and accepts YES/NO (single-sided props; parlay
  parent rows carry no single side)
- game_id becomes nullable (a parlay parent spans multiple games; its legs
  carry the game references)
- structured prop columns (player_external_id, stat_type, prop_type) are
  added alongside the display-string selection per ADR-029
- is_parlay/is_live flags and the parent_bet_id self-reference mark parlay
  parents and live bets without overloading market_type
- bet_grades gains prop actuals (actual_stat_value, stat_type)
- new parlay_legs table holds per-leg selections graded independently;
  the parent paper_bets row settles once all legs are decided (ADR-028)

The market_type_enum PLAYER_PROP/TEAM_PROP/GAME_PROP/LIVE values already
exist (owned by infra-ops init-db); no enum change here.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIDES_NULLABLE_WITH_PROPS = "side IS NULL OR side IN ('HOME', 'AWAY', 'DRAW', 'OVER', 'UNDER', 'YES', 'NO')"
_SIDES_NOT_NULL_WITHOUT_PROPS = "side IN ('HOME', 'AWAY', 'DRAW', 'OVER', 'UNDER')"


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, schema="public", create_type=False)


def upgrade() -> None:
    op.alter_column("paper_bets", "side", nullable=True, schema="emulator")
    op.alter_column("paper_bets", "game_id", nullable=True, schema="emulator")
    op.drop_constraint("chk_paper_bets_side", "paper_bets", schema="emulator", type_="check")
    op.create_check_constraint("chk_paper_bets_side", "paper_bets", _SIDES_NULLABLE_WITH_PROPS, schema="emulator")

    op.add_column("paper_bets", sa.Column("player_external_id", sa.Text()), schema="emulator")
    op.add_column("paper_bets", sa.Column("stat_type", sa.Text()), schema="emulator")
    op.add_column("paper_bets", sa.Column("prop_type", sa.Text()), schema="emulator")
    op.add_column(
        "paper_bets",
        sa.Column("is_parlay", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        schema="emulator",
    )
    op.add_column(
        "paper_bets",
        sa.Column("is_live", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        schema="emulator",
    )
    op.add_column(
        "paper_bets",
        sa.Column(
            "parent_bet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("emulator.paper_bets.id", name="fk_paper_bets_parent_bet"),
        ),
        schema="emulator",
    )

    op.add_column("bet_grades", sa.Column("actual_stat_value", sa.Numeric(10, 2)), schema="emulator")
    op.add_column("bet_grades", sa.Column("stat_type", sa.Text()), schema="emulator")

    op.create_table(
        "parlay_legs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "bet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("emulator.paper_bets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True)),
        sa.Column("game_external_id", sa.Text(), nullable=False),
        sa.Column("league", _enum("league_enum"), nullable=False),
        sa.Column("market_type", _enum("market_type_enum"), nullable=False),
        sa.Column("selection", sa.Text(), nullable=False),
        sa.Column("side", sa.Text()),
        sa.Column("line_value", sa.Numeric(8, 2)),
        sa.Column("player_external_id", sa.Text()),
        sa.Column("stat_type", sa.Text()),
        sa.Column("prop_type", sa.Text()),
        sa.Column("odds_american", sa.Integer(), nullable=False),
        sa.Column("odds_decimal", sa.Numeric(8, 4), nullable=False),
        sa.Column("leg_status", _enum("bet_result_enum"), nullable=False, server_default=sa.text("'OPEN'")),
        sa.UniqueConstraint("bet_id", "leg_index", name="uq_parlay_legs_bet_leg_index"),
        sa.CheckConstraint(_SIDES_NULLABLE_WITH_PROPS, name="chk_parlay_legs_side"),
        schema="emulator",
    )
    op.create_index("idx_parlay_legs_bet", "parlay_legs", ["bet_id"], schema="emulator")
    op.create_index(
        "idx_parlay_legs_open_game",
        "parlay_legs",
        ["game_id"],
        schema="emulator",
        postgresql_where=sa.text("leg_status = 'OPEN'"),
    )


def downgrade() -> None:
    # Fails if parlay legs, YES/NO sides, null sides, or null game_ids exist;
    # delete those rows before downgrading.
    op.drop_index("idx_parlay_legs_open_game", "parlay_legs", schema="emulator")
    op.drop_index("idx_parlay_legs_bet", "parlay_legs", schema="emulator")
    op.drop_table("parlay_legs", schema="emulator")

    op.drop_column("bet_grades", "stat_type", schema="emulator")
    op.drop_column("bet_grades", "actual_stat_value", schema="emulator")

    op.drop_constraint("fk_paper_bets_parent_bet", "paper_bets", schema="emulator", type_="foreignkey")
    op.drop_column("paper_bets", "parent_bet_id", schema="emulator")
    op.drop_column("paper_bets", "is_live", schema="emulator")
    op.drop_column("paper_bets", "is_parlay", schema="emulator")
    op.drop_column("paper_bets", "prop_type", schema="emulator")
    op.drop_column("paper_bets", "stat_type", schema="emulator")
    op.drop_column("paper_bets", "player_external_id", schema="emulator")

    op.drop_constraint("chk_paper_bets_side", "paper_bets", schema="emulator", type_="check")
    op.create_check_constraint("chk_paper_bets_side", "paper_bets", _SIDES_NOT_NULL_WITHOUT_PROPS, schema="emulator")
    op.alter_column("paper_bets", "game_id", nullable=False, schema="emulator")
    op.alter_column("paper_bets", "side", nullable=False, schema="emulator")
