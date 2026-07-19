"""Pure parlay math: combined odds and parent settlement from legs (ADR-028).

Semantics:

- Combined decimal odds are the product of the legs' decimal odds.
- Legs grade independently through the same market paths as single bets.
- PUSH/VOID legs drop out and contribute 1.0 (the stake carries through),
  re-pricing the parlay over the surviving legs.
- The parent is WON iff at least one leg WON and none LOST, LOST if any leg
  LOST, and PUSH (full stake refund) when every leg dropped out.
- The parent settles only once no leg remains OPEN.

CLV for parlays is deferred in v1: closing lines are per-leg and a combined
closing price is not well-defined.
"""

from collections.abc import Sequence
from math import prod

from bookie_emulator.core.grading import GradeStatus

_DROPPED = frozenset({"PUSH", "VOID"})
# grade-row summary labels: wins/losses abbreviate, drop-outs stay explicit
_LEG_LABEL = {"WON": "W", "LOST": "L", "PUSH": "PUSH", "VOID": "VOID"}


def combined_decimal(leg_decimals: Sequence[float]) -> float:
    """Combined decimal odds of a parlay: the product of its legs' decimals."""
    if not leg_decimals:
        raise ValueError("A parlay needs at least one leg to price")
    return prod(leg_decimals, start=1.0)


def settle_parlay(leg_statuses: Sequence[str], leg_decimals: Sequence[float]) -> tuple[GradeStatus, float] | None:
    """Settle a parlay parent from its legs, or None while any leg is OPEN.

    Returns (status, repriced_decimal). PUSH/VOID legs contribute 1.0, so
    the re-priced decimal is the product of the WON legs' decimals (1.0 when
    every leg dropped out). For a LOST parlay the re-priced decimal is
    informational only: the whole stake is lost.
    """
    if len(leg_statuses) != len(leg_decimals):
        raise ValueError("leg_statuses and leg_decimals must align")
    if any(status == "OPEN" for status in leg_statuses):
        return None
    repriced = prod(
        (decimal for status, decimal in zip(leg_statuses, leg_decimals, strict=True) if status == "WON"),
        start=1.0,
    )
    if any(status == "LOST" for status in leg_statuses):
        return "LOST", repriced
    if all(status in _DROPPED for status in leg_statuses):
        return "PUSH", 1.0
    return "WON", repriced


def leg_outcome_summary(leg_statuses: Sequence[str], status: str, repriced_decimal: float) -> str:
    """Human summary for the parent grade row, e.g. "Legs: W-W-PUSH (re-priced 3.20)"."""
    legs = "-".join(_LEG_LABEL.get(leg_status, leg_status) for leg_status in leg_statuses)
    if status == "WON" and any(leg_status in _DROPPED for leg_status in leg_statuses):
        return f"Legs: {legs} (re-priced {repriced_decimal:.2f})"
    if status == "PUSH":
        return f"Legs: {legs} (all legs push/void: stake refunded)"
    return f"Legs: {legs}"
