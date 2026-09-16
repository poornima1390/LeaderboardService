"""The read endpoints, end to end over HTTP (Spec.md §4, §5, §10)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": "7f3c91ab5e2d4806bc1f9a73de50c284"}
ADMIN = {"X-Admin-Key": "d4a82fe07c6b13594ea2f8db60371cae"}

# A deliberate tie at 98110. Byte-wise DESC puts player_77 ahead of
# player_1204 ('7' > '1'), which is the counter-intuitive case D3 exists to
# pin down.
ROSTER = [
    ("player_99", 99820, "Mio"),
    ("player_1204", 98110, "Sam"),
    ("player_77", 98110, None),
    ("player_5", 50000, "Ada"),
    ("player_12", 31000, None),
    ("player_300", 12000, "Bo"),
    ("player_7", 900, "Kai"),
]


async def seed(client: httpx.AsyncClient, roster: list | None = None) -> None:  # type: ignore[type-arg]
    await client.post("/v1/admin/games", headers=ADMIN, json={"id": "chess", "name": "Chess"})
    for user_id, score, name in roster if roster is not None else ROSTER:
        payload: dict[str, object] = {
            "user_id": user_id,
            "game_id": "chess",
            "score": score,
        }
        if name:
            payload["display_name"] = name
        response = await client.post("/v1/scores", headers=AUTH, json=payload)
        assert response.status_code == 200, response.text


class TestTopX:
    async def test_returns_highest_scores_first(self, api_client: httpx.AsyncClient) -> None:
        await seed(api_client)

        response = await api_client.get("/v1/games/chess/leaderboard?limit=4")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total_entries"] == 7
        assert payload["source"] == "redis"
        assert [entry["user_id"] for entry in payload["entries"]] == [
            "player_99",
            "player_77",
            "player_1204",
            "player_5",
        ]
        assert [entry["rank"] for entry in payload["entries"]] == [1, 2, 3, 4]

    async def test_ties_break_by_byte_wise_descending_user_id(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """player_77 beats player_1204 at equal scores: '7' > '1' byte-wise.

        Counter-intuitive on two counts — the shorter string and the smaller
        numeric suffix win — which is exactly why it is asserted.
        """
        await seed(api_client)

        entries = (await api_client.get("/v1/games/chess/leaderboard")).json()["entries"]
        tied = [e for e in entries if e["score"] == 98110]

        assert [e["user_id"] for e in tied] == ["player_77", "player_1204"]
        assert [e["rank"] for e in tied] == [2, 3]

    async def test_display_name_is_hydrated_from_postgres(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Redis holds only (member, score); names come from Postgres."""
        await seed(api_client)

        entries = (await api_client.get("/v1/games/chess/leaderboard")).json()["entries"]
        by_id = {entry["user_id"]: entry for entry in entries}

        assert by_id["player_99"]["display_name"] == "Mio"
        assert by_id["player_77"]["display_name"] is None
        assert by_id["player_99"]["achieved_at"]

    async def test_pagination_continues_the_ranking(self, api_client: httpx.AsyncClient) -> None:
        await seed(api_client)

        first = (await api_client.get("/v1/games/chess/leaderboard?limit=3")).json()
        second = (await api_client.get("/v1/games/chess/leaderboard?limit=3&offset=3")).json()

        assert [e["rank"] for e in first["entries"]] == [1, 2, 3]
        assert [e["rank"] for e in second["entries"]] == [4, 5, 6]
        assert second["total_entries"] == first["total_entries"] == 7

    async def test_offset_past_the_end_is_empty_not_an_error(
        self, api_client: httpx.AsyncClient
    ) -> None:
        await seed(api_client)

        payload = (await api_client.get("/v1/games/chess/leaderboard?offset=500")).json()

        assert payload["entries"] == []
        assert payload["total_entries"] == 7

    async def test_empty_board_returns_200_not_404(self, api_client: httpx.AsyncClient) -> None:
        """ "No scores yet" is a valid leaderboard, not a missing resource."""
        await api_client.post(
            "/v1/admin/games", headers=ADMIN, json={"id": "chess", "name": "Chess"}
        )

        response = await api_client.get("/v1/games/chess/leaderboard")

        assert response.status_code == 200
        assert response.json()["total_entries"] == 0
        assert response.json()["entries"] == []

    async def test_periods_are_independent_boards(self, api_client: httpx.AsyncClient) -> None:
        await seed(api_client)

        for period in ("all_time", "daily", "weekly"):
            payload = (await api_client.get(f"/v1/games/chess/leaderboard?period={period}")).json()
            assert payload["period"] == period
            assert payload["total_entries"] == 7

    async def test_unknown_game_returns_game_not_found(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.get("/v1/games/nope/leaderboard")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "GAME_NOT_FOUND"

    async def test_retired_game_is_still_readable(
        self, api_client: httpx.AsyncClient, db: AsyncEngine
    ) -> None:
        """Only writes are refused; existing boards stay readable."""
        await seed(api_client)
        async with db.begin() as conn:
            await conn.execute(text("UPDATE games SET is_active = false"))

        response = await api_client.get("/v1/games/chess/leaderboard")

        assert response.status_code == 200
        assert response.json()["total_entries"] == 7


class TestUserContext:
    async def test_returns_rank_and_surroundings(self, api_client: httpx.AsyncClient) -> None:
        await seed(api_client)

        response = await api_client.get("/v1/games/chess/users/player_12/rank?window=2")

        assert response.status_code == 200
        payload = response.json()
        assert payload["user"]["rank"] == 5
        assert payload["total_entries"] == 7
        assert [e["user_id"] for e in payload["above"]] == ["player_1204", "player_5"]
        assert [e["user_id"] for e in payload["below"]] == ["player_300", "player_7"]
        assert [e["rank"] for e in payload["above"]] == [3, 4]
        assert [e["rank"] for e in payload["below"]] == [6, 7]

    async def test_top_ranked_user_has_empty_above(self, api_client: httpx.AsyncClient) -> None:
        """Normal output at a boundary, not an error."""
        await seed(api_client)

        payload = (await api_client.get("/v1/games/chess/users/player_99/rank?window=2")).json()

        assert payload["user"]["rank"] == 1
        assert payload["above"] == []
        assert [e["user_id"] for e in payload["below"]] == ["player_77", "player_1204"]
        assert payload["percentile"] == 100.0

    async def test_last_ranked_user_has_empty_below(self, api_client: httpx.AsyncClient) -> None:
        await seed(api_client)

        payload = (await api_client.get("/v1/games/chess/users/player_7/rank?window=2")).json()

        assert payload["user"]["rank"] == 7
        assert payload["below"] == []
        assert len(payload["above"]) == 2

    async def test_window_zero_returns_only_the_user(self, api_client: httpx.AsyncClient) -> None:
        """One endpoint answers both 'my rank' and 'my surroundings'."""
        await seed(api_client)

        payload = (await api_client.get("/v1/games/chess/users/player_5/rank?window=0")).json()

        assert payload["user"]["rank"] == 4
        assert payload["above"] == []
        assert payload["below"] == []

    async def test_window_larger_than_board_is_truncated(
        self, api_client: httpx.AsyncClient
    ) -> None:
        await seed(api_client)

        payload = (await api_client.get("/v1/games/chess/users/player_5/rank?window=25")).json()

        assert len(payload["above"]) == 3
        assert len(payload["below"]) == 3

    async def test_percentile_is_reported(self, api_client: httpx.AsyncClient) -> None:
        await seed(api_client)

        payload = (await api_client.get("/v1/games/chess/users/player_12/rank")).json()

        # rank 5 of 7 -> (1 - 4/7) * 100
        assert payload["percentile"] == 42.86

    async def test_unranked_user_is_distinct_from_unknown_game(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """The remedies differ entirely: 'check your slug' vs 'no score yet'."""
        await seed(api_client)

        unranked = await api_client.get("/v1/games/chess/users/ghost/rank")
        unknown_game = await api_client.get("/v1/games/nope/users/player_99/rank")

        assert unranked.status_code == unknown_game.status_code == 404
        assert unranked.json()["error"]["code"] == "USER_NOT_RANKED"
        assert unknown_game.json()["error"]["code"] == "GAME_NOT_FOUND"

    async def test_user_ranked_all_time_but_not_on_a_past_daily_board(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """A real consequence of per-period boards."""
        await seed(api_client)

        response = await api_client.get(
            "/v1/games/chess/users/player_99/rank?period=daily&bucket=2020-01-01"
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "USER_NOT_RANKED"


class TestReadValidation:
    @pytest.fixture(autouse=True)
    async def _seeded(self, api_client: httpx.AsyncClient) -> None:
        await seed(api_client)

    @pytest.mark.parametrize(
        "query",
        [
            "limit=0",
            "limit=101",
            "limit=-1",
            "limit=abc",
            "offset=-1",
            "offset=10001",
            "period=yearly",
            "period=",
        ],
    )
    async def test_invalid_leaderboard_params_are_rejected(
        self, api_client: httpx.AsyncClient, query: str
    ) -> None:
        """Rejected, not clamped: silently returning 100 rows for limit=10000
        teaches a client its request worked."""
        response = await api_client.get(f"/v1/games/chess/leaderboard?{query}")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("query", ["window=-1", "window=26", "window=abc"])
    async def test_invalid_window_is_rejected(
        self, api_client: httpx.AsyncClient, query: str
    ) -> None:
        response = await api_client.get(f"/v1/games/chess/users/player_99/rank?{query}")
        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("period", "bucket"),
        [
            ("daily", "2026-W38"),  # right shape, wrong period
            ("weekly", "2026-09-16"),
            ("all_time", "2026-09-16"),
            ("daily", "2026-02-30"),  # matches the regex, not a real date
            ("weekly", "2025-W53"),  # 2025 has only 52 ISO weeks
            ("daily", "nonsense"),
        ],
    )
    async def test_mismatched_period_and_bucket_is_422_not_an_empty_board(
        self, api_client: httpx.AsyncClient, period: str, bucket: str
    ) -> None:
        """Answering with an empty leaderboard would look like 'no scores yet'
        rather than 'you asked the wrong question'."""
        response = await api_client.get(
            f"/v1/games/chess/leaderboard?period={period}&bucket={bucket}"
        )

        assert response.status_code == 422
        details = response.json()["error"]["details"]
        assert any(detail["field"] == "bucket" for detail in details)

    async def test_future_bucket_is_rejected(self, api_client: httpx.AsyncClient) -> None:
        """Always a client mistake; an empty answer would hide it."""
        response = await api_client.get(
            "/v1/games/chess/leaderboard?period=daily&bucket=2099-01-01"
        )

        assert response.status_code == 422
        assert "future" in response.json()["error"]["message"]

    async def test_past_bucket_is_accepted_and_empty(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.get(
            "/v1/games/chess/leaderboard?period=daily&bucket=2020-01-01"
        )

        assert response.status_code == 200
        assert response.json()["total_entries"] == 0

    @pytest.mark.parametrize("user_id", ["p:1", "p/1", "p 1", "u" * 65])
    async def test_malformed_user_id_in_path_is_rejected(
        self, api_client: httpx.AsyncClient, user_id: str
    ) -> None:
        response = await api_client.get(f"/v1/games/chess/users/{user_id}/rank")
        assert response.status_code in (404, 422)


class TestColdIndexFallback:
    """Spec.md §10: a cold key must never produce a silently empty board.

    This is the worst failure the service can have, because a 200 with an
    empty list looks exactly like a legitimate answer.
    """

    async def test_flushed_index_still_serves_correct_results(
        self, api_client: httpx.AsyncClient, redis_client: Redis
    ) -> None:
        await seed(api_client)
        expected = (await api_client.get("/v1/games/chess/leaderboard")).json()
        assert expected["source"] == "redis"

        await redis_client.flushdb()

        response = await api_client.get("/v1/games/chess/leaderboard")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total_entries"] == 7, "must not report an empty board"
        assert [e["user_id"] for e in payload["entries"]] == [
            e["user_id"] for e in expected["entries"]
        ]
        assert payload["source"] == "postgres", "a degraded read must be visible"

    async def test_flushed_index_still_serves_user_context(
        self, api_client: httpx.AsyncClient, redis_client: Redis
    ) -> None:
        await seed(api_client)
        expected = (await api_client.get("/v1/games/chess/users/player_12/rank?window=2")).json()

        await redis_client.flushdb()
        payload = (await api_client.get("/v1/games/chess/users/player_12/rank?window=2")).json()

        assert payload["source"] == "postgres"
        assert payload["user"]["rank"] == expected["user"]["rank"]
        assert [e["user_id"] for e in payload["above"]] == [e["user_id"] for e in expected["above"]]
        assert [e["user_id"] for e in payload["below"]] == [e["user_id"] for e in expected["below"]]

    async def test_cold_read_self_heals_the_index(
        self, api_client: httpx.AsyncClient, redis_client: Redis
    ) -> None:
        """A cold read serves from Postgres and rebuilds that board behind it,
        so the next read is fast again without operator intervention."""
        await seed(api_client)
        await redis_client.flushdb()

        first = await api_client.get("/v1/games/chess/leaderboard")
        assert first.json()["source"] == "postgres"

        deadline = asyncio.get_running_loop().time() + 5
        while await redis_client.zcard("lb:chess:all_time:ALL") == 0:
            assert asyncio.get_running_loop().time() < deadline, "index never rebuilt"
            await asyncio.sleep(0.05)

        second = await api_client.get("/v1/games/chess/leaderboard")
        assert second.json()["source"] == "redis"
        assert [e["user_id"] for e in second.json()["entries"]] == [
            e["user_id"] for e in first.json()["entries"]
        ]

    async def test_genuinely_empty_board_is_not_treated_as_cold(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """The other side of the check: an empty board must still answer from
        the index rather than permanently falling back."""
        await api_client.post(
            "/v1/admin/games", headers=ADMIN, json={"id": "chess", "name": "Chess"}
        )

        payload = (await api_client.get("/v1/games/chess/leaderboard")).json()

        assert payload["total_entries"] == 0
        assert payload["source"] == "redis"

    async def test_reads_work_with_no_index_configured(
        self, degraded_api_client: httpx.AsyncClient
    ) -> None:
        await seed(degraded_api_client)

        payload = (await degraded_api_client.get("/v1/games/chess/leaderboard")).json()

        assert payload["source"] == "postgres"
        assert payload["total_entries"] == 7
        assert [e["user_id"] for e in payload["entries"]][:3] == [
            "player_99",
            "player_77",
            "player_1204",
        ]

    async def test_user_context_works_with_no_index_configured(
        self, degraded_api_client: httpx.AsyncClient
    ) -> None:
        await seed(degraded_api_client)

        payload = (
            await degraded_api_client.get("/v1/games/chess/users/player_12/rank?window=1")
        ).json()

        assert payload["source"] == "postgres"
        assert payload["user"]["rank"] == 5
        assert [e["user_id"] for e in payload["above"]] == ["player_5"]
        assert [e["user_id"] for e in payload["below"]] == ["player_300"]
