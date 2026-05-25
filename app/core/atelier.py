"""Facade: wires store + executors + engine and exposes the public API."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.core.settings import AtelierSettings
from app.modules.engine import Engine, FlowStartedCallback, TaskEventCallback, TaskStartingCallback
from app.schemas.api import (
    CreateConduitInput,
    CreateScheduleInput,
    PriorFlow,
    RunTaskInput,
    RunTaskOutput,
    ScheduledJob,
    UpdateConduitInput,
)
from app.schemas.conduit import Conduit
from app.schemas.flow import parse_flow_id
from app.schemas.log import LogEntry
from app.schemas.progress import Progress
from app.services.executor.bash import BashExecutor
from app.services.executor.conduit import ConduitExecutor
from app.services.executor.harness import (
    ClaudeHarness,
    CodexHarness,
    CopilotHarness,
    CursorHarness,
    OpencodeHarness,
)
from app.services.executor.hitl import HitlExecutor
from app.services.executor.prompt_sink import PromptSink, TerminalPromptSink
from app.services.scheduler.store import ScheduleStore
from app.services.store.filesystem import FilesystemStore

logger = logging.getLogger(__name__)


class Atelier:
    """Top-level facade for the flow-atelier engine.

    Wires :class:`FilesystemStore`, the tool/harness executors, and the DAG
    :class:`Engine` together and exposes the public API used by the CLI.

    :param settings: explicit :class:`AtelierSettings`; if omitted, loads
        from environment / ``.env``
    :param base_dir: convenience override for ``settings.atelier_dir``;
        ignored when ``settings`` is passed explicitly
    """

    def __init__(
        self,
        settings: AtelierSettings | None = None,
        base_dir: Path | str | None = None,
        prompt_sink: PromptSink | None = None,
    ) -> None:
        """Construct the facade, wiring store, executors, engine and schedule store.

        :param settings: explicit :class:`AtelierSettings`; if omitted, loads
            from environment / ``.env``.
        :param base_dir: convenience override for ``settings.atelier_dir``;
            ignored when ``settings`` is passed explicitly.
        :param prompt_sink: optional :class:`PromptSink` shared by harness
            executors; defaults to :class:`TerminalPromptSink`.
        """
        if settings is None:
            settings = (
                AtelierSettings(atelier_dir=Path(base_dir))
                if base_dir is not None
                else AtelierSettings()
            )
        self.settings = settings
        self.store = FilesystemStore(
            self.settings.atelier_dir,
            global_dir=self.settings.global_atelier_dir,
        )
        sink: PromptSink = prompt_sink if prompt_sink is not None else TerminalPromptSink()
        claude_launch = (
            self.settings.claude_launch_cmd or None
        )
        codex_launch = self.settings.codex_launch_cmd or None
        opencode_launch = self.settings.opencode_launch_cmd or None
        copilot_launch = self.settings.copilot_launch_cmd or None
        cursor_launch = self.settings.cursor_launch_cmd or None
        self.executors = {
            "tool:bash": BashExecutor(),
            "tool:hitl": HitlExecutor(),
            "tool:conduit": ConduitExecutor(),
            "harness:claude-code": ClaudeHarness(
                sink=sink,
                launch_cmd=claude_launch,
                done_marker=self.settings.done_marker,
            ),
            "harness:codex": CodexHarness(
                sink=sink,
                launch_cmd=codex_launch,
                done_marker=self.settings.done_marker,
            ),
            "harness:opencode": OpencodeHarness(
                sink=sink,
                launch_cmd=opencode_launch,
                done_marker=self.settings.done_marker,
            ),
            "harness:copilot": CopilotHarness(
                sink=sink,
                launch_cmd=copilot_launch,
                done_marker=self.settings.done_marker,
            ),
            "harness:cursor": CursorHarness(
                sink=sink,
                launch_cmd=cursor_launch,
                done_marker=self.settings.done_marker,
            ),
        }
        self.engine = Engine(self.executors, self.store)
        self.schedule_store = ScheduleStore(self.settings.atelier_dir)

    async def run_conduit(
        self,
        name: str,
        inputs: dict[str, Any],
        on_task_event: TaskEventCallback | None = None,
        on_flow_started: FlowStartedCallback | None = None,
        on_task_starting: TaskStartingCallback | None = None,
        show_steps: bool = True,
        working_dir: Path | str | None = None,
    ) -> str:
        """Start a new flow for the named conduit.

        :param name: conduit name (must match a folder under ``conduits/``)
        :param inputs: conduit input map, keyed by input name
        :param on_task_event: optional callback invoked with a
            :class:`TaskEvent` after every task iteration finishes (success
            or failure). Exceptions raised by the callback are logged but
            do not affect the flow.
        :param on_flow_started: optional callback invoked once with the
            new flow id, before any task runs. Lets the caller record the
            id and surface it on failure as well as on success.
        :param on_task_starting: optional callback invoked with
            ``(task_name, tool)`` when a task begins its first iteration.
        :param show_steps: stream intermediate harness steps (thinking,
            tool calls, tool results) to the executor's prompt sink as
            they happen. Defaults to ``True``; the CLI exposes
            ``--hide-steps`` to opt out.
        :param working_dir: working directory for task execution. When
            ``None``, executors use the process cwd.
        :returns: the newly created flow id
        """
        wd = Path(working_dir) if working_dir is not None else None
        conduit = self.store.read_conduit(name)
        return await self.engine.run(
            conduit,
            inputs,
            on_task_event=on_task_event,
            on_flow_started=on_flow_started,
            on_task_starting=on_task_starting,
            show_steps=show_steps,
            working_dir=wd,
        )

    def get_status(self, flow_id: str) -> Progress:
        """Return the latest :class:`Progress` snapshot for ``flow_id``.

        :param flow_id: flow identifier
        :returns: current progress snapshot
        """
        return self.store.read_progress(flow_id)

    def list_conduits(self) -> list[str]:
        """List all available conduit names.

        :returns: sorted list of conduit names
        """
        return self.store.list_conduits()

    def list_flows(self, conduit_name: str | None = None) -> list[str]:
        """List flow ids, optionally filtered by conduit.

        :param conduit_name: restrict to flows of this conduit
        :returns: sorted list of flow ids
        """
        return self.store.list_flows(conduit_name)

    # ------------------------------------------------------------------ CRUD

    def create_conduit(self, payload: CreateConduitInput) -> Conduit:
        """Persist a new conduit; raise if one with that name already exists.

        :param payload: validated :class:`CreateConduitInput`
        :returns: the persisted :class:`Conduit`
        :raises FileExistsError: if a project conduit with that name exists
        """
        existing = self.store.base_dir / "conduits" / payload.name
        if existing.exists():
            raise FileExistsError(f"conduit already exists: {payload.name}")
        conduit = Conduit.model_validate(payload.model_dump())
        self.store.write_conduit(conduit)
        return conduit

    def update_conduit(
        self, name: str, payload: UpdateConduitInput
    ) -> Conduit:
        """Apply a partial update to an existing conduit.

        :param name: conduit to modify
        :param payload: subset of fields to overwrite
        :returns: the updated :class:`Conduit`
        :raises FileNotFoundError: if the conduit doesn't exist
        """
        existing = self.store.read_conduit(name)
        merged = existing.model_dump()
        for key, value in payload.model_dump(exclude_none=True).items():
            merged[key] = value
        merged["name"] = merged.get("name") or name
        updated = Conduit.model_validate(merged)
        self.store.write_conduit(updated)
        if updated.name != name:
            # Rename: drop the old folder.
            self.store.delete_conduit(name)
        return updated

    def delete_conduit(self, name: str) -> bool:
        """Remove a project-level conduit.

        :param name: conduit name
        :returns: True if it existed and was deleted, False otherwise
        """
        return self.store.delete_conduit(name)

    async def run_single_task(self, payload: RunTaskInput) -> RunTaskOutput:
        """Run an ad-hoc one-task conduit and return the resulting logs.

        :param payload: validated :class:`RunTaskInput`
        :returns: :class:`RunTaskOutput` carrying the flow id and logs
        """
        conduit = Conduit.model_validate(
            {
                "name": f"task__{payload.name}",
                "description": payload.description or payload.name,
                "tasks": [
                    {
                        "name": payload.name,
                        "description": payload.description or payload.name,
                        "task": payload.task,
                        "tool": payload.tool,
                        "depends_on": [],
                    }
                ],
            }
        )
        captured: dict[str, str | None] = {"id": None}

        def _on_started(fid: str) -> None:
            """Capture the flow id emitted by the engine before tasks run.

            :param fid: flow id assigned by the engine.
            """
            captured["id"] = fid

        try:
            flow_id = await self.engine.run(
                conduit,
                {},
                on_flow_started=_on_started,
                working_dir=Path(payload.run_path) if payload.run_path else None,
            )
        except Exception:  # noqa: BLE001
            flow_id = captured["id"] or ""
        logs = self.store.read_logs(flow_id) if flow_id else []
        return RunTaskOutput(flow_id=flow_id, logs=logs)

    # ------------------------------------------------------------------ schedules

    def list_schedules(self) -> list[ScheduledJob]:
        """Return active schedules persisted by this Atelier."""
        return self.schedule_store.list()

    def create_schedule(self, payload: CreateScheduleInput) -> ScheduledJob:
        """Persist a new schedule and return it.

        :param payload: validated :class:`CreateScheduleInput`
        :returns: the new :class:`ScheduledJob`
        """
        return self.schedule_store.create(payload)

    def delete_schedule(self, schedule_id: str) -> ScheduledJob:
        """Soft-delete a schedule by id.

        :param schedule_id: schedule identifier
        :returns: the soft-deleted :class:`ScheduledJob`
        :raises KeyError: if the schedule doesn't exist
        """
        return self.schedule_store.delete(schedule_id)

    # ------------------------------------------------------------------ history

    def list_prior_flows(self) -> list[PriorFlow]:
        """Return :class:`PriorFlow` summaries for every flow on disk."""
        out: list[PriorFlow] = []
        for flow_id in self.store.list_flows():
            try:
                conduit_name, _, _ = parse_flow_id(flow_id)
            except ValueError:
                continue
            try:
                progress = self.store.read_progress(flow_id)
                status = progress.status.value
                started_at = progress.started_at
                finished_at = progress.finished_at
            except (FileNotFoundError, ValueError):
                status = "unknown"
                started_at = None
                finished_at = None
            out.append(
                PriorFlow(
                    flow_id=flow_id,
                    conduit_name=conduit_name,
                    started_at=started_at,
                    finished_at=finished_at,
                    status=status,
                )
            )
        return out

    def get_flow_logs(self, flow_id: str) -> list[LogEntry]:
        """Return the log entries for ``flow_id``.

        :param flow_id: flow identifier
        :returns: list of :class:`LogEntry` (empty if the file is empty)
        :raises FileNotFoundError: if no flow with that id exists
        """
        # ``_flow_dir`` raises FileNotFoundError when the id is unknown.
        self.store._flow_dir(flow_id)
        return self.store.read_logs(flow_id)

    def open_conduit_path(self, run_path: str) -> bool:
        """Reveal ``run_path`` in the host's file explorer.

        :param run_path: absolute path to open
        :returns: True if the platform opener was launched, False otherwise
        """
        target = str(Path(run_path))
        cmd: list[str]
        if sys.platform == "darwin":
            cmd = ["open", target]
        elif sys.platform == "win32":
            cmd = ["explorer", target]
        else:
            cmd = ["xdg-open", target]
        try:
            subprocess.Popen(cmd)
        except (FileNotFoundError, OSError) as e:
            logger.warning("open_conduit_path failed: %s", e)
            return False
        return True
        return target
