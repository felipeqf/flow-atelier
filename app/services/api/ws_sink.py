"""WebSocket-driven PromptSink for real-time step streaming.

Replaces :class:`TerminalPromptSink` for connections served over the
WebSocket endpoint: ``display_step`` forwards intermediate steps as
``step`` envelopes through the broker as they arrive from the harness
executor, rather than writing them to the terminal.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.modules.engine import _current_task_ctx
from app.services.api.ws_manager import WebSocketBroker
from app.services.executor.prompt_sink import PermissionOption

if TYPE_CHECKING:
    from app.schemas.log import IntermediateStep


class WsPromptSink:
    """:class:`PromptSink` that streams intermediate steps over WebSocket.

    :param broker: per-connection broker used to send envelopes
    :param flow_id: flow this sink instance is bound to
    """

    def __init__(self, broker: WebSocketBroker, flow_id: str) -> None:
        """Bind the sink to a broker and a specific flow.

        :param broker: per-connection broker used to send envelopes
        :param flow_id: flow this sink instance is bound to
        """
        self._broker = broker
        self._flow_id = flow_id

    async def display(self, text: str) -> None:
        """No-op for WebSocket runs (agent text streaming is separate).

        :param text: text to display (ignored).
        """

    async def start_agent_turn(self, label: str = "agent") -> None:
        """No-op for WebSocket runs.

        :param label: turn label (ignored).
        """

    async def request_input(self, prompt: str) -> str:
        """Not supported over WebSocket — interactive input uses HITL.

        :param prompt: prompt label (unused).
        :raises NotImplementedError: always.
        """
        raise NotImplementedError("WS sink does not support request_input")

    async def request_permission(
        self, summary: str, options: list[PermissionOption]
    ) -> str:
        """Auto-approve with the first available option.

        :param summary: permission description (unused).
        :param options: available permission choices.
        :returns: the ``id`` of the first option.
        """
        return options[0].id if options else ""

    async def display_step(self, step: IntermediateStep) -> None:
        """Forward an intermediate step as a ``step`` envelope over WebSocket.

        Reads the current task name from the engine's contextvar so that
        concurrent tasks don't clobber each other.

        :param step: the :class:`IntermediateStep` to forward.
        """
        await self._broker.send(
            {
                "type": "step",
                "flow_id": self._flow_id,
                "task": _current_task_ctx.get(""),
                "step": step.model_dump(mode="json"),
            }
        )
