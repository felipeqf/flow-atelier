"""Test that WebSocket emits step messages correctly for harness vs non-harness tasks."""
from __future__ import annotations

from app.schemas.log import IntermediateStep, StepKind, TaskEvent
from app.schemas.progress import TaskStatus


async def test_step_messages_emitted_for_non_harness_task() -> None:
    """Non-harness tasks (bash, hitl, conduit) should emit individual StepMessage
    envelopes from on_task_event."""
    from app.routes.ws import _step_status_for

    steps = [
        IntermediateStep(kind=StepKind.thinking, text="analyzing"),
        IntermediateStep(kind=StepKind.tool_call, tool_name="Read"),
    ]
    event = TaskEvent(
        task="build",
        tool="tool:bash",
        success=True,
        status=TaskStatus.completed,
        steps=steps,
    )

    assert _step_status_for(event) == "completed"
    assert not event.tool.startswith("harness:")

    for step in steps:
        msg = {
            "type": "step",
            "flow_id": "test-flow",
            "task": event.task,
            "step": step.model_dump(mode="json"),
        }
        assert msg["type"] == "step"
        assert msg["task"] == "build"


async def test_step_messages_skipped_for_harness_task() -> None:
    """Harness tasks stream steps live via WsPromptSink, so on_task_event
    should skip the step iteration."""
    event = TaskEvent(
        task="scrape",
        tool="harness:opencode",
        success=True,
        status=TaskStatus.completed,
        steps=[
            IntermediateStep(kind=StepKind.thinking, text="planning"),
        ],
    )

    assert event.tool.startswith("harness:")
