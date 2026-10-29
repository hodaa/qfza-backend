"""Integration tests for the internal database benchmark endpoint."""

import pytest
from httpx import AsyncClient

from app.core.config import get_settings

settings = get_settings()

_HEADERS = {"X-Internal-API-Key": settings.internal_api_key}


class TestBenchmarkAuth:
    """Tests for internal API key enforcement on the benchmark route."""

    @pytest.mark.asyncio
    async def test_rejects_missing_key(self, client: AsyncClient) -> None:
        response = await client.get("/internal/benchmark/db")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_wrong_key(self, client: AsyncClient) -> None:
        response = await client.get(
            "/internal/benchmark/db",
            headers={"X-Internal-API-Key": "wrong"},
        )

        assert response.status_code == 401


class TestDatabaseBenchmark:
    """Tests for GET /internal/benchmark/db."""

    @pytest.mark.asyncio
    async def test_returns_latency_and_database_metadata(self, client: AsyncClient) -> None:
        response = await client.get("/internal/benchmark/db?samples=3", headers=_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["samples"] == 3
        assert len(data["latency"]["samples_ms"]) == 3
        assert data["latency"]["connect_ms"] >= 0
        assert data["latency"]["first_query_ms"] >= 0
        assert data["latency"]["min_ms"] <= data["latency"]["median_ms"]
        assert data["latency"]["median_ms"] <= data["latency"]["p95_ms"]
        assert data["database"]["server_version"].startswith("PostgreSQL")
        assert data["backend"]["timestamp"]
        assert response.headers["cache-control"] == "no-store, max-age=0"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("samples", [0, 11])
    async def test_rejects_invalid_sample_count(self, client: AsyncClient, samples: int) -> None:
        response = await client.get(f"/internal/benchmark/db?samples={samples}", headers=_HEADERS)

        assert response.status_code == 422
