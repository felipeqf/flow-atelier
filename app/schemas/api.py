"""Request/response DTOs for the FastAPI HTTP layer (snake_case).

Mirrors the SPEC §5–§6 frontend contract. Each DTO is a thin Pydantic v2
shape; complex validation lives on the underlying domain models
(:class:`Conduit`, :class:`TaskDefinition`, :class:`LogEntry`).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.conduit import Conduit, TaskDefinition
from app.schemas.log import LogEntry

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class CreateConduitInput(Conduit):
    """Body for ``POST /conduits``: a full conduit definition."""


class UpdateConduitInput(BaseModel):
    """Body for ``PATCH /conduits/:name``: partial update over a conduit."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    timeout: int | None = None
    max_concurrency: int | None = None
    inputs: dict[str, str] | None = None
    tasks: list[TaskDefinition] | None = None


class ConduitDTO(Conduit):
    """Response shape for conduit reads — identical to the on-disk model."""


class OpenPathInput(BaseModel):
    """Body for ``POST /conduits/open-path``: open a flow's run path."""

    model_config = ConfigDict(extra="forbid")

    conduit_name: str
    run_path: str


class RunTaskInput(BaseModel):
    """Body for ``POST /tasks/run``: an ad-hoc one-task conduit."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    task: str
    tool: str
    run_path: str


class RunTaskOutput(BaseModel):
    """Response shape for ``POST /tasks/run``."""

    flow_id: str
    logs: list[LogEntry] = Field(default_factory=list)


class ScheduleConfig(BaseModel):
    """Inline schedule configuration — recurring (days×times) or one-shot."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["recurring", "once"]
    name: str = ""
    days: list[int] | None = None
    times: list[str] | None = None
    run_at: datetime | None = None

    @field_validator("days")
    @classmethod
    def _validate_days(cls, v: list[int] | None) -> list[int] | None:
        """Ensure each entry in ``days`` is an ISO 8601 day-of-week (1..7).

        :param v: list of day numbers to validate, or ``None``.
        :returns: the validated list unchanged, or ``None`` if not provided.
        """
        if v is None:
            return v
        for d in v:
            if d < 1 or d > 7:
                raise ValueError(
                    f"day must be ISO 8601 day-of-week (1..7), got {d!r}"
                )
        return v

    @field_validator("times")
    @classmethod
    def _validate_times(cls, v: list[str] | None) -> list[str] | None:
        """Ensure each entry in ``times`` matches the 24h ``HH:mm`` format.

        :param v: list of time strings to validate, or ``None``.
        :returns: the validated list unchanged, or ``None`` if not provided.
        """
        if v is None:
            return v
        for t in v:
            if not _HHMM_RE.match(t):
                raise ValueError(
                    f"time must be 'HH:mm' 24h string, got {t!r}"
                )
        return v

    @field_validator("run_at", mode="before")
    @classmethod
    def _parse_iso(cls, v: Any) -> Any:
        """Parse an ISO 8601 string into a ``datetime``, accepting a trailing ``Z``.

        :param v: raw value; parsed when it is a string, otherwise returned as-is.
        :returns: a ``datetime`` when ``v`` is a valid ISO string, otherwise ``v`` unchanged.
        """
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError as e:
                raise ValueError(f"invalid ISO 8601 datetime: {v!r}") from e
        return v

    @model_validator(mode="after")
    def _check_mode_fields(self) -> ScheduleConfig:
        """Ensure required fields are present for the selected ``mode``.

        :returns: the validated ``ScheduleConfig`` instance.
        """
        if self.mode == "recurring":
            if not self.days:
                raise ValueError("recurring schedule requires at least one day")
            if not self.times:
                raise ValueError("recurring schedule requires at least one time")
        else:  # once
            if self.run_at is None:
                raise ValueError("once schedule requires run_at")
        return self


class CreateScheduleInput(BaseModel):
    """Body for ``POST /schedules``."""

    model_config = ConfigDict(extra="forbid")

    conduit_name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    run_path: str
    schedule: ScheduleConfig


class ScheduledJob(BaseModel):
    """Server-side representation of a persisted schedule (per SPEC §7)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    conduit_name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    run_path: str
    schedule: ScheduleConfig
    created_at: int
    status: Literal["active", "deleted"] = "active"
    runs_completed: int = 0


class PriorFlow(BaseModel):
    """List entry for ``GET /flows``."""

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    conduit_name: str
    started_at: str | None = None
    finished_at: str | None = None
    status: str
