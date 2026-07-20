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
    theme: Literal["light", "dark", "system"] = "light"


class OnboardingSettings(_SettingsModel):
    agreement_accepted: bool = False


class ModelSettings(_SettingsModel):
    chat: ModelTag = "qwen3:8b"
    title: ModelTag = "granite4:tiny-h"
    translation: ModelTag = "translategemma:4b"


class GenerationSettings(_SettingsModel):
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    num_ctx: int = Field(default=4096, ge=2048, le=16384)
    seed: int = Field(default=-1, ge=-1, le=2147483647)
    system_instructions: str = Field(default="", max_length=1800)


class MemorySettings(_SettingsModel):
    enabled: bool = True


class TranslationSettings(_SettingsModel):
    enabled: bool = False
    target_language: LanguageName = "Spanish"


class SuggestionSettings(_SettingsModel):
    enabled: bool = True
    model: ModelTag = "qwen3:8b"


class CortexSettings(_SettingsModel):
    """Complete validated settings snapshot with legacy-compatible defaults."""

    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)
    onboarding: OnboardingSettings = Field(default_factory=OnboardingSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    translation: TranslationSettings = Field(default_factory=TranslationSettings)
    suggestions: SuggestionSettings = Field(default_factory=SuggestionSettings)
