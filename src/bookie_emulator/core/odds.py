"""American/decimal odds conversions and implied probabilities."""


def american_to_decimal(odds_american: int) -> float:
    """Convert American odds to decimal odds (e.g., -110 -> 1.9091, +150 -> 2.5)."""
    if odds_american == 0:
        raise ValueError("American odds cannot be 0")
    if odds_american > 0:
        return 1.0 + odds_american / 100.0
    return 1.0 + 100.0 / -odds_american


def decimal_to_american(odds_decimal: float) -> int:
    """Convert decimal odds to the nearest American odds (e.g., 1.9091 -> -110)."""
    if odds_decimal <= 1.0:
        raise ValueError("Decimal odds must be greater than 1")
    if odds_decimal >= 2.0:
        return round((odds_decimal - 1.0) * 100.0)
    return -round(100.0 / (odds_decimal - 1.0))


def implied_probability(odds_american: int) -> float:
    """Implied win probability of American odds (vig included)."""
    return 1.0 / american_to_decimal(odds_american)


def implied_probability_decimal(odds_decimal: float) -> float:
    """Implied win probability of decimal odds (vig included)."""
    if odds_decimal <= 1.0:
        raise ValueError("Decimal odds must be greater than 1")
    return 1.0 / odds_decimal
