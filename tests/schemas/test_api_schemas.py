"""API DTO schema tests."""
from __future__ import annotations

import pytest

from app.schemas.api import (
    ConduitDTO,
    CreateConduitInput,
    CreateScheduleInput,
    OpenPathInput,
    PriorFlow,
    RunTaskInput,
    RunTaskOutput,
    ScheduleConfig,
    ScheduledJob,
    UpdateConduitInput,
)


def test_create_conduit_input_round_trips_to_conduit_payload():
    """Verify CreateConduitInput accepts and dumps a payload in snake_case."""
    payload = {
        "name": "release_notes",
        "description": "Generate release notes",
        "tasks": [
            {
                "name": "echo",
                "description": "echo",
                "task": "echo hi",
                "tool": "tool:bash",
                "depends_on": [],
            }
        ],
    }
    dto = CreateConduitInput.model_validate(payload)
    assert dto.name == "release_notes"
    assert dto.tasks[0].name == "echo"
    # snake_case retained on dump
    dumped = dto.model_dump()
    assert "depends_on" in dumped["tasks"][0]


def test_update_conduit_input_allows_partial_update():
    """Verify UpdateConduitInput accepts partial updates with None defaults."""
    dto = UpdateConduitInput.model_validate({"description": "new"})
    assert dto.description == "new"
    assert dto.timeout is None


def test_conduit_dto_is_serializable():
    """Verify ConduitDTO accepts a full conduit definition."""
    dto = ConduitDTO.model_validate(
        {
            "name": "x",
            "description": "d",
            "timeout": 600,
            "max_concurrency": 4,
            "inputs": {"foo": "bar"},
            "tasks": [
                {
                    "name": "echo",
                    "description": "d",
                    "task": "echo",
                    "tool": "tool:bash",
                    "depends_on": [],
                }
            ],
        }
    )
    assert dto.name == "x"
    assert dto.max_concurrency == 4


def test_open_path_input_requires_conduit_and_run_path():
    """Verify OpenPathInput requires both conduit_name and run_path."""
    dto = OpenPathInput.model_validate(
        {"conduit_name": "release_notes", "run_path": "/abs/path"}
    )
    assert dto.conduit_name == "release_notes"
    assert dto.run_path == "/abs/path"
    with pytest.raises(Exception):
        OpenPathInput.model_validate({"conduit_name": "x"})


def test_run_task_input_validates_minimum_fields():
    """Verify RunTaskInput accepts the minimum required fields."""
    dto = RunTaskInput.model_validate(
        {
            "name": "echo",
            "description": "d",
            "task": "echo hello",
            "tool": "tool:bash",
            "run_path": "/tmp/x",
        }
    )
    assert dto.task == "echo hello"
    assert dto.run_path == "/tmp/x"


def test_run_task_output_carries_flow_id_and_logs():
    """Verify RunTaskOutput exposes flow_id and logs fields."""
    dto = RunTaskOutput.model_validate(
        {
            "flow_id": "x_aabbccdd_20260101T000000Z",
            "logs": [],
        }
    )
    assert dto.flow_id.startswith("x_")
    assert dto.logs == []


def test_schedule_config_recurring_validates_days_and_times():
    """Verify recurring ScheduleConfig accepts valid days and times."""
    sc = ScheduleConfig.model_validate(
        {
            "mode": "recurring",
            "name": "weekday mornings",
            "days": [1, 2, 3, 4, 5],
            "times": ["06:00", "12:00"],
        }
    )
    assert sc.mode == "recurring"
    assert sc.days == [1, 2, 3, 4, 5]
    assert sc.times == ["06:00", "12:00"]


def test_schedule_config_once_requires_run_at():
    """Verify once-mode ScheduleConfig accepts run_at."""
    sc = ScheduleConfig.model_validate(
        {"mode": "once", "name": "tomorrow", "run_at": "2026-05-01T08:00:00Z"}
    )
    assert sc.mode == "once"
    assert sc.run_at is not None


def test_schedule_config_recurring_rejects_bad_day():
    """Verify recurring ScheduleConfig rejects an out-of-range day."""
    with pytest.raises(Exception):
        ScheduleConfig.model_validate(
            {"mode": "recurring", "name": "x", "days": [0], "times": ["06:00"]}
        )


def test_schedule_config_recurring_rejects_bad_time():
    """Verify recurring ScheduleConfig rejects an unparsable time."""
    with pytest.raises(Exception):
        ScheduleConfig.model_validate(
            {"mode": "recurring", "name": "x", "days": [1], "times": ["6:00 AM"]}
        )


def test_create_schedule_input_assembles_full_payload():
    """Verify CreateScheduleInput accepts a full schedule payload."""
    csi = CreateScheduleInput.model_validate(
        {
            "conduit_name": "release_notes",
            "inputs": {"repo": "org/x"},
            "run_path": "/abs/path",
            "schedule": {
                "mode": "recurring",
                "name": "weekday mornings",
                "days": [1, 2, 3, 4, 5],
                "times": ["06:00", "12:00"],
            },
        }
    )
    assert csi.conduit_name == "release_notes"
    assert csi.schedule.times == ["06:00", "12:00"]


def test_scheduled_job_has_id_and_metadata():
    """Verify ScheduledJob exposes id and status metadata."""
    sj = ScheduledJob.model_validate(
        {
            "id": "SCH-12345678",
            "conduit_name": "release_notes",
            "inputs": {"repo": "org/x"},
            "run_path": "/abs/path",
            "schedule": {
                "mode": "recurring",
                "name": "weekday mornings",
                "days": [1, 2, 3, 4, 5],
                "times": ["06:00", "12:00"],
            },
            "created_at": 1745140800000,
            "status": "active",
            "runs_completed": 0,
        }
    )
    assert sj.id == "SCH-12345678"
    assert sj.status == "active"
    assert sj.runs_completed == 0


def test_scheduled_job_serializes_snake_case():
    """Verify ScheduledJob dumps fields in snake_case."""
    sj = ScheduledJob.model_validate(
        {
            "id": "SCH-1",
            "conduit_name": "x",
            "inputs": {},
            "run_path": "/abs",
            "schedule": {"mode": "once", "name": "n", "run_at": "2026-05-01T08:00:00Z"},
            "created_at": 1,
            "status": "active",
            "runs_completed": 0,
        }
    )
    dumped = sj.model_dump()
    assert "conduit_name" in dumped
    assert "runs_completed" in dumped
    assert "run_path" in dumped


def test_prior_flow_carries_minimum_fields():
    """Verify PriorFlow exposes flow_id and status fields."""
    pf = PriorFlow.model_validate(
        {
            "flow_id": "x_aabbccdd_20260101T000000Z",
            "conduit_name": "x",
            "started_at": "2026-01-01T00:00:00Z",
            "status": "completed",
        }
    )
    assert pf.flow_id.startswith("x_")
    assert pf.status == "completed"
