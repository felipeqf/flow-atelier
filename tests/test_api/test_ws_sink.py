"""Test WsPromptSink sends step envelopes via the broker."""
from __future__ import annotations

from app.modules.engine import _current_task_ctx
from app.schemas.log import IntermediateStep, StepKind
from app.services.api.ws_sink import WsPromptSink


async def test_display_step_sends_step_envelope() -> None:
    """display_step should forward an IntermediateStep as a step envelope."""
    sent: list[dict] = []

    class FakeBroker:
        async def send(self, payload: dict) -> None:
            sent.append(payload)

    broker = FakeBroker()
    sink = WsPromptSink(broker=broker, flow_id="flow-123")  # type: ignore[arg-type]

    _current_task_ctx.set("my-task")
    step = IntermediateStep(kind=StepKind.thinking, text="analyzing input")
    await sink.display_step(step)

    assert len(sent) == 1
    msg = sent[0]
    assert msg["type"] == "step"
    assert msg["flow_id"] == "flow-123"
    assert msg["task"] == "my-task"
    assert msg["step"]["kind"] == "thinking"
    assert msg["step"]["text"] == "analyzing input"


async def test_display_step_reads_current_task_from_contextvar() -> None:
    """display_step should read the task name from _current_task_ctx."""
    sent: list[dict] = []

    class FakeBroker:
        async def send(self, payload: dict) -> None:
            sent.append(payload)

    broker = FakeBroker()
    sink = WsPromptSink(broker=broker, flow_id="flow-456")  # type: ignore[arg-type]

    _current_task_ctx.set("other-task")
    step = IntermediateStep(kind=StepKind.tool_call, tool_name="Read")
    await sink.display_step(step)

    assert sent[0]["task"] == "other-task"


async def test_request_permission_auto_approves() -> None:
    """request_permission should auto-approve with the first option."""
    from app.services.executor.prompt_sink import PermissionOption

    sink = WsPromptSink(broker=None, flow_id="f1")  # type: ignore[arg-type]
    options = [
        PermissionOption(id="allow", label="Allow"),
        PermissionOption(id="deny", label="Deny"),
    ]
    result = await sink.request_permission("summary", options)
    assert result == "allow"
