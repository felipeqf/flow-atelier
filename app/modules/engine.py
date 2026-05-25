"""Async DAG engine.

Given a parsed Conduit and inputs, the engine:

1. Validates the DAG (unknown deps, cycles, invalid regex).
2. Creates a flow via the store.
3. Runs tasks concurrently. All tasks whose deps are satisfied are launched
   in parallel (bounded by ``conduit.max_concurrency``).
4. Handles ``repeat``, fail-fast, per-task timeout, skip propagation via
   conditional dependencies and templating SkipSignals.

The engine is pure business logic: it holds no knowledge of filesystems or
CLIs beyond the ``StoreBase`` / ``ExecutorBase`` interfaces it is given.
"""
from __future__ import annotations

import asyncio
import sys
import traceback
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.modules.conditions import (
    DependencyParseError,
    evaluate,
    evaluate_loop_predicate,
    parse_dependencies,
    parse_output_predicate,
)
from app.modules.templating import SkipSignal, TemplateError, resolve
from app.schemas.conduit import Conduit, TaskDefinition, ToolType
from app.schemas.log import ExecutionResult, LogEntry, TaskEvent

TaskEventCallback = Callable[[TaskEvent], None]
FlowStartedCallback = Callable[[str], None]
TaskStartingCallback = Callable[[str, str], None]
from app.schemas.progress import FlowStatus, Progress, TaskProgress, TaskStatus
from app.services.executor.base import ExecutorBase, FlowContext
from app.services.store.base import StoreBase


class ConduitValidationError(ValueError):
    pass


