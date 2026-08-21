"""API smoke tests against a real database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.conftest import at, trade
from tradepnl.api import create_app
from tradepnl.load import load_trades
from tradepnl.pnl import compute_daily_pnl


@pytest.fixture()
def client(seeded_instruments: Engine) -> TestClient:
    load_trades(
        seeded_instruments,
        [
            trade("B1", side="BUY", quantity=100, price="10", executed_at=at(1)),
            trade("S1", side="SELL", quantity=40, price="18", executed_at=at(4)),
            trade("BAD", price=""),
        ],
        incremental=False,
    )
    compute_daily_pnl(seeded_instruments)
    return TestClient(create_app(seeded_instruments))


def test_health_reports_ok_when_the_database_is_reachable(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_instruments_lists_the_dimension(client: TestClient) -> None:
    payload = client.get("/instruments").json()
    assert {row["symbol"] for row in payload} == {"AAPL", "MSFT", "EURUSD"}


def test_running_pnl_can_be_filtered_by_symbol(client: TestClient) -> None:
    payload = client.get("/pnl/running", params={"symbol": "AAPL"}).json()
    assert payload
    assert {row["symbol"] for row in payload} == {"AAPL"}
    assert any(row["cumulative_realised_pnl"] > 0 for row in payload)


def test_top_endpoint_is_ranked(client: TestClient) -> None:
    payload = client.get("/pnl/top", params={"top_n": 5}).json()
    assert payload
    assert payload[0]["pnl_rank"] == 1


def test_stats_separates_valid_rows_from_landed_rows(client: TestClient) -> None:
    """The gap between the two counts is exactly what the data quality project reports."""
    payload = client.get("/stats").json()
    assert payload["trade_rows"] == 3
    assert payload["valid_trade_rows"] == 2
