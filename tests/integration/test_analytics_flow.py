"""Performance, breakdown, bankroll, and history endpoints over seeded fixtures.

Seeds MLB bets (only this module uses MLB) so aggregate assertions are
isolated from bets placed by other test modules sharing the session DB.
"""

import uuid
from typing import Any

import pytest
from httpx import Response

from tests.integration.conftest import (
    LINES_URL,
    STATS_URL,
    closing_lines_payload,
    enveloped,
    final_game_payload,
    game_payload,
    place_bet,
)


def _mock_game(router: Any, game_id: str, payload: dict[str, Any]) -> None:
    router.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(return_value=Response(200, json=enveloped(payload)))


def _mock_best(router: Any, ext_id: str, lines: list[dict[str, Any]]) -> None:
    router.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(return_value=Response(200, json=enveloped(lines)))


def _best_line(market_type: str, side: str, line_value: float | None, odds: int, decimal: float) -> dict[str, Any]:
    return {
        "market_type": market_type,
        "selection": f"{market_type} {side}",
        "side": side,
        "line_value": line_value,
        "best_odds_american": odds,
        "best_odds_decimal": decimal,
        "sportsbook_id": "00000000-0000-4000-8000-00000000aaaa",
        "sportsbook_key": "pinnacle",
        "timestamp": "2026-07-04T12:00:00Z",
    }


@pytest.fixture(scope="module")
def seeded_mlb(client) -> dict[str, Any]:
    """Three graded MLB bets: SPREAD WIN, TOTAL LOSS, MONEYLINE LOSS."""
    import respx

    with respx.mock(assert_all_called=False) as router:
        game_a = str(uuid.uuid4())
        ext_a = f"odds-{uuid.uuid4().hex}"
        game_b = str(uuid.uuid4())
        ext_b = f"odds-{uuid.uuid4().hex}"

        _mock_game(router, game_a, game_payload(game_a, league="MLB"))
        _mock_game(router, game_b, game_payload(game_b, league="MLB"))
        _mock_best(
            router,
            ext_a,
            [
                _best_line("SPREAD", "HOME", -1.5, -110, 1.909),
                _best_line("TOTAL", "OVER", 12.5, -105, 1.952),
            ],
        )
        _mock_best(router, ext_b, [_best_line("MONEYLINE", "HOME", None, 120, 2.2)])

        bets = {
            "spread": place_bet(
                client,
                game_a,
                ext_a,
                market_type="SPREAD",
                side="HOME",
                selection="HOME -1.5",
                stake=1.0,
                predicted_probability=0.60,
                edge_percentage=5.0,
            ).json()["data"]["id"],
            "total": place_bet(
                client,
                game_a,
                ext_a,
                market_type="TOTAL",
                side="OVER",
                selection="Over 12.5",
                stake=2.0,
                predicted_probability=0.55,
                edge_percentage=3.0,
            ).json()["data"]["id"],
            "moneyline": place_bet(
                client,
                game_b,
                ext_b,
                market_type="MONEYLINE",
                side="HOME",
                selection="HOME ML",
                stake=1.0,
                predicted_probability=0.65,
                edge_percentage=4.0,
            ).json()["data"]["id"],
        }

        # Game A final 7-3: HOME -1.5 covers (WIN); Over 12.5 lands 10 (LOSS)
        _mock_game(router, game_a, final_game_payload(game_a, 7, 3, league="MLB"))
        # SPREAD closing at pinnacle -120 -> CLV vs -110 placement
        closing = closing_lines_payload(ext_a)
        closing[0]["market_type"] = "SPREAD"
        closing[0]["side"] = "HOME"
        closing[0]["line_value"] = -1.5
        router.get(f"{LINES_URL}/api/v1/lines/game/{ext_a}/closing").mock(
            return_value=Response(200, json=enveloped(closing))
        )
        # Game B final 3-4: HOME ML loses; closing lines unavailable -> CLV null
        _mock_game(router, game_b, final_game_payload(game_b, 3, 4, league="MLB"))
        router.get(f"{LINES_URL}/api/v1/lines/game/{ext_b}/closing").mock(return_value=Response(500))

        for bet_id in bets.values():
            graded = client.post(f"/api/v1/emulator/bets/{bet_id}/grade", json={})
            assert graded.status_code == 200, graded.text
        return bets


