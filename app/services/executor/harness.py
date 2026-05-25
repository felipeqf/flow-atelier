"""Harness executors — five ACP-speaking coding agents.

Each harness is a thin :class:`AcpHarnessExecutor` subclass differing only
in its ``launch_cmd``. Each reuses the host CLI's own config and auth.

- ``harness:claude-code`` → ``@agentclientprotocol/claude-agent-acp`` via ``npx``
- ``harness:codex``       → ``@zed-industries/codex-acp`` via ``npx``
- ``harness:opencode``    → ``opencode acp`` (native ACP)
- ``harness:copilot``     → ``copilot --acp`` (GitHub Copilot CLI, native ACP)
- ``harness:cursor``      → ``@blowmage/cursor-agent-acp`` via ``npx``

Non-interactive mode sends one prompt turn and returns whatever the agent
streams before ``stop_reason``. Interactive mode keeps the session open
and loops: after each turn, if the accumulated output has not contained
the done marker, the executor asks the :class:`PromptSink` for the user's
next message and sends another ``session/prompt``. The loop terminates
when the marker appears or when :attr:`MAX_INTERACTIVE_TURNS` is reached.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import acp
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    DeniedOutcome,
    RequestPermissionResponse,
    SessionModeState,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

from app.schemas.conduit import TaskDefinition
from app.schemas.log import ExecutionResult, IntermediateStep, StepKind
from app.services.executor.base import ExecutorBase, FlowContext
from app.services.executor.prompt_sink import (
    PromptSink,
    TerminalPromptSink,
)

logger = logging.getLogger(__name__)


def _resolve_cmd(cmd: str) -> str:
    """On Windows, resolve .cmd/.bat wrappers that ``create_subprocess_exec`` can't find."""
    if os.name == "nt":
        resolved = shutil.which(cmd)
        if resolved is not None:
            return resolved
    return cmd


def _close_proc_transports(proc: asyncio.subprocess.Process) -> None:
    """Explicitly close pipe transports on Windows to prevent ``__del__`` errors."""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            with contextlib.suppress(OSError, ValueError):
                transport.close()


DEFAULT_DONE_MARKER = "[ATELIER_DONE]"
MAX_INTERACTIVE_TURNS = 20

# Group consecutive AgentThoughtChunk updates into one IntermediateStep
# until the merged text reaches this many characters or hits a newline.
# Some agents (notably opencode) emit one thought chunk per token; without
# grouping the UI shows one rendered line per word.
THINKING_FLUSH_CHARS = 200

# Mode-id keyword tiers in descending permissiveness. Each ACP agent
# advertises its own mode ids; we pick the most permissive one available so
# the agent never has to ask for tool permission. Keyword matched against
# ``id`` case-insensitively.
PERMISSIVE_MODE_KEYWORDS: tuple[str, ...] = (
    "bypass",      # claude-code-acp's bypassPermissions
    "yolo",
    "danger",      # codex-acp's danger-full-access family
    "full-auto",
    "full_auto",
    "accept",      # claude-code-acp's acceptEdits (weaker, but still skips edit prompts)
    "auto",
)


def _pick_permissive_mode(state: SessionModeState | None) -> str | None:
    """Return the most permissive mode id from ``state``, or ``None``.

    Walks :data:`PERMISSIVE_MODE_KEYWORDS` and returns the first
    available mode whose ``id`` contains a keyword (case-insensitive).

    :param state: ACP session mode state advertised by the agent, or ``None``.
    :returns: the most permissive matching mode id, or ``None`` if no match.
    """
    if state is None or not state.available_modes:
        return None
    available = state.available_modes
    for keyword in PERMISSIVE_MODE_KEYWORDS:
        for mode in available:
            if keyword in mode.id.lower():
                return mode.id
    return None

CLAUDE_ACP_LAUNCH = [
    "npx",
    "-y",
    "@agentclientprotocol/claude-agent-acp@0.37.0",
]
CODEX_ACP_LAUNCH = [
    "npx",
    "-y",
    "@zed-industries/codex-acp@0.14.0",
]
OPENCODE_ACP_LAUNCH = ["opencode", "acp"]
COPILOT_ACP_LAUNCH = ["copilot", "--acp"]
CURSOR_ACP_LAUNCH = [
    "npx",
    "-y",
    "@blowmage/cursor-agent-acp@0.7.1",
]


