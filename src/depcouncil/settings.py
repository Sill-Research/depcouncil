"""Run configuration, read from ``DEPCOUNCIL_*`` environment variables or a ``.env`` file."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """Which models run each tier, how long the council deliberates, and source credentials.

    Models are Pydantic AI model strings (``provider:model``), so any provider Pydantic AI
    supports works, e.g. ``openai:gpt-5`` or ``ollama:qwen3``. ``test`` runs the whole
    pipeline against live data with Pydantic AI's offline ``TestModel`` and no API key.
    """

    # An empty value (`GITHUB_TOKEN=` in .env) means unset rather than an empty string or path.
    model_config = SettingsConfigDict(
        env_prefix="DEPCOUNCIL_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    quick_model: str = Field(
        default="anthropic:claude-sonnet-5-5",
        description="Model for the analysts and the debaters.",
    )
    deep_model: str = Field(
        default="anthropic:claude-opus-5-5",
        description="Model for the chair's final decision.",
    )
    quick_effort: Effort = Field(
        default="medium", description="Thinking effort for the quick tier."
    )
    deep_effort: Effort = Field(default="high", description="Thinking effort for the deep tier.")
    debate_rounds: int = Field(
        default=1, ge=1, le=5, description="Advocate/skeptic exchanges before the chair decides."
    )
    max_tool_rounds: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Model turns an analyst may spend calling data tools before it must report.",
    )
    github_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DEPCOUNCIL_GITHUB_TOKEN", "GITHUB_TOKEN"),
        description="Raises GitHub's limit from 60 to 5,000 requests an hour.",
    )
    http_timeout: float = Field(
        default=30.0, gt=0, description="Seconds before a data request fails."
    )
    memory_path: Path | None = Field(
        default=None,
        description="JSON Lines file where the CLI remembers each decision and recalls past "
        "lessons for the chair. Memory is off when unset.",
    )
