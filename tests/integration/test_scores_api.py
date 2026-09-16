"""POST /v1/scores and the game registry, end to end over HTTP.

Exercises the full stack — middleware, auth, validation, the transaction, the
index write and the response contract — against real Postgres and Redis.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.integration

API_KEY = "7f3c91ab5e2d4806bc1f9a73de50c284"
ADMIN_KEY = "d4a82fe07c6b13594ea2f8db60371cae"
AUTH = {"X-API-Key": API_KEY}
ADMIN = {"X-Admin-Key": ADMIN_KEY}


async def register(client: httpx.AsyncClient, game_id: str = "chess") -> None:
    response = await client.post(
        "/v1/admin/games", headers=ADMIN, json={"id": game_id, "name": game_id.title()}
    )
    assert response.status_code == 200, response.text


def body(user_id: str = "player_4417", score: int = 18500, **extra: object) -> dict[str, object]:
    return {"user_id": user_id, "game_id": "chess", "score": score, **extra}


class TestAuthentication:
    async def test_submission_without_a_key_is_rejected(
        self, api_client: httpx.AsyncClient
    ) -> None:
        response = await api_client.post("/v1/scores", json=body())

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_submission_with_a_wrong_key_is_rejected(
        self, api_client: httpx.AsyncClient
    ) -> None:
        response = await api_client.post("/v1/scores", headers={"X-API-Key": "x" * 32}, json=body())
        assert response.status_code == 401

    async def test_auth_is_checked_before_the_game_lookup(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """An unauthenticated caller must not learn whether a game exists."""
        response = await api_client.post("/v1/scores", json=body())
        assert response.status_code == 401
        assert "GAME" not in response.text

    async def test_the_write_key_cannot_register_games(self, api_client: httpx.AsyncClient) -> None:
        """Separate keys: a leaked game-server credential must not create games."""
        response = await api_client.post(
            "/v1/admin/games",
            headers={"X-Admin-Key": API_KEY},
            json={"id": "chess", "name": "Chess"},
        )
        assert response.status_code == 401

    async def test_reads_are_public(self, api_client: httpx.AsyncClient) -> None:
        assert (await api_client.get("/v1/games")).status_code == 200


class TestSubmission:
    async def test_successful_submission_reports_all_three_boards(
        self, api_client: httpx.AsyncClient
    ) -> None:
        await register(api_client)

        response = await api_client.post(
            "/v1/scores", headers=AUTH, json=body(score=18500, display_name="Ayo")
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["user_id"] == "player_4417"
        assert payload["submitted_score"] == 18500
        assert payload["submitted_at"]
        assert [s["period"] for s in payload["standings"]] == [
            "all_time",
            "daily",
            "weekly",
        ]
        assert all(s["improved"] for s in payload["standings"])
        assert all(s["rank"] == 1 for s in payload["standings"])

    async def test_lower_score_returns_200_with_improved_false(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Not beating your best is a successful request, not an error."""
        await register(api_client)
        await api_client.post("/v1/scores", headers=AUTH, json=body(score=18500))

        response = await api_client.post("/v1/scores", headers=AUTH, json=body(score=9000))

        assert response.status_code == 200
        standings = response.json()["standings"]
        assert not any(s["improved"] for s in standings)
        assert all(s["score"] == 18500 for s in standings)

    async def test_replay_is_idempotent_over_http(self, api_client: httpx.AsyncClient) -> None:
        await register(api_client)
        first = await api_client.post("/v1/scores", headers=AUTH, json=body(score=18500))
        second = await api_client.post("/v1/scores", headers=AUTH, json=body(score=18500))

        assert first.status_code == second.status_code == 200
        assert [s["score"] for s in second.json()["standings"]] == [18500] * 3
        assert not any(s["improved"] for s in second.json()["standings"])

    async def test_ranks_reflect_other_players(self, api_client: httpx.AsyncClient) -> None:
        await register(api_client)
        await api_client.post("/v1/scores", headers=AUTH, json=body("leader", 99_999))
        response = await api_client.post("/v1/scores", headers=AUTH, json=body("runner_up", 500))

        assert [s["rank"] for s in response.json()["standings"]] == [2, 2, 2]

    async def test_unregistered_game_returns_404(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.post("/v1/scores", headers=AUTH, json=body())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "GAME_NOT_FOUND"

    async def test_retired_game_returns_409(
        self, api_client: httpx.AsyncClient, db: object
    ) -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncEngine

        assert isinstance(db, AsyncEngine)
        await register(api_client)
        async with db.begin() as conn:
            await conn.execute(text("UPDATE games SET is_active = false"))

        response = await api_client.post("/v1/scores", headers=AUTH, json=body())

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "GAME_INACTIVE"


class TestValidation:
    @pytest.fixture(autouse=True)
    async def _game(self, api_client: httpx.AsyncClient) -> None:
        await register(api_client)

    @pytest.mark.parametrize(
        ("payload", "bad_field"),
        [
            ({"user_id": "", "game_id": "chess", "score": 1}, "user_id"),
            ({"user_id": "p:1", "game_id": "chess", "score": 1}, "user_id"),
            ({"user_id": "p1", "game_id": "Chess", "score": 1}, "game_id"),
            ({"user_id": "p1", "game_id": "chess", "score": -1}, "score"),
            ({"user_id": "p1", "game_id": "chess", "score": 10**13}, "score"),
            ({"user_id": "p1", "game_id": "chess", "score": 1.5}, "score"),
            ({"user_id": "p1", "game_id": "chess"}, "score"),
            ({"game_id": "chess", "score": 1}, "user_id"),
        ],
    )
    async def test_invalid_payloads_are_rejected_with_the_offending_field(
        self,
        api_client: httpx.AsyncClient,
        payload: dict[str, object],
        bad_field: str,
    ) -> None:
        response = await api_client.post("/v1/scores", headers=AUTH, json=payload)

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert bad_field in [detail["field"] for detail in error["details"]]

    async def test_client_supplied_achieved_at_is_rejected(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Backdating a score would win every tiebreak against honest players."""
        response = await api_client.post(
            "/v1/scores",
            headers=AUTH,
            json=body(achieved_at="2020-01-01T00:00:00Z"),
        )

        assert response.status_code == 422
        details = response.json()["error"]["details"]
        assert any(
            detail["field"] == "achieved_at" and detail["issue"] == "extra_forbidden"
            for detail in details
        )

    async def test_unknown_field_is_rejected_not_ignored(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """A silently dropped `user_ID` loses scores for a week before anyone notices."""
        response = await api_client.post("/v1/scores", headers=AUTH, json=body(user_ID="p9"))
        assert response.status_code == 422

    async def test_all_failures_are_reported_at_once(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.post(
            "/v1/scores",
            headers=AUTH,
            json={"user_id": "", "game_id": "chess", "score": -5, "nope": 1},
        )

        fields = {detail["field"] for detail in response.json()["error"]["details"]}
        assert {"user_id", "score", "nope"} <= fields

    async def test_malformed_json_is_400_not_422(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.post(
            "/v1/scores",
            headers={**AUTH, "Content-Type": "application/json"},
            content=b"{not json",
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "MALFORMED_JSON"

    async def test_blank_display_name_is_rejected(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.post("/v1/scores", headers=AUTH, json=body(display_name="   "))
        assert response.status_code == 422

    async def test_display_name_is_trimmed(self, api_client: httpx.AsyncClient) -> None:
        await api_client.post("/v1/scores", headers=AUTH, json=body(display_name="  Ayo  "))
        games = (await api_client.get("/v1/games")).json()
        assert games["games"][0]["id"] == "chess"


class TestDegradedWrites:
    async def test_submission_succeeds_with_no_rank_index(
        self, degraded_api_client: httpx.AsyncClient
    ) -> None:
        """Spec.md D2: no Redis is a serving state, not an outage."""
        await register(degraded_api_client)

        response = await degraded_api_client.post(
            "/v1/scores", headers=AUTH, json=body(score=18500)
        )

        assert response.status_code == 200
        standings = response.json()["standings"]
        assert all(s["score"] == 18500 for s in standings)
        assert all(s["improved"] for s in standings)
        # Rank is reported as unknown rather than fabricated.
        assert all(s["rank"] is None for s in standings)

    async def test_health_reports_degraded_with_no_stranded_outbox_work(
        self, degraded_api_client: httpx.AsyncClient
    ) -> None:
        """With no index configured, nothing is enqueued, so nothing is stranded.

        Reporting a permanently-rising backlog here would train an operator to
        ignore the one metric that signals real index drift.
        """
        await register(degraded_api_client)
        await degraded_api_client.post("/v1/scores", headers=AUTH, json=body())

        response = await degraded_api_client.get("/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "degraded"
        assert payload["outbox"]["pending"] == 0
        assert payload["outbox"]["oldest_pending_age_s"] is None


class TestGameRegistry:
    async def test_register_then_list(self, api_client: httpx.AsyncClient) -> None:
        await register(api_client, "chess")
        await register(api_client, "tetris")

        payload = (await api_client.get("/v1/games")).json()

        assert [game["id"] for game in payload["games"]] == ["chess", "tetris"]
        assert payload["games"][0]["periods"] == ["all_time", "daily", "weekly"]

    async def test_get_one_game(self, api_client: httpx.AsyncClient) -> None:
        await register(api_client)
        response = await api_client.get("/v1/games/chess")

        assert response.status_code == 200
        assert response.json()["name"] == "Chess"

    async def test_unknown_game_returns_game_not_found(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.get("/v1/games/nope")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "GAME_NOT_FOUND"

    async def test_registration_is_idempotent_over_http(
        self, api_client: httpx.AsyncClient
    ) -> None:
        await register(api_client)
        response = await api_client.post(
            "/v1/admin/games", headers=ADMIN, json={"id": "chess", "name": "Chess Deluxe"}
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Chess Deluxe"
        assert len((await api_client.get("/v1/games")).json()["games"]) == 1

    @pytest.mark.parametrize("bad_id", ["Chess", "-chess", "chess_1", "chess:blitz", ""])
    async def test_malformed_game_id_is_rejected(
        self, api_client: httpx.AsyncClient, bad_id: str
    ) -> None:
        response = await api_client.post(
            "/v1/admin/games", headers=ADMIN, json={"id": bad_id, "name": "X"}
        )
        assert response.status_code == 422
