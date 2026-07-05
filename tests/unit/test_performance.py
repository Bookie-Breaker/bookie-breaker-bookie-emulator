"""Performance analytics over hand-computed graded bet sets."""

from datetime import UTC, datetime, timedelta

import pytest

from bookie_emulator.core.performance import (
    GradedBet,
    brier_score,
    calibration_bins,
    compute_breakdown,
    compute_performance,
    expected_calibration_error,
    streaks,
    win_rate,
)

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def bet(
    status: str,
    order: int,
    stake: float = 1.0,
    odds_decimal: float = 1.909,
    predicted_probability: float = 0.55,
    edge: float = 0.04,
    profit_loss: float = 0.0,
    clv: float | None = None,
    league: str = "NBA",
    market_type: str = "SPREAD",
    sportsbook_key: str = "draftkings",
    placed_at: datetime | None = None,
) -> GradedBet:
    return GradedBet(
        league=league,
        market_type=market_type,
        sportsbook_key=sportsbook_key,
        stake=stake,
        odds_decimal=odds_decimal,
        predicted_probability=predicted_probability,
        edge_at_placement=edge,
        status=status,
        profit_loss=profit_loss,
        clv=clv,
        placed_at=placed_at or (BASE + timedelta(hours=order)),
        graded_at=BASE + timedelta(days=1, hours=order),
    )


def sample_bets() -> list[GradedBet]:
    return [
        bet("WON", 1, stake=1.0, odds_decimal=2.0, predicted_probability=0.6, edge=0.05, profit_loss=1.0, clv=0.01),
        bet("LOST", 2, stake=1.0, predicted_probability=0.55, edge=0.03, profit_loss=-1.0, market_type="TOTAL"),
        bet(
            "WON",
            3,
            stake=2.0,
            predicted_probability=0.7,
            edge=0.06,
            profit_loss=1.818,
            clv=0.03,
            league="MLB",
            sportsbook_key="fanduel",
        ),
        bet("PUSH", 4, stake=1.0, predicted_probability=0.5, edge=0.02, profit_loss=0.0, league="MLB"),
        bet(
            "WON",
            5,
            stake=1.0,
            odds_decimal=2.5,
            predicted_probability=0.65,
            edge=0.04,
            profit_loss=1.5,
            market_type="MONEYLINE",
        ),
    ]


class TestComputePerformance:
    def test_counts_and_win_rate_excludes_pushes(self) -> None:
        metrics = compute_performance(sample_bets())
        assert metrics.total_bets == 5
        assert metrics.wins == 3
        assert metrics.losses == 1
        assert metrics.pushes == 1
        assert metrics.win_rate == pytest.approx(0.75)  # 3 / (3 + 1)

    def test_roi_is_profit_over_wagered(self) -> None:
        metrics = compute_performance(sample_bets())
        assert metrics.total_wagered_units == pytest.approx(6.0)
        assert metrics.total_profit_units == pytest.approx(3.318)
        assert metrics.roi == pytest.approx(3.318 / 6.0)

    def test_avg_odds_from_mean_decimal(self) -> None:
        metrics = compute_performance(sample_bets())
        # mean decimal = (2.0 + 1.909 + 1.909 + 1.909 + 2.5) / 5 = 2.0454 -> +105
        assert metrics.avg_odds_american == 105

    def test_avg_edge_in_percentage_points(self) -> None:
        metrics = compute_performance(sample_bets())
        assert metrics.avg_edge_percentage == pytest.approx(4.0)

    def test_avg_clv_ignores_missing(self) -> None:
        metrics = compute_performance(sample_bets())
        assert metrics.avg_clv == pytest.approx(0.02)

    def test_brier_over_settled_only(self) -> None:
        metrics = compute_performance(sample_bets())
        expected = (0.4**2 + 0.55**2 + 0.3**2 + 0.35**2) / 4
        assert metrics.brier_score == pytest.approx(expected)

    def test_streaks(self) -> None:
        metrics = compute_performance(sample_bets())
        # settled sequence by graded_at: W L W W
        assert metrics.longest_win_streak == 2
        assert metrics.longest_loss_streak == 1
        assert metrics.current_streak == 2

    def test_empty_set(self) -> None:
        metrics = compute_performance([])
        assert metrics.total_bets == 0
        assert metrics.win_rate == 0.0
        assert metrics.roi == 0.0
        assert metrics.avg_odds_american is None
        assert metrics.brier_score is None
        assert metrics.calibration_error is None


