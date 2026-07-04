"""Integration and skill catalog metadata for JARVIS onboarding.

This module is intentionally side-effect free: it centralises the providers that
can be configured through the UI and exposes safe helpers for checking whether a
key is present without leaking secrets.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

PLACEHOLDER_PREFIXES = ("your-", "sk-...", "todo", "changeme")


@dataclass(frozen=True)
class ApiProvider:
    id: str
    name: str
    env_key: str
    category: str
    description: str
    placeholder: str = ""
    docs_url: str = ""
    optional: bool = True


@dataclass(frozen=True)
class SkillPack:
    id: str
    name: str
    category: str
    description: str
    bundled: bool = True
    install_hint: str = ""


API_PROVIDERS: tuple[ApiProvider, ...] = (
    ApiProvider("anthropic", "Anthropic Claude", "ANTHROPIC_API_KEY", "LLM", "Primary low-latency reasoning and conversation brain.", "sk-ant-...", "https://console.anthropic.com/", optional=False),
    ApiProvider("fish_audio", "Fish Audio", "FISH_API_KEY", "Voice", "Default JARVIS-style text-to-speech voice.", "fish_...", "https://fish.audio/"),
    ApiProvider("elevenlabs", "ElevenLabs", "ELEVENLABS_API_KEY", "Voice", "Optional premium voices and multilingual TTS.", "eleven_...", "https://elevenlabs.io/"),
    ApiProvider("deepseek", "DeepSeek", "DEEPSEEK_API_KEY", "LLM", "Optional DeepSeek V4 Pro coding/reasoning provider for agent tasks.", "sk-...", "https://platform.deepseek.com/"),
    ApiProvider("openai", "OpenAI", "OPENAI_API_KEY", "LLM", "Optional GPT-5 family, multimodal, realtime, and tool-using model provider.", "sk-...", "https://platform.openai.com/docs/models"),
    ApiProvider("google", "Google AI", "GOOGLE_API_KEY", "LLM", "Optional Gemini models and Google ecosystem integrations.", "AIza...", "https://aistudio.google.com/"),
    ApiProvider("brave_search", "Brave Search", "BRAVE_API_KEY", "Research", "Optional web search key powering the Web Search MCP tool.", "BSA...", "https://brave.com/search/api/"),
)


SKILL_PACKS: tuple[SkillPack, ...] = (
    SkillPack("core-productivity", "Core Productivity", "Bundled", "Calendar, Mail, Notes, tasks, memory, and day planning."),
    SkillPack("developer", "Developer Agent", "Bundled", "Project scanning, Claude Code dispatch, terminal automation, and prompt templates."),
    SkillPack("browser-research", "Browser Research", "Bundled", "Web browsing, research briefs, and source-aware summaries."),
    SkillPack("screen-context", "Screen Context", "Bundled", "Active window awareness and screenshot-based context."),
    SkillPack("voice-studio", "Voice Studio", "Optional", "Fish Audio now; ElevenLabs-ready voice provider expansion.", bundled=False, install_hint="Add ELEVENLABS_API_KEY in onboarding."),
    SkillPack("marketplace", "Task Skill Downloader", "Planned", "Download task-specific skills on demand when an agent needs new capabilities.", bundled=False, install_hint="Use the Skills tab to track planned installable packs."),
)


def is_configured(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip()
    if not normalized:
        return False
    return not any(normalized.lower().startswith(prefix) for prefix in PLACEHOLDER_PREFIXES)


def provider_env_keys() -> set[str]:
    return {provider.env_key for provider in API_PROVIDERS}


def providers_for_status(env: dict[str, str] | None = None) -> list[dict]:
    source = env if env is not None else os.environ
    providers = []
    for provider in API_PROVIDERS:
        item = asdict(provider)
        item["configured"] = is_configured(source.get(provider.env_key, ""))
        providers.append(item)
    return providers


def skills_for_status() -> list[dict]:
    return [asdict(skill) for skill in SKILL_PACKS]
