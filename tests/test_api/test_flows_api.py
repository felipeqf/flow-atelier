"""/flows REST tests."""
from __future__ import annotations

import httpx
import pytest

from app.core.atelier import Atelier
from app.schemas.api import RunTaskInput
from app.services.api.app import FastApiServer


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    """Yield an httpx client wired to a fresh Atelier instance.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.delenv("ATELIER_GLOBAL_ATELIER_DIR", raising=False)
    atelier = Atelier(base_dir=tmp_path / ".atelier")
    app = FastApiServer().create_app(atelier)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=15.0
    ) as c:
        yield c, atelier


async def _seed_flow(atelier: Atelier) -> str:
    """Run a one-off bash task to seed a completed flow.

    :param atelier: Atelier instance to run the task against.
    """
    out = await atelier.run_single_task(
        RunTaskInput(
            name="echo",
            description="d",
            task="echo flow-api-output",
            tool="tool:bash",
            run_path="/tmp",
        )
    )
    return out.flow_id


async def test_list_flows_empty(fixture):
    """Verify GET /flows returns an empty list when no flows have run.

    :param fixture: client+atelier tuple fixture.
    """
    client, _ = fixture
    resp = await client.get("/flows")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_flows_returns_completed(fixture):
    """Verify GET /flows includes the seeded completed flow.

    :param fixture: client+atelier tuple fixture.
    """
    client, atelier = fixture
    flow_id = await _seed_flow(atelier)
    resp = await client.get("/flows")
    assert resp.status_code == 200
    bodies = resp.json()
    assert any(item["flow_id"] == flow_id for item in bodies)
    target = next(item for item in bodies if item["flow_id"] == flow_id)
    assert target["status"] == "completed"


async def test_get_logs_round_trips(fixture):
    """Verify GET /flows/<id>/logs returns the recorded stdout entries.

    :param fixture: client+atelier tuple fixture.
    """
    client, atelier = fixture
    flow_id = await _seed_flow(atelier)
    resp = await client.get(f"/flows/{flow_id}/logs")
    assert resp.status_code == 200
    logs = resp.json()
    assert isinstance(logs, list)
    assert any(
        "flow-api-output" in (entry.get("stdout") or "") for entry in logs
    )


async def test_get_logs_unknown_returns_404(fixture):
    """Verify requesting logs for an unknown flow returns 404.

    :param fixture: client+atelier tuple fixture.
    """
    client, _ = fixture
    resp = await client.get("/flows/no_such_flow/logs")
    assert resp.status_code == 404