def _now() -> str:
    """Return the current UTC timestamp as an ISO-8601 ``Z`` suffixed string.

    :returns: ISO-8601 timestamp ending with ``Z``.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


_current_task_ctx: ContextVar[str] = ContextVar("current_task_name", default="")


def _validate_dag(conduit: Conduit) -> dict[str, list]:
    """Return {task_name: [parsed deps]}. Raises on cycle/unknown/invalid regex.

    :param conduit: parsed conduit whose tasks/dependencies to validate.
    :returns: mapping of task name to its list of parsed dependencies.
    """
    task_names = {t.name for t in conduit.tasks}
    parsed: dict[str, list] = {}
    for t in conduit.tasks:
        try:
            parsed_deps = parse_dependencies(t.depends_on)
        except DependencyParseError as e:
            raise ConduitValidationError(
                f"task {t.name!r}: {e}"
            ) from e
        for d in parsed_deps:
            if d.task not in task_names:
                raise ConduitValidationError(
                    f"task {t.name!r} depends on unknown task {d.task!r}"
                )
        parsed[t.name] = parsed_deps

    # Cycle detection via DFS
    WHITE, GREY, BLACK = 0, 1, 2
    color = {name: WHITE for name in parsed}

    def visit(name: str, stack: list[str]) -> None:
        """DFS-visit ``name``, raising on grey-revisit cycles.

        :param name: task name to visit next.
        :param stack: current DFS stack used to render cycle paths.
        """
        if color[name] == GREY:
            cycle = " -> ".join(stack[stack.index(name):] + [name])
            raise ConduitValidationError(f"circular dependency: {cycle}")
        if color[name] == BLACK:
            return
        color[name] = GREY
        stack.append(name)
        for d in parsed[name]:
            visit(d.task, stack)
        stack.pop()
        color[name] = BLACK

    for name in parsed:
        visit(name, [])

    return parsed


class Engine:
    """Async DAG executor for :class:`Conduit` definitions.

    :param executors: mapping of tool string (e.g. ``"tool:bash"``) to executor
    :param store: :class:`StoreBase` used to read conduits and persist flow state
    """

    def __init__(
        self,
        executors: dict[str, ExecutorBase],
        store: StoreBase,
    ) -> None:
        """Wire the engine with its executor registry and persistence store.

        :param executors: mapping of tool string to :class:`ExecutorBase`.
        :param store: :class:`StoreBase` used to read conduits and persist flow state.
        """
        self.executors = executors
        self.store = store

    # ------------------------------------------------------------------ public

    async def run(
        self,
        conduit: Conduit,
        inputs: dict[str, Any],
        parent_flow_id: str | None = None,
        on_task_event: TaskEventCallback | None = None,
        on_flow_started: FlowStartedCallback | None = None,
        on_task_starting: TaskStartingCallback | None = None,
        show_steps: bool = True,
        working_dir: Path | None = None,
    ) -> str:
        """Execute a conduit to completion, returning the flow id.

        :param conduit: parsed :class:`Conduit` definition
        :param inputs: conduit input map (must cover all required keys)
        :param parent_flow_id: parent flow id for nested ``tool:conduit`` runs
        :param on_task_event: optional callback invoked after each task
            iteration with a :class:`TaskEvent`; used by the CLI renderer.
        :param on_flow_started: optional callback invoked exactly once with
            the new flow id, immediately after it is created and before any
            task runs. Lets callers (e.g. the CLI) record the id so they
            can surface it even if the flow later fails.
        :param on_task_starting: optional callback invoked once per task as it
            transitions to running for the first time, with ``(task_name, tool)``.
        :param show_steps: whether nested executors should surface per-step
            progress events.
        :returns: the new flow id on success
        :raises ConduitValidationError: DAG is invalid (cycle, unknown dep, bad regex)
        :raises ValueError: required inputs are missing
        :raises Exception: first task failure propagates after fail-fast cancel
        """
        # Validate required inputs
        missing = [k for k in conduit.inputs if k not in inputs]
        if missing:
            raise ValueError(f"missing required inputs: {missing}")

        parsed_deps = _validate_dag(conduit)

        flow_id = self.store.create_flow(conduit.name, inputs, parent_flow_id)
        if on_flow_started is not None:
            try:
                on_flow_started(flow_id)
            except Exception as cb_exc:  # noqa: BLE001
                # Caller-supplied callback bugs must never break the flow.
                print(
                    f"[flow-atelier] on_flow_started callback raised: "
                    f"{type(cb_exc).__name__}: {cb_exc}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        progress = Progress(
            status=FlowStatus.running,
            tasks={
                t.name: TaskProgress(status=TaskStatus.pending, of=t.repeat)
                for t in conduit.tasks
            },
            started_at=_now(),
        )
        self.store.write_progress(flow_id, progress)

        # Mutable runtime state
        statuses: dict[str, TaskStatus] = {
            t.name: TaskStatus.pending for t in conduit.tasks
        }
        outputs: dict[str, str] = {}
        skip_reasons: dict[str, str] = {}
        task_map = {t.name: t for t in conduit.tasks}

        runtime_inputs = dict(inputs)  # mutable copy (HITL may append)

        semaphore = asyncio.Semaphore(conduit.max_concurrency)
        running: dict[str, asyncio.Task[None]] = {}
        failed = False
        failure_error: Exception | None = None
        state_changed = asyncio.Event()
        state_changed.set()

        def _safe_emit(event: TaskEvent) -> None:
            """Forward ``event`` to ``on_task_event``, swallowing callback errors.

            :param event: :class:`TaskEvent` to dispatch to the renderer.
            """
            if on_task_event is None:
                return
            try:
                on_task_event(event)
            except Exception as cb_exc:  # noqa: BLE001
                # Renderer bugs must never break the flow.
                print(
                    f"[flow-atelier] on_task_event callback raised: "
                    f"{type(cb_exc).__name__}: {cb_exc}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        def emit_event(
            t: TaskDefinition,
            iteration: int,
            result: ExecutionResult,
            duration: float,
        ) -> None:
            """Emit a TaskEvent for a completed/failed iteration.

            :param t: task definition that just executed.
            :param iteration: 1-based iteration index within ``t.repeat``.
            :param result: :class:`ExecutionResult` returned by the executor.
            :param duration: wall-clock duration of the iteration in seconds.
            """
            _safe_emit(
                TaskEvent(
                    task=t.name,
                    tool=t.tool.value,
                    iteration=iteration,
                    of=t.repeat,
                    exit_code=result.exit_code,
                    duration_seconds=round(duration, 3),
                    output=result.output,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    success=result.success,
                    status=TaskStatus.completed if result.success else TaskStatus.failed,
                    live_streamed=t.interactive,
                    steps=result.steps,
                )
            )

        def emit_disposition(
            name: str, status: TaskStatus, reason: str = ""
        ) -> None:
            """Emit a TaskEvent for a non-running disposition (skip/cancel).

            :param name: task name whose disposition is being reported.
            :param status: terminal :class:`TaskStatus` (skipped or cancelled).
            :param reason: optional human-readable reason for the disposition.
            """
            t = task_map[name]
            _safe_emit(
                TaskEvent(
                    task=t.name,
                    tool=t.tool.value,
                    of=t.repeat,
                    success=False,
                    status=status,
                    reason=reason,
                )
            )

        def mark_skipped(name: str, reason: str) -> None:
            """Mark a task as skipped, persist progress, and emit a disposition.

            :param name: task name being skipped.
            :param reason: human-readable explanation for the skip.
            """
            statuses[name] = TaskStatus.skipped
            skip_reasons[name] = reason
            progress.tasks[name] = TaskProgress(
                status=TaskStatus.skipped,
                of=task_map[name].repeat,
                reason=reason,
            )
            self.store.write_progress(flow_id, progress)
            emit_disposition(name, TaskStatus.skipped, reason)

        def mark_running(name: str, iteration: int) -> None:
            """Transition a task to running state and persist progress.

            :param name: task name entering the running state.
            :param iteration: 1-based iteration number about to execute.
            """
            statuses[name] = TaskStatus.running
            progress.tasks[name] = TaskProgress(
                status=TaskStatus.running,
                iteration=iteration,
                of=task_map[name].repeat,
            )
            progress.current_tasks = [
                n for n, s in statuses.items() if s == TaskStatus.running
            ]
            self.store.write_progress(flow_id, progress)
            if iteration == 1 and on_task_starting is not None:
                try:
                    on_task_starting(name, task_map[name].tool.value)
                except Exception:  # noqa: BLE001
                    pass

        def mark_completed(name: str, iteration: int) -> None:
            """Mark a task as completed at ``iteration`` and persist progress.

            :param name: task name that has finished successfully.
            :param iteration: 1-based iteration number that completed the task.
            """
            statuses[name] = TaskStatus.completed
            progress.tasks[name] = TaskProgress(
                status=TaskStatus.completed,
                iteration=iteration,
                of=task_map[name].repeat,
            )
            progress.current_tasks = [
                n for n, s in statuses.items() if s == TaskStatus.running
            ]
            self.store.write_progress(flow_id, progress)

        def mark_failed(name: str) -> None:
            """Mark a task as failed and persist progress.

            :param name: task name that has failed.
            """
            statuses[name] = TaskStatus.failed
            progress.tasks[name] = TaskProgress(
                status=TaskStatus.failed,
                of=task_map[name].repeat,
            )
            self.store.write_progress(flow_id, progress)

        async def run_task(t: TaskDefinition) -> None:
            """Execute one task end-to-end including repeats and loop predicates.

            :param t: task definition to execute.
            """
            nonlocal failed, failure_error
            _current_task_ctx.set(t.name)
            try:
                # Resolve {{task.output}} templates now (inputs resolved per-iteration)
                unavailable = {
                    n for n, s in statuses.items()
                    if s in (TaskStatus.skipped, TaskStatus.failed, TaskStatus.cancelled)
                }
                try:
                    resolved = resolve(
                        t.task, runtime_inputs, outputs, unavailable_tasks=unavailable
                    )
                except SkipSignal as e:
                    mark_skipped(t.name, str(e))
                    state_changed.set()
                    return
                except TemplateError as e:
                    mark_failed(t.name)
                    failed = True
                    failure_error = ValueError(f"task {t.name!r}: {e}")
                    state_changed.set()
                    return

                executor = self.executors.get(t.tool.value)
                if executor is None:
                    mark_failed(t.name)
                    failed = True
                    failure_error = ValueError(
                        f"no executor registered for tool {t.tool.value!r}"
                    )
                    state_changed.set()
                    return

                ctx = FlowContext(
                    flow_id=flow_id,
                    store=self.store,
                    inputs=runtime_inputs,
                    task_outputs=outputs,
                    timeout=conduit.timeout,
                    working_dir=working_dir,
                    show_steps=show_steps,
                    run_nested_conduit=self._make_nested_runner(
                        on_task_event, show_steps=show_steps, working_dir=working_dir
                    ),
                )

                # Pre-parse the loop predicate once per task. Schema enforces
                # that at most one of `until` / `while_` is set, so we end up
                # with a single (compiled_pattern, negate) plus a mode tag.
                # Already validated at conduit-load time, so this cannot raise
                # in practice.
                loop_predicate: tuple | None = None
                loop_mode: str = "until"
                if t.until is not None:
                    loop_predicate = parse_output_predicate(t.until)
                    loop_mode = "until"
                elif t.while_ is not None:
                    loop_predicate = parse_output_predicate(t.while_)
                    loop_mode = "while"

                async with semaphore:
                    last_output = ""
                    for iteration in range(1, t.repeat + 1):
                        mark_running(t.name, iteration)
                        started = _now()
                        start_ts = datetime.now(UTC)
                        try:
                            result = await asyncio.wait_for(
                                executor.execute(t, resolved, ctx),
                                timeout=conduit.timeout,
                            )
                        except TimeoutError:
                            result = ExecutionResult(
                                exit_code=124,
                                stderr=f"engine timeout after {conduit.timeout}s",
                            )
                        except Exception as exc:  # noqa: BLE001
                            result = ExecutionResult(
                                exit_code=1, stderr=f"{type(exc).__name__}: {exc}"
                            )
                        finished = _now()
                        duration = (
                            datetime.now(UTC) - start_ts
                        ).total_seconds()
                        await self.store.append_log(
                            flow_id,
                            LogEntry(
                                task=t.name,
                                tool=t.tool.value,
                                iteration=iteration,
                                of=t.repeat,
                                command=resolved,
                                stdout=result.stdout,
                                stderr=result.stderr,
                                exit_code=result.exit_code,
                                output=result.output,
                                started_at=started,
                                finished_at=finished,
                                duration_seconds=round(duration, 3),
                                steps=result.steps,
                            ),
                        )
                        emit_event(t, iteration, result, duration)
                        if not result.success:
                            mark_failed(t.name)
                            failed = True
                            failure_error = RuntimeError(
                                f"task {t.name!r} failed: exit={result.exit_code} "
                                f"stderr={result.stderr.strip()[:200]}"
                            )
                            state_changed.set()
                            return
                        last_output = (
                            result.last_turn_output
                            if result.last_turn_output is not None
                            else result.output
                        )
                        if loop_predicate is not None:
                            # Conduit tasks evaluate the predicate against the
                            # outputs of every nested sub-task (any-match),
                            # not just the conduit's aggregate result.output.
                            if t.tool == ToolType.conduit:
                                scope_outputs = result.sub_outputs
                            else:
                                scope_outputs = [result.output]
                            if evaluate_loop_predicate(
                                loop_predicate, scope_outputs, loop_mode
                            ):
                                break
                    outputs[t.name] = last_output
                    mark_completed(t.name, iteration)
            except asyncio.CancelledError:
                if statuses[t.name] not in (
                    TaskStatus.completed, TaskStatus.failed, TaskStatus.skipped
                ):
                    statuses[t.name] = TaskStatus.cancelled
                    progress.tasks[t.name] = TaskProgress(
                        status=TaskStatus.cancelled, of=t.repeat
                    )
                    self.store.write_progress(flow_id, progress)
                    emit_disposition(
                        t.name, TaskStatus.cancelled, "fail-fast: upstream failed"
                    )
                raise
            finally:
                state_changed.set()

        # ------------------------------------------------------------------ loop
        try:
            while True:
                # Evaluate all pending tasks; launch satisfied, skip unsatisfiable.
                for name, t in task_map.items():
                    if statuses[name] != TaskStatus.pending:
                        continue
                    if failed:
                        break
                    deps = parsed_deps[name]
                    decision = "satisfied"
                    skip_reason: str | None = None
                    for d in deps:
                        r, reason = evaluate(d, statuses, outputs)
                        if r == "skip":
                            decision = "skip"
                            skip_reason = reason
                            break
                        if r == "wait":
                            decision = "wait"
                            break
                    if decision == "skip":
                        mark_skipped(name, skip_reason or "dependency not met")
                    elif decision == "satisfied":
                        statuses[name] = TaskStatus.running  # reserve
                        running[name] = asyncio.create_task(run_task(t))

                # Termination check
                pending_exists = any(
                    s == TaskStatus.pending for s in statuses.values()
                )
                if not running and not pending_exists:
                    break
                if failed and not running:
                    break

                # Wait for at least one task transition
                state_changed.clear()
                if running:
                    done, _pending = await asyncio.wait(
                        list(running.values()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for d in done:
                        name_done = next(n for n, task in running.items() if task is d)
                        running.pop(name_done)
                        # propagate exceptions only for engine bugs; task bodies trap them
                        exc = d.exception()
                        if exc and not isinstance(exc, asyncio.CancelledError):
                            failed = True
                            failure_error = exc
                else:
                    # No running tasks but still pending (waiting on something that
                    # can't change) — shouldn't happen after evaluation; safeguard:
                    await state_changed.wait()

                if failed and running:
                    for rt in running.values():
                        rt.cancel()
                    await asyncio.gather(*running.values(), return_exceptions=True)
                    running.clear()

            # Mark any still-pending tasks (due to fail-fast) as cancelled
            for name, s in statuses.items():
                if s == TaskStatus.pending:
                    statuses[name] = TaskStatus.cancelled
                    progress.tasks[name] = TaskProgress(
                        status=TaskStatus.cancelled, of=task_map[name].repeat
                    )
                    emit_disposition(
                        name, TaskStatus.cancelled, "upstream failed"
                    )

            progress.current_tasks = []
            progress.finished_at = _now()
            progress.status = FlowStatus.failed if failed else FlowStatus.completed
            self.store.write_progress(flow_id, progress)

            if not failed:
                self.store.write_outputs(
                    flow_id,
                    {name: outputs.get(name) for name in task_map},
                )

            if failed:
                raise failure_error or RuntimeError("flow failed")
            return flow_id
        except BaseException:
            # Ensure progress reflects failure on unexpected errors
            progress.current_tasks = []
            progress.finished_at = _now()
            if progress.status == FlowStatus.running:
                progress.status = FlowStatus.failed
            self.store.write_progress(flow_id, progress)
            raise

    # ------------------------------------------------------------------ helpers

    def _make_nested_runner(
        self,
        on_task_event: TaskEventCallback | None = None,
        show_steps: bool = True,
        working_dir: Path | None = None,
    ):
        """Build the nested-conduit runner passed to executors via FlowContext.

        :param on_task_event: optional task-event callback forwarded to the child run.
        :param show_steps: whether the nested run should surface per-step progress.
        :param working_dir: working directory forwarded to nested runs.
        :returns: an async callable ``(conduit_name, child_inputs, parent_flow_id)``
            that loads and runs the named child conduit.
        """
        async def _run_nested(conduit_name: str, child_inputs, parent_flow_id):
            """Load and run a child conduit, returning its flow id.

            :param conduit_name: name of the child conduit to load from the store.
            :param child_inputs: input map passed to the child conduit.
            :param parent_flow_id: flow id of the parent run, for linkage.
            """
            child_conduit = self.store.read_conduit(conduit_name)
            return await self.run(
                child_conduit,
                child_inputs,
                parent_flow_id,
                on_task_event=on_task_event,
                show_steps=show_steps,
                working_dir=working_dir,
            )

        return _run_nested
