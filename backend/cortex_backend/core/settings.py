"""Typed settings shared by legacy adapters and future backend services."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


ModelTag = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
LanguageName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]


class _SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AppearanceSettings(_SettingsModel):
    theme: Literal["light", "dark", "system"] = "dark"


class OnboardingSettings(_SettingsModel):
    agreement_accepted: bool = False


class ModelSettings(_SettingsModel):
    # Chat and title models are selected from the models Ollama reports on
    # this machine, or from local GGUF files (id "gguf:<filename>", see
    # cortex_backend.llamacpp.model_directory). Keeping them unset until a
    # scan happens avoids shipping a hidden, hard-coded model preference.
    chat: ModelTag | None = None
    title: ModelTag | None = None
    translation: ModelTag = "translategemma:4b"
    # Folder scanned for .gguf files and used as the download destination for
    # "download a model by URL/Hugging Face repo". None => AppPaths' default
    # (created lazily on first use, not eagerly here).
    gguf_directory: str | None = None


class LlamaCppSettings(_SettingsModel):
    # "auto" tries Vulkan (broad GPU support, no extra toolkit) first and
    # falls back to the CPU build if Vulkan can't launch on this machine.
    gpu_backend: Literal["auto", "vulkan", "cpu"] = "auto"


class GenerationSettings(_SettingsModel):
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    top_k: int = Field(default=40, ge=0, le=200)
    repeat_penalty: float = Field(default=1.1, ge=0.5, le=2.0)
    num_ctx: int = Field(default=4096, ge=2048, le=16384)
    seed: int = Field(default=-1, ge=-1, le=2147483647)
    system_instructions: str = Field(default="", max_length=1800)


# The subset of GenerationSettings that a single request may override for
# just that turn, without changing the standing global default. Kept as a
# tuple (not derived from the model) so the merge order in routes.py is
# explicit and doesn't silently pick up unrelated future settings fields.
GENERATION_OVERRIDE_FIELDS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "repeat_penalty",
    "num_ctx",
    "seed",
)


class GenerationOptionsOverride(_SettingsModel):
    """Optional per-request overrides. Unset fields fall back to GenerationSettings."""

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0, le=200)
    repeat_penalty: float | None = Field(default=None, ge=0.5, le=2.0)
    num_ctx: int | None = Field(default=None, ge=2048, le=16384)
    seed: int | None = Field(default=None, ge=-1, le=2147483647)


class ExecutionSettings(_SettingsModel):
    """Small, user-visible controls for the bounded local execution tools."""

    automatic_compute: bool = True
    code_execution_enabled: bool = True


class MemorySettings(_SettingsModel):
    enabled: bool = True


class TranslationSettings(_SettingsModel):
    enabled: bool = False
    target_language: LanguageName = "Spanish"


class SuggestionSettings(_SettingsModel):
    """Legacy-compatible settings retained for reading existing workspaces."""

    enabled: bool = True
    # Cortex no longer generates or renders follow-up suggestion prompts.
    model: ModelTag | None = None


class CortexSettings(_SettingsModel):
    """Complete validated settings snapshot with legacy-compatible defaults."""

    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)
    onboarding: OnboardingSettings = Field(default_factory=OnboardingSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    llamacpp: LlamaCppSettings = Field(default_factory=LlamaCppSettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    translation: TranslationSettings = Field(default_factory=TranslationSettings)
    suggestions: SuggestionSettings = Field(default_factory=SuggestionSettings)
