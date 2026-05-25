"""Atelier facade: run_single_task (ad-hoc one-task conduit)."""
from __future__ import annotations

import pytest

from app.core.atelier import Atelier
from app.schemas.api import RunTaskInput, RunTaskOutput


@pytest.fixture
def atelier(tmp_path, monkeypatch):
    """Construct an Atelier instance rooted under tmp_path.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.delenv("ATELIER_GLOBAL_ATELIER_DIR", raising=False)
    return Atelier(base_dir=tmp_path / ".atelier")


async def test_run_single_task_runs_bash_echo_and_returns_logs(atelier):
    """Verify run_single_task runs bash echo and returns logs with exit 0.

    :param atelier: Atelier facade fixture.
    """
    payload = RunTaskInput(
        name="echo",
        description="ad-hoc echo",
        task="echo hello-from-task",
        tool="tool:bash",
        run_path="/tmp",
    )
    out = await atelier.run_single_task(payload)
    assert isinstance(out, RunTaskOutput)
    assert out.flow_id
    assert out.logs
    assert any("hello-from-task" in (e.stdout or "") for e in out.logs)
    assert out.logs[-1].exit_code == 0


async def test_run_single_task_persists_flow_dir(atelier):
    """Verify run_single_task writes the flow's logs.json to disk.

    :param atelier: Atelier facade fixture.
    """
    payload = RunTaskInput(
        name="echo",
        description="ad-hoc echo",
        task="echo persisted",
        tool="tool:bash",
        run_path="/tmp",
    )
    out = await atelier.run_single_task(payload)
    flow_dir = atelier.store._flow_dir(out.flow_id)
    assert (flow_dir / "logs.json").exists()


async def test_run_single_task_failure_returns_non_zero_exit(atelier):
    """Verify run_single_task returns a non-zero exit code on failure.

    :param atelier: Atelier facade fixture.
    """
    payload = RunTaskInput(
        name="boom",
        description="explode",
        task="exit 7",
        tool="tool:bash",
        run_path="/tmp",
    )
    out = await atelier.run_single_task(payload)
    assert out.flow_id
    assert out.logs
    assert out.logs[-1].exit_code != 0
