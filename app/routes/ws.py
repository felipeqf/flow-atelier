"""``/ws/run-conduit`` WebSocket route."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket
from pydantic import TypeAdapter, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.core.atelier import Atelier
from app.schemas.log import TaskEvent
from app.schemas.progress import TaskStatus
from app.schemas.ws import (
    CancelMessage,
    ClientMessage,
    HitlAnswerMessage,
    RunMessage,
)
from app.services.api.base import get_atelier
from app.services.api.ws_hitl import WsHitlExecutor
from app.services.api.ws_manager import WebSocketBroker
from app.services.api.ws_sink import WsPromptSink

logger = logging.getLogger(__name__)
router = APIRouter()
_client_message_adapter = TypeAdapter(ClientMessage)


def _step_status_for(event: TaskEvent) -> str:
    """Map a :class:`TaskEvent` to the WS step-status string.

    :param event: task event emitted by the engine.
    :returns: ``"completed"``, ``"failed"`` or the raw status value.
    """
    if event.status == TaskStatus.completed and event.success:
        return "completed"
    if event.status == TaskStatus.completed and not event.success:
        return "failed"
    return event.status.value


@router.websocket("/ws/run-conduit")
async def run_conduit_ws(websocket: WebSocket) -> None:
    """Accept a WS connection, multiplex flow runs over it.

    Builds a fresh :class:`Atelier` per connection so swapping
    ``executors["tool:hitl"]`` for a :class:`WsHitlExecutor` does not
    leak across sockets.

    :param websocket: the incoming Starlette WebSocket connection.
    """
    base_atelier: Atelier = get_atelier(websocket)  # type: ignore[arg-type]
    await websocket.accept()

    async def _send(payload: dict[str, Any]) -> None:
        """Send a JSON payload if the socket is still connected.

        :param payload: JSON-serializable envelope to deliver.
        """
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(payload)

    broker = WebSocketBroker(send=_send)

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            try:
                message = _client_message_adapter.validate_python(raw)
            except ValidationError as e:
                await _send(
                    {
                        "type": "error",
                        "message": f"invalid envelope: {e.errors()[0]['msg']}",
                    }
                )
                continue
            if isinstance(message, RunMessage):
                await _spawn_run(base_atelier, broker, message)
            elif isinstance(message, HitlAnswerMessage):
                try:
                    await broker.deliver_hitl_answer(
                        message.flow_id, dict(message.answers)
                    )
                except KeyError:
                    await _send(
                        {
                            "type": "error",
                            "flow_id": message.flow_id,
                            "message": "no flow registered for hitl_answer",
                        }
                    )
            elif isinstance(message, CancelMessage):
                broker.cancel(message.flow_id)
    finally:
        try:
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.close()
        except RuntimeError:
            pass


async def _spawn_run(
    base_atelier: Atelier,
    broker: WebSocketBroker,
    message: RunMessage,
) -> None:
    """Wire a per-flow Atelier and start the run task.

    :param base_atelier: connection-scoped :class:`Atelier` used as template.
    :param broker: :class:`WebSocketBroker` that fans envelopes out.
    :param message: :class:`RunMessage` describing what to run.
    """
    # Per-connection Atelier instance keeps executor swaps from leaking
    # across sockets (SPEC §10 / risk table).
    atelier = Atelier(
        base_dir=base_atelier.settings.global_atelier_dir,
        prompt_sink=WsPromptSink(broker, message.flow_id),
    )
    broker.register_flow(message.flow_id)
    atelier.executors["tool:hitl"] = WsHitlExecutor(
        broker=broker, flow_id=message.flow_id
    )

    async def _on_task_event(event: TaskEvent) -> None:
        """Emit per-step and step-status envelopes for a task event.

        :param event: task event produced by the engine.
        """
        # Harness tasks stream steps live via WsPromptSink.display_step;
        # only emit batched steps for non-harness tools (bash, hitl, conduit).
        if not event.tool.startswith("harness:"):
            for step in event.steps:
                await broker.send(
                    {
                        "type": "step",
                        "flow_id": message.flow_id,
                        "task": event.task,
                        "step": step.model_dump(mode="json"),
                    }
                )
        await broker.send(
            {
                "type": "step_status",
                "flow_id": message.flow_id,
                "step": event.task,
                "status": _step_status_for(event),
            }
        )

    def _on_task_event_sync(event: TaskEvent) -> None:
        """Schedule the async task-event handler from sync engine code.

        :param event: task event produced by the engine.
        """
        # Engine fires task events synchronously; schedule the async work.
        asyncio.create_task(_on_task_event(event))

    async def _run_and_report() -> None:
        """Drive one flow end-to-end and broadcast its lifecycle envelopes."""
        try:
            await broker.send(
                {"type": "started", "flow_id": message.flow_id}
            )
            try:
                conduit = atelier.store.read_conduit(message.conduit_name)
            except FileNotFoundError as e:
                await broker.send(
                    {
                        "type": "flow_failed",
                        "flow_id": message.flow_id,
                        "error": str(e),
                    }
                )
                return
            try:
                flow_id = await atelier.engine.run(
                    conduit,
                    dict(message.inputs),
                    on_task_event=_on_task_event_sync,
                    working_dir=Path(message.run_path) if message.run_path else None,
                )
            except asyncio.CancelledError:
                await broker.send(
                    {
                        "type": "flow_failed",
                        "flow_id": message.flow_id,
                        "error": "cancelled",
                    }
                )
                raise
            except Exception as e:  # noqa: BLE001
                await broker.send(
                    {
                        "type": "flow_failed",
                        "flow_id": message.flow_id,
                        "error": str(e),
                    }
                )
                return

            for entry in atelier.store.read_logs(flow_id):
                await broker.send(
                    {
                        "type": "log",
                        "flow_id": message.flow_id,
                        "entry": entry.model_dump(mode="json"),
                    }
                )
            await broker.send(
                {"type": "flow_complete", "flow_id": message.flow_id}
            )
        finally:
            broker.unregister_flow(message.flow_id)

    task = asyncio.create_task(_run_and_report())
    broker.track_run(message.flow_id, task)