class TestPureHelpers:
    def test_win_rate_zero_when_only_pushes(self) -> None:
        assert win_rate(0, 0) == 0.0

    def test_loss_streak_is_negative_current(self) -> None:
        bets = [bet("WON", 1), bet("LOST", 2), bet("LOST", 3)]
        assert streaks(bets) == (-2, 1, 2)

    def test_pushes_do_not_break_streaks(self) -> None:
        bets = [bet("WON", 1), bet("PUSH", 2), bet("WON", 3)]
        assert streaks(bets) == (2, 2, 0)

    def test_brier_none_without_settled_bets(self) -> None:
        assert brier_score([bet("PUSH", 1)]) is None

    def test_ece_single_bin_hand_computed(self) -> None:
        bets = [
            bet("WON", 1, predicted_probability=0.75),
            bet("WON", 2, predicted_probability=0.75),
        ]
        # one occupied bin: confidence 0.75, accuracy 1.0 -> ECE 0.25
        assert expected_calibration_error(bets) == pytest.approx(0.25)

    def test_ece_two_bins_weighted(self) -> None:
        bets = [
            bet("WON", 1, predicted_probability=0.75),
            bet("LOST", 2, predicted_probability=0.75),
            bet("WON", 3, predicted_probability=0.55),
            bet("WON", 4, predicted_probability=0.55),
        ]
        # bin .7-.8: |0.5 - 0.75| = 0.25 (weight 1/2); bin .5-.6: |1.0 - 0.55| = 0.45 (weight 1/2)
        assert expected_calibration_error(bets) == pytest.approx(0.35)


class TestCalibrationBins:
    def test_bin_stats_hand_computed(self) -> None:
        bets = [
            bet("WON", 1, predicted_probability=0.75),
            bet("LOST", 2, predicted_probability=0.79),
            bet("WON", 3, predicted_probability=0.55),
        ]
        bins = calibration_bins(bets)
        assert len(bins) == 10
        seventies = bins[7]
        assert (seventies.lower, seventies.upper) == (0.7, 0.8)
        assert seventies.bet_count == 2
        assert seventies.avg_predicted_probability == pytest.approx(0.77)
        assert seventies.actual_win_rate == pytest.approx(0.5)
        fifties = bins[5]
        assert fifties.bet_count == 1
        assert fifties.actual_win_rate == pytest.approx(1.0)

    def test_empty_bins_have_null_stats(self) -> None:
        bins = calibration_bins([bet("WON", 1, predicted_probability=0.55)])
        assert sum(b.bet_count for b in bins) == 1
        empty = bins[0]
        assert empty.bet_count == 0
        assert empty.avg_predicted_probability is None
        assert empty.actual_win_rate is None

    def test_no_bets_returns_all_empty_bins(self) -> None:
        bins = calibration_bins([])
        assert len(bins) == 10
        assert all(b.bet_count == 0 for b in bins)

    def test_probability_one_lands_in_last_bin(self) -> None:
        bins = calibration_bins([bet("WON", 1, predicted_probability=1.0)])
        assert bins[9].bet_count == 1

    def test_pushes_and_voids_excluded(self) -> None:
        bins = calibration_bins([bet("PUSH", 1), bet("VOID", 2), bet("WON", 3)])
        assert sum(b.bet_count for b in bins) == 1

    def test_custom_bin_count_edges(self) -> None:
        bins = calibration_bins([bet("WON", 1, predicted_probability=0.55)], n_bins=4)
        assert len(bins) == 4
        assert (bins[2].lower, bins[2].upper) == (0.5, 0.75)
        assert bins[2].bet_count == 1

    def test_ece_matches_bins_derivation(self) -> None:
        bets = sample_bets()
        bins = calibration_bins(bets)
        total = sum(b.bet_count for b in bins)
        expected = sum(
            (b.bet_count / total) * abs(b.actual_win_rate - b.avg_predicted_probability)
            for b in bins
            if b.actual_win_rate is not None and b.avg_predicted_probability is not None
        )
        assert expected_calibration_error(bets) == pytest.approx(expected)


class TestBreakdown:
    def test_group_by_league(self) -> None:
        groups = {g.group: g for g in compute_breakdown(sample_bets(), "league")}
        assert set(groups) == {"NBA", "MLB"}
        assert groups["NBA"].total_bets == 3
        assert groups["NBA"].wins == 2
        assert groups["NBA"].losses == 1
        assert groups["MLB"].total_bets == 2
        assert groups["MLB"].pushes == 1
        assert groups["MLB"].total_profit_units == pytest.approx(1.818)
        assert groups["MLB"].roi == pytest.approx(1.818 / 3.0)

    def test_group_by_market_type(self) -> None:
        groups = {g.group: g for g in compute_breakdown(sample_bets(), "market_type")}
        assert set(groups) == {"SPREAD", "TOTAL", "MONEYLINE"}
        assert groups["SPREAD"].total_bets == 3

    def test_group_by_sportsbook(self) -> None:
        groups = {g.group: g for g in compute_breakdown(sample_bets(), "sportsbook")}
        assert groups["fanduel"].total_bets == 1

    def test_group_by_month(self) -> None:
        bets = [
            bet("WON", 1, placed_at=datetime(2026, 2, 10, tzinfo=UTC), profit_loss=1.0),
            bet("LOST", 2, placed_at=datetime(2026, 3, 10, tzinfo=UTC), profit_loss=-1.0),
        ]
        groups = {g.group: g for g in compute_breakdown(bets, "month")}
        assert set(groups) == {"2026-02", "2026-03"}
