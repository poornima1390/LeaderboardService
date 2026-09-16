"""Request-id propagation and the body size guard."""

from __future__ import annotations

import httpx
import pytest

from app.core.middleware import _sanitise_inbound_request_id, new_request_id


class TestRequestIdGeneration:
    def test_is_26_char_crockford_base32(self) -> None:
        request_id = new_request_id()
        assert len(request_id) == 26
        assert set(request_id) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

    def test_excludes_ambiguous_characters(self) -> None:
        """No I/L/O/U, so ids survive being transcribed from a screenshot."""
        alphabet = {ch for _ in range(200) for ch in new_request_id()}
        assert not (alphabet & set("ILOU"))

    def test_ids_are_unique(self) -> None:
        assert len({new_request_id() for _ in range(2000)}) == 2000


class TestInboundRequestIdSanitisation:
    """A caller-supplied id lands in our log stream, so it is untrusted input."""

    def test_accepts_a_well_formed_id(self) -> None:
        assert _sanitise_inbound_request_id("client-abc_123") == "client-abc_123"

    @pytest.mark.parametrize(
        "hostile",
        [
            None,
            "",
            "   ",
            "a" * 65,  # unbounded growth in every log line
            "id with spaces",
            'id","injected":"value',  # log/JSON injection attempt
            "id\nlevel=critical",  # forged extra log line
            "id\x00truncated",
        ],
    )
    def test_discards_anything_malformed(self, hostile: str | None) -> None:
        """Discarded, not cleaned: a half-sanitised id looks trustworthy."""
        assert _sanitise_inbound_request_id(hostile) is None

    def test_accepts_a_ulid_we_generated(self) -> None:
        generated = new_request_id()
        assert _sanitise_inbound_request_id(generated) == generated


class TestRequestIdPropagation:
    async def test_client_supplied_id_is_echoed_back(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health", headers={"X-Request-ID": "game-server-42"})
        assert response.headers["x-request-id"] == "game-server-42"

    async def test_hostile_id_is_replaced_not_echoed(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health", headers={"X-Request-ID": "bad id with spaces"})
        assert response.headers["x-request-id"] != "bad id with spaces"
        assert len(response.headers["x-request-id"]) == 26

    async def test_each_request_gets_a_distinct_id(self, client: httpx.AsyncClient) -> None:
        first = await client.get("/health")
        second = await client.get("/health")
        assert first.headers["x-request-id"] != second.headers["x-request-id"]


class TestBodySizeLimit:
    async def test_oversized_body_is_rejected_with_413(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/health", content=b"x" * (9 * 1024))

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

    async def test_rejection_happens_before_routing(self, client: httpx.AsyncClient) -> None:
        """413 must win over 404: we should not buffer a huge body to route it."""
        response = await client.post("/no/such/route", content=b"x" * (9 * 1024))
        assert response.status_code == 413

    async def test_413_still_carries_a_request_id(self, client: httpx.AsyncClient) -> None:
        """Regression: middleware order decides this.

        Starlette makes the last-registered middleware outermost. If the body
        guard is registered after the request-context middleware it runs first,
        and 413s come back with a null request_id and no access log line — the
        response you most want to be able to trace.
        """
        response = await client.post("/health", content=b"x" * (9 * 1024))

        assert response.status_code == 413
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
        assert len(response.headers["x-request-id"]) == 26

    async def test_invalid_content_length_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/health",
            content=b"{}",
            headers={"Content-Length": "not-a-number"},
        )
        assert response.status_code in (400, 413)
