"""Atelier runtime settings (env-driven)."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AtelierSettings(BaseSettings):
    """Environment-driven configuration."""

    model_config = SettingsConfigDict(
        env_prefix="ATELIER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    atelier_dir: Path = Field(
        default_factory=lambda: Path.cwd() / ".atelier",
        description="Base directory holding conduits/ and flows/",
    )
    global_atelier_dir: Path = Field(
        default_factory=lambda: Path.home() / ".atelier",
        description="Global directory holding shared conduits/ (no flows).",
    )
    default_timeout: int = 3600
    default_max_concurrency: int = 3
    claude_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:claude-code ACP agent. "
            "Empty = use the bundled default (@agentclientprotocol/claude-agent-acp)."
        ),
    )
    codex_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:codex ACP agent. "
            "Empty = use the bundled default (@zed-industries/codex-acp)."
        ),
    )
    opencode_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:opencode ACP agent. "
            "Empty = use the bundled default (opencode acp)."
        ),
    )
    copilot_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:copilot ACP agent. "
            "Empty = use the bundled default (copilot --acp)."
        ),
    )
    cursor_launch_cmd: list[str] = Field(
        default_factory=list,
        description=(
            "Override argv for the harness:cursor ACP agent. "
            "Empty = use the bundled default (@blowmage/cursor-agent-acp)."
        ),
    )
    done_marker: str = "[ATELIER_DONE]"