class TestPerformance:
    def test_mlb_metrics_hand_computed(self, client, seeded_mlb) -> None:
        response = client.get("/api/v1/emulator/performance", params={"league": "MLB"})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["period"]["window"] == "all_time"
        assert data["total_bets"] == 3
        assert data["total_wins"] == 1
        assert data["total_losses"] == 2
        assert data["total_pushes"] == 0
        assert data["win_rate"] == pytest.approx(1 / 3)
        # profit = 1*(1.909-1) - 2.0 - 1.0 = -2.091 over 4.0 wagered
        assert data["total_wagered_units"] == pytest.approx(4.0)
        assert data["total_profit_units"] == pytest.approx(-2.091, abs=1e-3)
        assert data["roi"] == pytest.approx(-2.091 / 4.0, abs=1e-3)
        assert data["total_wagered_dollars"] == pytest.approx(400.0)
        assert data["avg_edge_percentage"] == pytest.approx(4.0)
        # only the spread bet has CLV: implied(-120) - implied(-110)
        assert data["avg_clv"] == pytest.approx(0.02165, abs=1e-4)
        # graded order: spread WIN, total LOSS, moneyline LOSS
        assert data["longest_win_streak"] == 1
        assert data["longest_loss_streak"] == 2
        brier = (0.40**2 + 0.55**2 + 0.65**2) / 3
        assert data["brier_score"] == pytest.approx(brier, abs=1e-4)
        assert data["calibration_error"] is not None

    def test_market_type_filter(self, client, seeded_mlb) -> None:
        response = client.get("/api/v1/emulator/performance", params={"league": "MLB", "market_type": "SPREAD"})
        data = response.json()["data"]
        assert data["total_bets"] == 1
        assert data["total_wins"] == 1
        assert data["win_rate"] == 1.0

    def test_window_daily_includes_fresh_grades(self, client, seeded_mlb) -> None:
        response = client.get("/api/v1/emulator/performance", params={"league": "MLB", "window": "daily"})
        data = response.json()["data"]
        assert data["period"]["window"] == "daily"
        assert data["total_bets"] == 3  # graded moments ago

    def test_date_filter_excludes_everything_in_the_past(self, client, seeded_mlb) -> None:
        response = client.get(
            "/api/v1/emulator/performance", params={"league": "MLB", "date_to": "2020-01-01T00:00:00Z"}
        )
        assert response.json()["data"]["total_bets"] == 0


class TestBreakdown:
    def test_group_by_league_contains_mlb_group(self, client, seeded_mlb) -> None:
        response = client.get("/api/v1/emulator/performance/breakdown", params={"group_by": "league"})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["group_by"] == "league"
        by_group = {entry["group"]: entry for entry in data["breakdowns"]}
        assert "MLB" in by_group
        mlb = by_group["MLB"]
        assert mlb["total_bets"] == 3
        assert mlb["wins"] == 1
        assert mlb["losses"] == 2
        assert mlb["total_profit_units"] == pytest.approx(-2.091, abs=1e-3)
        assert mlb["avg_edge_percentage"] == pytest.approx(4.0)

    def test_group_by_market_type_and_month(self, client, seeded_mlb) -> None:
        markets = client.get("/api/v1/emulator/performance/breakdown", params={"group_by": "market_type"})
        assert {entry["group"] for entry in markets.json()["data"]["breakdowns"]} >= {"SPREAD"}
        months = client.get("/api/v1/emulator/performance/breakdown", params={"group_by": "month"})
        for entry in months.json()["data"]["breakdowns"]:
            assert len(entry["group"]) == 7  # YYYY-MM

    def test_invalid_group_by_rejected(self, client) -> None:
        response = client.get("/api/v1/emulator/performance/breakdown", params={"group_by": "vibe"})
        assert response.status_code == 400


class TestBankroll:
    def test_current_bankroll_is_consistent(self, client, seeded_mlb) -> None:
        response = client.get("/api/v1/emulator/bankroll")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["starting_bankroll_units"] == 100.0
        assert data["unit_size_dollars"] == 100.0
        assert data["bankroll_units"] == pytest.approx(100.0 + data["total_profit_units"], abs=1e-6)
        assert data["bankroll_dollars"] == pytest.approx(data["bankroll_units"] * 100.0, abs=1e-4)
        assert data["open_bets_count"] >= 0
        assert data["open_bets_exposure_units"] >= 0.0
        assert data["config"] == {
            "max_bet_units": 3.0,
            "max_daily_exposure_units": 10.0,
            "kelly_fraction": 0.25,
            "kelly_enabled": True,
        }

    def test_history_per_bet_and_daily_buckets(self, client, seeded_mlb) -> None:
        per_bet = client.get("/api/v1/emulator/bankroll/history", params={"interval": "per_bet"})
        assert per_bet.status_code == 200
        snapshots = per_bet.json()["data"]["snapshots"]
        assert len(snapshots) >= 3  # one snapshot per grading transaction
        timestamps = [snap["timestamp"] for snap in snapshots]
        assert timestamps == sorted(timestamps)
        latest = snapshots[-1]
        assert latest["total_bets"] >= 3
        assert latest["total_bets"] >= latest["total_wins"] + latest["total_losses"]
        assert latest["bankroll_dollars"] == pytest.approx(latest["bankroll_units"] * 100.0, abs=1e-4)

        daily = client.get("/api/v1/emulator/bankroll/history", params={"interval": "daily"})
        daily_snapshots = daily.json()["data"]["snapshots"]
        assert len(daily_snapshots) == 1  # everything graded today collapses to one bucket
        assert daily_snapshots[0]["timestamp"] == snapshots[-1]["timestamp"]

        weekly = client.get("/api/v1/emulator/bankroll/history", params={"interval": "weekly"})
        assert len(weekly.json()["data"]["snapshots"]) == 1

    def test_bankroll_matches_performance_totals(self, client, seeded_mlb) -> None:
        bankroll = client.get("/api/v1/emulator/bankroll").json()["data"]
        performance = client.get("/api/v1/emulator/performance").json()["data"]
        assert bankroll["total_profit_units"] == pytest.approx(performance["total_profit_units"], abs=1e-6)
