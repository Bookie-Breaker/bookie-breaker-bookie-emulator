"""Bet placement and ledger flows against real Postgres/Redis with mocked upstreams."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import Response

from tests.integration.conftest import (
    LINES_URL,
    STATS_URL,
    enveloped,
    game_lines_payload,
    game_payload,
    iso,
    mock_best_lines,
    mock_scheduled_game,
    mock_soccer_best_lines,
    place_bet,
)


class TestPlacement:
    def test_happy_path_captures_best_line(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        mock_best_lines(upstream, ext_id)

        response = place_bet(client, game_id, ext_id)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["game_id"] == game_id
        assert data["game_external_id"] == ext_id
        assert data["sportsbook_key"] == "pinnacle"
        assert data["line_value"] == -3.5
        assert data["odds_american"] == -105
        assert data["odds_decimal"] == 1.952
        assert data["result"] == "PENDING"
        assert data["stake"] == 1.5
        assert data["stake_dollars"] == 150.0
        assert data["edge_percentage"] == 4.2
        assert data["kelly_fraction"] == 0.25  # service default
        assert data["profit_loss"] is None
        assert data["graded_at"] is None

    def test_pinned_sportsbook_placement(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        upstream.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}").mock(
            return_value=Response(200, json=enveloped(game_lines_payload(ext_id)))
        )

        response = place_bet(client, game_id, ext_id, sportsbook_key="draftkings")
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["sportsbook_key"] == "draftkings"
        assert data["odds_american"] == -110

    def test_idempotent_replay_returns_existing_bet(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        mock_best_lines(upstream, ext_id)
        key = str(uuid.uuid4())

        first = place_bet(client, game_id, ext_id, idempotency_key=key)
        assert first.status_code == 201
        replay = place_bet(client, game_id, ext_id, idempotency_key=key)
        assert replay.status_code == 200
        assert replay.json()["data"]["id"] == first.json()["data"]["id"]

    def test_missing_idempotency_key_is_rejected(self, client, upstream) -> None:
        response = client.post("/api/v1/emulator/bets", json={"game_id": str(uuid.uuid4())})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_game_already_started_rejected(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        started = game_payload(game_id, scheduled_start=iso(datetime.now(tz=UTC) - timedelta(hours=1)))
        upstream.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(
            return_value=Response(200, json=enveloped(started))
        )
        response = place_bet(client, game_id, "odds-any")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNPROCESSABLE_ENTITY"

    def test_live_game_rejected(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        upstream.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(
            return_value=Response(200, json=enveloped(game_payload(game_id, status="LIVE")))
        )
        assert place_bet(client, game_id, "odds-any").status_code == 422

    def test_unknown_game_rejected(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        upstream.get(f"{STATS_URL}/api/v1/stats/games/{game_id}").mock(
            return_value=Response(404, json={"error": {"code": "RESOURCE_NOT_FOUND", "message": "no"}, "meta": {}})
        )
        assert place_bet(client, game_id, "odds-any").status_code == 422

    def test_insufficient_bankroll_rejected(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        mock_best_lines(upstream, ext_id)
        response = place_bet(client, game_id, ext_id, stake=100_000.0)
        assert response.status_code == 422
        assert "bankroll" in response.json()["error"]["message"]

    def test_non_positive_stake_rejected(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        mock_scheduled_game(upstream, game_id)
        assert place_bet(client, game_id, "odds-any", stake=0.0).status_code == 422

    def test_unreconcilable_game_rejected(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        mock_scheduled_game(upstream, game_id)
        upstream.get(f"{LINES_URL}/api/v1/lines/current").mock(return_value=Response(200, json=enveloped([])))
        response = place_bet(client, game_id)  # no game_external_id -> reconciler
        assert response.status_code == 422
        assert "matched" in response.json()["error"]["message"]

    def test_lines_service_down_is_502(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        upstream.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(return_value=Response(500))
        response = place_bet(client, game_id, ext_id)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "DEPENDENCY_ERROR"

    def test_no_matching_market_rejected(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        upstream.get(f"{LINES_URL}/api/v1/lines/game/{ext_id}/best").mock(
            return_value=Response(200, json=enveloped([]))
        )
        assert place_bet(client, game_id, ext_id).status_code == 422


class TestDrawPlacement:
    """DRAW is a valid side only on (three-way) moneylines -- ADR-027."""

    def test_draw_moneyline_bet_accepted_on_soccer_game(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id, league="FIFA_WC")
        mock_soccer_best_lines(upstream, ext_id)

        response = place_bet(client, game_id, ext_id, market_type="MONEYLINE", selection="Draw", side="DRAW")
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["side"] == "DRAW"
        assert data["market_type"] == "MONEYLINE"
        assert data["odds_american"] == 230  # the DRAW row's odds were captured
        assert data["result"] == "PENDING"

    def test_draw_rejected_for_spread(self, client, upstream) -> None:
        response = place_bet(client, str(uuid.uuid4()), "odds-any", market_type="SPREAD", side="DRAW")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_draw_rejected_for_total(self, client, upstream) -> None:
        response = place_bet(client, str(uuid.uuid4()), "odds-any", market_type="TOTAL", side="DRAW")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestLedger:
    def _seed(self, client, upstream, count: int = 3) -> list[str]:
        ids = []
        for index in range(count):
            game_id = str(uuid.uuid4())
            ext_id = f"odds-{uuid.uuid4().hex}"
            mock_scheduled_game(upstream, game_id, league="NFL")
            mock_best_lines(upstream, ext_id)
            response = place_bet(client, game_id, ext_id, edge_percentage=3.0 + index)
            assert response.status_code == 201
            ids.append(response.json()["data"]["id"])
        return ids

    def test_filters_and_cursor_pagination(self, client, upstream) -> None:
        seeded = self._seed(client, upstream, count=3)

        # league filter isolates this test's bets (only it uses NFL)
        listing = client.get("/api/v1/emulator/bets", params={"league": "NFL", "limit": 200})
        assert listing.status_code == 200
        ids = [bet["id"] for bet in listing.json()["data"]]
        assert set(seeded) <= set(ids)
        pagination = listing.json()["meta"]["pagination"]
        assert pagination["limit"] == 200
        assert pagination["has_more"] is False
        assert pagination["next_cursor"] is None

        # min_edge filters in percentage points
        edged = client.get("/api/v1/emulator/bets", params={"league": "NFL", "min_edge": 4.5})
        edges = [bet["edge_percentage"] for bet in edged.json()["data"]]
        assert edges and all(edge >= 4.5 for edge in edges)

        # status=open returns only PENDING bets
        open_bets = client.get("/api/v1/emulator/bets", params={"league": "NFL", "status": "open"})
        assert all(bet["result"] == "PENDING" for bet in open_bets.json()["data"])

        # cursor paging walks all NFL bets without duplicates
        collected: list[str] = []
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, object] = {"league": "NFL", "limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = client.get("/api/v1/emulator/bets", params=params)
            assert page.status_code == 200
            body = page.json()
            collected.extend(bet["id"] for bet in body["data"])
            if not body["meta"]["pagination"]["has_more"]:
                break
            cursor = body["meta"]["pagination"]["next_cursor"]
            assert cursor
        assert len(collected) == len(set(collected))
        assert set(seeded) <= set(collected)

    def test_invalid_cursor_rejected(self, client) -> None:
        response = client.get("/api/v1/emulator/bets", params={"cursor": "garbage!!"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_PARAMETER"

    def test_result_filter_uses_api_vocabulary(self, client) -> None:
        response = client.get("/api/v1/emulator/bets", params={"result": "PENDING", "limit": 5})
        assert response.status_code == 200
        assert all(bet["result"] == "PENDING" for bet in response.json()["data"])
        assert client.get("/api/v1/emulator/bets", params={"result": "OPEN"}).status_code == 400


class TestBetDetail:
    def test_unknown_bet_404(self, client) -> None:
        response = client.get(f"/api/v1/emulator/bets/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_pending_bet_detail_has_no_grade(self, client, upstream) -> None:
        game_id = str(uuid.uuid4())
        ext_id = f"odds-{uuid.uuid4().hex}"
        mock_scheduled_game(upstream, game_id)
        mock_best_lines(upstream, ext_id)
        bet_id = place_bet(client, game_id, ext_id).json()["data"]["id"]

        detail = client.get(f"/api/v1/emulator/bets/{bet_id}")
        assert detail.status_code == 200
        data = detail.json()["data"]
        assert data["result"] == "PENDING"
        assert data["grade"] is None
        assert data["closing_line_value"] is None