def build_interactive_suffix(marker: str) -> str:
    """Return the trailing instruction appended to interactive prompts.

    :param marker: the done-token the agent must emit when finished.
    :returns: instruction text instructing the agent to emit ``marker`` once.
    """
    return (
        "\n\nWhen — and only when — you are completely finished answering, "
        f"output the exact token {marker} to signal completion. "
        "Do NOT echo or repeat the prompt back. Do NOT mention this "
        f"instruction in your answer. The token {marker} must appear only "
        "once, at the very end of your final response."
    )


class _BufferingClient:
    """ACP :class:`acp.Client` that buffers agent output and routes user I/O.

    Agent message chunks are appended to ``buffer`` and mirrored to the
    :class:`PromptSink`. Tool-permission requests are presented to the sink;
    the selected option id is returned as an :class:`AllowedOutcome`.

    The harness capabilities default to "no filesystem, no terminal" so the
    agent should not call the file/terminal methods — if it does, they raise
    :class:`NotImplementedError`.

    :param sink: surface for permission requests and (when enabled) live
        chunk / step streaming
    :param stream_messages: when ``True`` (interactive turns), agent message
        chunks are mirrored to ``sink.display`` as they arrive so the user
        can follow the agent before deciding their next reply. When
        ``False`` (single-turn / non-interactive), chunks are only buffered
        for the result — the caller renders them once when the turn ends,
        avoiding double-rendering of the same text.
    :param stream_steps: when ``True``, intermediate steps (thinking, tool
        calls, tool results) are mirrored to ``sink.display_step`` as they
        arrive. Independent of ``stream_messages`` so non-interactive runs
        can surface tool/thinking activity live without dumping the agent's
        full prose mid-flow.
    """

    def __init__(
        self,
        sink: PromptSink,
        stream_messages: bool = False,
        stream_steps: bool = False,
        done_marker: str = DEFAULT_DONE_MARKER,
    ) -> None:
        """Initialize the buffering client.

        :param sink: prompt sink used for permission requests and streaming.
        :param stream_messages: if ``True``, mirror agent message chunks to the sink.
        :param stream_steps: if ``True``, mirror intermediate steps to the sink.
        :param done_marker: token stripped from streamed text before display.
        """
        self._sink = sink
        self._stream_messages = stream_messages
        self._stream_steps = stream_steps
        self._done_marker = done_marker
        self.buffer: list[str] = []
        self.steps: list[IntermediateStep] = []
        self._pending_thinking: list[str] = []
        self._pending_thinking_len: int = 0

    async def _flush_thinking(self) -> None:
        """Emit any buffered thought chunks as one merged ``thinking`` step.

        No-op when the buffer is empty or contains only whitespace.
        """
        if not self._pending_thinking:
            return
        text = "".join(self._pending_thinking).strip()
        self._pending_thinking.clear()
        self._pending_thinking_len = 0
        if not text:
            return
        step = IntermediateStep(kind=StepKind.thinking, text=text)
        self.steps.append(step)
        if self._stream_steps and hasattr(self._sink, "display_step"):
            await self._sink.display_step(step)

    async def flush_pending(self) -> None:
        """Flush any partial thought-chunk buffer at a turn boundary.

        Called by the driver after each ``conn.prompt`` round so the last
        group of thinking does not get stuck pending until the next update.
        """
        await self._flush_thinking()

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        """Handle a session update notification from the ACP agent.

        :param session_id: id of the session emitting the update (unused).
        :param update: the ACP update payload (message chunk, tool call, etc.).
        :param kwargs: additional ACP fields (unused).
        """
        del session_id, kwargs
        if isinstance(update, AgentMessageChunk):
            await self._flush_thinking()
            content = update.content
            text = getattr(content, "text", None)
            if text:
                self.buffer.append(text)
                if self._stream_messages:
                    await self._sink.display(text.replace(self._done_marker, ""))
        elif isinstance(update, AgentThoughtChunk):
            text = getattr(update.content, "text", "") or ""
            if not text:
                return
            self._pending_thinking.append(text)
            self._pending_thinking_len += len(text)
            if self._pending_thinking_len >= THINKING_FLUSH_CHARS or "\n" in text:
                await self._flush_thinking()
        elif isinstance(update, ToolCallStart):
            await self._flush_thinking()
            step = IntermediateStep(
                kind=StepKind.tool_call,
                tool_call_id=update.tool_call_id,
                tool_name=update.title,
                tool_kind=update.kind or "",
                tool_status=update.status or "",
                tool_input=str(update.raw_input)[:500] if update.raw_input else "",
                locations=[loc.path for loc in (update.locations or [])],
            )
            self.steps.append(step)
            if self._stream_steps and hasattr(self._sink, "display_step"):
                await self._sink.display_step(step)
        elif isinstance(update, ToolCallProgress):
            if update.status in ("completed", "failed"):
                await self._flush_thinking()
                step = IntermediateStep(
                    kind=StepKind.tool_result,
                    tool_call_id=update.tool_call_id,
                    tool_status=update.status or "",
                    tool_output=str(update.raw_output)[:500] if update.raw_output else "",
                )
                self.steps.append(step)
                if self._stream_steps and hasattr(self._sink, "display_step"):
                    await self._sink.display_step(step)

    async def request_permission(
        self, options, session_id: str, tool_call, **kwargs
    ) -> RequestPermissionResponse:
        """Auto-approve a tool permission request with the most permissive option.

        :param options: list of permission options offered by the agent.
        :param session_id: id of the requesting session (unused).
        :param tool_call: the pending tool call (unused).
        :param kwargs: additional ACP fields (unused).
        :returns: an :class:`AllowedOutcome` selecting the chosen option, or
            a :class:`DeniedOutcome` if no allow option is available.
        """
        # Backstop for any agent that still asks despite the permissive
        # session mode: silently pick the most permissive allow option.
        # Tool activity is still surfaced to the user via display_step.
        del session_id, tool_call, kwargs
        if not options:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        chosen = (
            next((o for o in options if (o.kind or "") == "allow_always"), None)
            or next((o for o in options if (o.kind or "") == "allow_once"), None)
            or next((o for o in options if (o.kind or "").startswith("allow")), None)
        )
        if chosen is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=chosen.option_id)
        )

    # ---- capabilities we don't advertise: safe stubs ----
    async def write_text_file(self, *args: Any, **kwargs: Any) -> None:
        """Reject file-write requests; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError("file write not supported by atelier harness")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> None:
        """Reject file-read requests; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError("file read not supported by atelier harness")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> None:
        """Reject terminal-create requests; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError("terminal not supported by atelier harness")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> None:
        """Reject terminal-output requests; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError

    async def release_terminal(self, *args: Any, **kwargs: Any) -> None:
        """No-op release_terminal stub.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        return None

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> None:
        """Reject wait_for_terminal_exit; capability not advertised.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        raise NotImplementedError

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> None:
        """No-op kill_terminal stub.

        :param args: ignored positional ACP arguments.
        :param kwargs: ignored keyword ACP arguments.
        """
        del args, kwargs
        return None

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """No-op handler for unknown ACP extension methods.

        :param method: the extension method name (unused).
        :param params: the extension method params (unused).
        :returns: an empty dict.
        """
        del method, params
        return {}

    async def ext_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        """No-op handler for unknown ACP extension notifications.

        :param method: the notification method name (unused).
        :param params: the notification params (unused).
        """
        del method, params
        return None

    def on_connect(self, conn) -> None:
        """Store the ACP connection for later use.

        :param conn: the ACP connection handed to the client on attach.
        """
        self._conn = conn


