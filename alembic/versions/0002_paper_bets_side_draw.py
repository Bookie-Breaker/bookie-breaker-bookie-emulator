"""Widen chk_paper_bets_side to accept DRAW for three-way moneylines.

ADR-027: soccer's primary moneyline is three-way (home/draw/away); the
third outcome is one more side value on the existing MONEYLINE market.
The league_enum values for the new leagues (FIFA_WC, EPL, NHL, NCAA_HKY)
are owned by infra-ops init-db scripts and are not migrated here.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-05

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIDES_WITH_DRAW = "side IN ('HOME', 'AWAY', 'DRAW', 'OVER', 'UNDER')"
_SIDES_WITHOUT_DRAW = "side IN ('HOME', 'AWAY', 'OVER', 'UNDER')"


def upgrade() -> None:
    op.drop_constraint("chk_paper_bets_side", "paper_bets", schema="emulator", type_="check")
    op.create_check_constraint("chk_paper_bets_side", "paper_bets", _SIDES_WITH_DRAW, schema="emulator")


def downgrade() -> None:
    op.drop_constraint("chk_paper_bets_side", "paper_bets", schema="emulator", type_="check")
    op.create_check_constraint("chk_paper_bets_side", "paper_bets", _SIDES_WITHOUT_DRAW, schema="emulator")