class AcpHarnessExecutor(ExecutorBase):
    """Executor that drives an ACP agent subprocess.

    :param launch_cmd: argv to spawn the ACP agent (e.g.
        ``["npx", "-y", "@zed-industries/claude-code-acp"]``)
    :param sink: :class:`PromptSink` for user I/O and permission requests
    :param done_marker: substring that terminates an interactive loop
    """

    def __init__(
        self,
        launch_cmd: list[str],
        sink: PromptSink | None = None,
        done_marker: str | None = None,
    ) -> None:
        """Initialize the harness executor.

        :param launch_cmd: argv used to spawn the ACP agent subprocess.
        :param sink: optional :class:`PromptSink` for user I/O; defaults to
            :class:`TerminalPromptSink`.
        :param done_marker: optional done-token override; defaults to
            :data:`DEFAULT_DONE_MARKER`.
        """
        if not launch_cmd:
            raise ValueError("launch_cmd must not be empty")
        self.launch_cmd = list(launch_cmd)
        self.sink = sink if sink is not None else TerminalPromptSink()
        self.done_marker = done_marker or DEFAULT_DONE_MARKER

    async def execute(
        self,
        task: TaskDefinition,
        resolved_command: str,
        context: FlowContext,
    ) -> ExecutionResult:
        """Drive an ACP agent for one task, single-turn or interactive.

        :param task: the task definition; ``task.interactive`` selects the loop.
        :param resolved_command: the prompt text with templates resolved.
        :param context: runtime :class:`FlowContext` providing timeout/step flags.
        :returns: :class:`ExecutionResult` capturing buffered output and steps.
        """
        prompt_text = resolved_command
        if task.interactive:
            prompt_text = prompt_text + build_interactive_suffix(self.done_marker)

        cwd = str(context.working_dir) if context.working_dir else str(Path.cwd())
        client = _BufferingClient(
            self.sink,
            stream_messages=task.interactive,
            stream_steps=(task.interactive or context.show_steps),
            done_marker=self.done_marker,
        )

        try:
            return await asyncio.wait_for(
                self._drive_session(client, prompt_text, task.interactive, cwd),
                timeout=context.timeout,
            )
        except TimeoutError:
            await client.flush_pending()
            return ExecutionResult(
                exit_code=124,
                stdout="".join(client.buffer),
                stderr=f"harness timeout after {context.timeout}s",
                output="".join(client.buffer),
                steps=client.steps,
            )
        except Exception as exc:  # noqa: BLE001
            await client.flush_pending()
            return ExecutionResult(
                exit_code=1,
                stdout="".join(client.buffer),
                stderr=f"{type(exc).__name__}: {exc}",
                output="".join(client.buffer),
                steps=client.steps,
            )

    async def _drive_session(
        self,
        client: _BufferingClient,
        initial_prompt: str,
        interactive: bool,
        cwd: str,
    ) -> ExecutionResult:
        """Spawn the agent, initialize a session, and run one or many turns.

        :param client: buffering ACP client receiving session updates.
        :param initial_prompt: first prompt to send (already suffix-augmented
            for interactive runs).
        :param interactive: ``True`` to loop until the done marker arrives.
        :param cwd: working directory passed to the agent process.
        :returns: :class:`ExecutionResult` from single-turn or interactive run.
        """
        cmd, *args = self.launch_cmd
        cmd = _resolve_cmd(cmd)
        # Raise the per-line StreamReader limit well above asyncio's 64 KiB
        # default; Codex and similar harnesses emit large JSON-RPC frames
        # (tool results, planning output) that routinely exceed it.
        async with acp.spawn_agent_process(
            client,
            cmd,
            *args,
            cwd=cwd,
            transport_kwargs={"limit": 8 * 1024 * 1024},
        ) as (
            conn,
            proc,
        ):
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            sess = await conn.new_session(cwd=cwd)
            await self._maybe_switch_to_permissive_mode(conn, sess)

            try:
                if not interactive:
                    return await self._run_single_turn(
                        conn, sess.session_id, initial_prompt, client
                    )
                return await self._run_interactive(
                    conn, sess.session_id, initial_prompt, client
                )
            finally:
                if sys.platform == "win32":
                    _close_proc_transports(proc)

    @staticmethod
    async def _maybe_switch_to_permissive_mode(conn, sess) -> None:
        """Switch the session into the most permissive mode the agent offers.

        ACP session modes are the upstream "skip permissions" knob (see
        https://agentclientprotocol.com/protocol/session-modes). Each agent
        advertises its own mode ids (e.g. claude-code-acp's
        ``bypassPermissions``, codex-acp's ``danger-full-access``) under
        ``new_session.modes``. We pick the most permissive one and switch
        before the first prompt so the agent never has to call
        ``session/request_permission``. A bad mode switch is non-fatal —
        the auto-approve backstop in :meth:`_BufferingClient.request_permission`
        still catches any residual prompts.

        :param conn: the ACP connection used to issue ``set_session_mode``.
        :param sess: the new-session response carrying advertised mode ids.
        """
        modes = getattr(sess, "modes", None)
        picked = _pick_permissive_mode(modes)
        if picked is None or picked == modes.current_mode_id:
            return
        try:
            await conn.set_session_mode(mode_id=picked, session_id=sess.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("set_session_mode(%s) failed: %s", picked, exc)

    async def _run_single_turn(
        self,
        conn,
        session_id: str,
        prompt_text: str,
        client: _BufferingClient,
    ) -> ExecutionResult:
        """Send one prompt and return the resulting :class:`ExecutionResult`.

        :param conn: the active ACP connection.
        :param session_id: the ACP session id to address.
        :param prompt_text: the prompt content to send.
        :param client: buffering client capturing streamed output.
        :returns: :class:`ExecutionResult` built from the agent's stop_reason.
        """
        resp = await conn.prompt(
            prompt=[TextContentBlock(type="text", text=prompt_text)],
            session_id=session_id,
        )
        await self._drain_pending_notifications(client)
        await client.flush_pending()
        return self._result_for_turn(client, resp.stop_reason)

    @staticmethod
    async def _drain_pending_notifications(client: _BufferingClient) -> None:
        """Wait for supervised notification handlers to finish.

        The ACP dispatcher runs each notification handler as a background
        task, so session_update handlers for the last few chunks may still
        be running when ``conn.prompt`` returns. We wait for the client's
        buffer to stabilize (no growth for two consecutive short yields)
        or until ``max_wait`` seconds have passed.

        :param client: buffering client whose ``buffer`` is polled for growth.
        """
        max_wait = 0.5
        stable_yields_required = 2
        deadline = asyncio.get_running_loop().time() + max_wait
        last_len = -1
        stable = 0
        while True:
            await asyncio.sleep(0.01)
            cur_len = len(client.buffer)
            if cur_len == last_len:
                stable += 1
                if stable >= stable_yields_required:
                    return
            else:
                stable = 0
                last_len = cur_len
            if asyncio.get_running_loop().time() >= deadline:
                return

    async def _run_interactive(
        self,
        conn,
        session_id: str,
        initial_prompt: str,
        client: _BufferingClient,
    ) -> ExecutionResult:
        """Loop prompts and user replies until the done marker or limit.

        :param conn: the active ACP connection.
        :param session_id: the ACP session id to address.
        :param initial_prompt: the first prompt (already suffix-augmented).
        :param client: buffering client whose buffer is checked for the marker.
        :returns: :class:`ExecutionResult` with cleaned output on success, or
            an error result on input failure or turn-limit exhaustion.
        """
        next_prompt = initial_prompt
        last_stop = "end_turn"
        # Sinks that don't render visually (e.g. API/queue transports)
        # may omit start_agent_turn; only call it if implemented.
        start_turn = getattr(self.sink, "start_agent_turn", None)
        for _ in range(MAX_INTERACTIVE_TURNS):
            if start_turn is not None:
                await start_turn()
            prev_buffer_len = len(client.buffer)
            resp = await conn.prompt(
                prompt=[TextContentBlock(type="text", text=next_prompt)],
                session_id=session_id,
            )
            await self._drain_pending_notifications(client)
            await client.flush_pending()
            last_stop = resp.stop_reason
            buffer_text = "".join(client.buffer)
            if self.done_marker in buffer_text:
                # Strip the protocol sentinel from anything we hand back to
                # the user — it's an internal coordination marker, not content.
                cleaned = buffer_text.replace(self.done_marker, "").rstrip()
                last_turn = (
                    "".join(client.buffer[prev_buffer_len:])
                    .replace(self.done_marker, "")
                    .rstrip()
                )
                return ExecutionResult(
                    exit_code=0,
                    stdout=cleaned,
                    stderr="",
                    output=cleaned,
                    last_turn_output=last_turn,
                    steps=client.steps,
                )
            if resp.stop_reason not in ("end_turn", "max_tokens"):
                break
            try:
                user_reply = await self.sink.request_input(
                    "agent is waiting for your reply:"
                )
            except (EOFError, KeyboardInterrupt) as exc:
                return ExecutionResult(
                    exit_code=1,
                    stdout=buffer_text,
                    stderr=f"interactive input unavailable: {type(exc).__name__}",
                    output=buffer_text,
                    steps=client.steps,
                )
            next_prompt = user_reply + build_interactive_suffix(self.done_marker)

        buffer_text = "".join(client.buffer)
        return ExecutionResult(
            exit_code=1,
            stdout=buffer_text,
            stderr=(
                f"interactive session ended without done marker "
                f"(last stop_reason={last_stop})"
            ),
            output=buffer_text,
            steps=client.steps,
        )

    @staticmethod
    def _result_for_turn(
        client: _BufferingClient, stop_reason: str
    ) -> ExecutionResult:
        """Build an :class:`ExecutionResult` from a finished single turn.

        :param client: buffering client holding the accumulated output/steps.
        :param stop_reason: the agent's reported ``stop_reason`` for the turn.
        :returns: :class:`ExecutionResult` with exit_code 0 on normal stops.
        """
        output = "".join(client.buffer)
        if stop_reason in ("end_turn", "max_tokens"):
            return ExecutionResult(
                exit_code=0, stdout=output, stderr="", output=output,
                steps=client.steps,
            )
        return ExecutionResult(
            exit_code=1,
            stdout=output,
            stderr=f"agent stopped with reason={stop_reason}",
            output=output,
            steps=client.steps,
        )


class ClaudeHarness(AcpHarnessExecutor):
    """`harness:claude-code` — drives ``@zed-industries/claude-code-acp``."""

    def __init__(
        self,
        sink: PromptSink | None = None,
        launch_cmd: list[str] | None = None,
        done_marker: str | None = None,
    ) -> None:
        """Initialize the Claude Code harness.

        :param sink: optional :class:`PromptSink` for user I/O.
        :param launch_cmd: optional argv override; defaults to
            :data:`CLAUDE_ACP_LAUNCH`.
        :param done_marker: optional done-token override.
        """
        super().__init__(
            launch_cmd=launch_cmd or list(CLAUDE_ACP_LAUNCH),
            sink=sink,
            done_marker=done_marker,
        )


class CodexHarness(AcpHarnessExecutor):
    """`harness:codex` — drives ``@zed-industries/codex-acp``."""

    def __init__(
        self,
        sink: PromptSink | None = None,
        launch_cmd: list[str] | None = None,
        done_marker: str | None = None,
    ) -> None:
        """Initialize the Codex harness.

        :param sink: optional :class:`PromptSink` for user I/O.
        :param launch_cmd: optional argv override; defaults to
            :data:`CODEX_ACP_LAUNCH`.
        :param done_marker: optional done-token override.
        """
        super().__init__(
            launch_cmd=launch_cmd or list(CODEX_ACP_LAUNCH),
            sink=sink,
            done_marker=done_marker,
        )


class OpencodeHarness(AcpHarnessExecutor):
    """`harness:opencode` — drives ``opencode acp`` (native ACP)."""

    def __init__(
        self,
        sink: PromptSink | None = None,
        launch_cmd: list[str] | None = None,
        done_marker: str | None = None,
    ) -> None:
        """Initialize the opencode harness.

        :param sink: optional :class:`PromptSink` for user I/O.
        :param launch_cmd: optional argv override; defaults to
            :data:`OPENCODE_ACP_LAUNCH`.
        :param done_marker: optional done-token override.
        """
        super().__init__(
            launch_cmd=launch_cmd or list(OPENCODE_ACP_LAUNCH),
            sink=sink,
            done_marker=done_marker,
        )


class CopilotHarness(AcpHarnessExecutor):
    """`harness:copilot` — drives ``copilot --acp`` (GitHub Copilot CLI)."""

    def __init__(
        self,
        sink: PromptSink | None = None,
        launch_cmd: list[str] | None = None,
        done_marker: str | None = None,
    ) -> None:
        """Initialize the Copilot CLI harness.

        :param sink: optional :class:`PromptSink` for user I/O.
        :param launch_cmd: optional argv override; defaults to
            :data:`COPILOT_ACP_LAUNCH`.
        :param done_marker: optional done-token override.
        """
        super().__init__(
            launch_cmd=launch_cmd or list(COPILOT_ACP_LAUNCH),
            sink=sink,
            done_marker=done_marker,
        )


class CursorHarness(AcpHarnessExecutor):
    """`harness:cursor` — drives the ``@blowmage/cursor-agent-acp`` adapter via npx."""

    def __init__(
        self,
        sink: PromptSink | None = None,
        launch_cmd: list[str] | None = None,
        done_marker: str | None = None,
    ) -> None:
        """Initialize the Cursor harness.

        :param sink: optional :class:`PromptSink` for user I/O.
        :param launch_cmd: optional argv override; defaults to
            :data:`CURSOR_ACP_LAUNCH`.
        :param done_marker: optional done-token override.
        """
        super().__init__(
            launch_cmd=launch_cmd or list(CURSOR_ACP_LAUNCH),
            sink=sink,
            done_marker=done_marker,
        )
