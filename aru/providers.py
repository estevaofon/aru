"""Multi-provider LLM abstraction for aru.

Supports provider/model format (e.g., "anthropic/claude-sonnet-4-5", "ollama/llama3.1").
Maps provider names to Agno model classes and handles provider-specific configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Built-in provider definitions
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    """Configuration for an LLM provider."""
    name: str
    api_key_env: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


# Built-in providers with sensible defaults
BUILTIN_PROVIDERS: dict[str, ProviderConfig] = {
    "anthropic": ProviderConfig(
        name="Anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-5-20250929",
        models={
            "claude-sonnet-4-5": {"id": "claude-sonnet-4-5-20250929", "max_tokens": 16384},
            "claude-sonnet-4-6": {"id": "claude-sonnet-4-6-20250514", "max_tokens": 64000},
            "claude-opus-4": {"id": "claude-opus-4-20250514", "max_tokens": 32000},
            "claude-opus-4-6": {"id": "claude-opus-4-6-20250918", "max_tokens": 64000},
            "claude-haiku-3-5": {"id": "claude-haiku-3-5-20241022", "max_tokens": 8192},
            "claude-haiku-4-5": {"id": "claude-haiku-4-5-20251001", "max_tokens": 8192},
            # Full IDs also work as-is
            "claude-sonnet-4-5-20250929": {"id": "claude-sonnet-4-5-20250929", "max_tokens": 16384},
            "claude-sonnet-4-6-20250514": {"id": "claude-sonnet-4-6-20250514", "max_tokens": 64000},
            "claude-opus-4-20250514": {"id": "claude-opus-4-20250514", "max_tokens": 32000},
            "claude-opus-4-6-20250918": {"id": "claude-opus-4-6-20250918", "max_tokens": 64000},
            "claude-haiku-3-5-20241022": {"id": "claude-haiku-3-5-20241022", "max_tokens": 8192},
            "claude-haiku-4-5-20251001": {"id": "claude-haiku-4-5-20251001", "max_tokens": 8192},
        },
    ),
    "openai": ProviderConfig(
        name="OpenAI",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o",
        models={
            "gpt-4o": {"id": "gpt-4o", "max_tokens": 4096},
            "gpt-4o-mini": {"id": "gpt-4o-mini", "max_tokens": 4096},
            "gpt-4.1": {"id": "gpt-4.1", "max_tokens": 4096},
            "gpt-4.1-mini": {"id": "gpt-4.1-mini", "max_tokens": 4096},
            "gpt-4.1-nano": {"id": "gpt-4.1-nano", "max_tokens": 4096},
            "o3-mini": {"id": "o3-mini", "max_tokens": 4096},
        },
    ),
    "ollama": ProviderConfig(
        name="Ollama",
        base_url="http://localhost:11434",
        default_model="llama3.1",
        models={},  # Ollama models are dynamic - any installed model works
    ),
    "groq": ProviderConfig(
        name="Groq",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        models={
            "llama-3.3-70b-versatile": {"id": "llama-3.3-70b-versatile", "max_tokens": 4096},
            "llama-3.1-8b-instant": {"id": "llama-3.1-8b-instant", "max_tokens": 4096},
            "mixtral-8x7b-32768": {"id": "mixtral-8x7b-32768", "max_tokens": 4096},
        },
    ),
    "openrouter": ProviderConfig(
        name="OpenRouter",
        api_key_env="OPENROUTER_API_KEY",
        default_model="anthropic/claude-sonnet-4-5",
        models={},  # OpenRouter supports hundreds of models dynamically
    ),
    "deepseek": ProviderConfig(
        name="DeepSeek",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        models={
            "deepseek-chat": {"id": "deepseek-chat", "max_tokens": 8192},
            "deepseek-chat-v3-0324": {"id": "deepseek-chat-v3-0324", "max_tokens": 16384},
            "deepseek-reasoner": {"id": "deepseek-reasoner", "max_tokens": 16384},
        },
    ),
}

# Common short names (map to anthropic/ provider)
MODEL_ALIASES: dict[str, str] = {
    "sonnet": "anthropic/claude-sonnet-4-5",
    "opus": "anthropic/claude-opus-4",
    "haiku": "anthropic/claude-haiku-3-5",
}


# ---------------------------------------------------------------------------
# Provider registry (built-ins + user overrides from aru.json)
# ---------------------------------------------------------------------------

_providers: dict[str, ProviderConfig] = {}


def _init_providers():
    """Initialize provider registry with built-in defaults."""
    global _providers
    _providers = {k: v for k, v in BUILTIN_PROVIDERS.items()}


_init_providers()


def register_provider(key: str, config: ProviderConfig):
    """Register or override a provider configuration."""
    _providers[key] = config


def get_provider(key: str) -> ProviderConfig | None:
    """Get provider config by key."""
    return _providers.get(key)


def list_providers() -> dict[str, ProviderConfig]:
    """Return all registered providers."""
    return dict(_providers)


# ---------------------------------------------------------------------------
# Load user provider overrides from config
# ---------------------------------------------------------------------------

def load_providers_from_config(config_data: dict[str, Any]):
    """Merge user-defined providers from aru.json into the registry.

    Expected format in aru.json:
    {
      "providers": {
        "ollama": {
          "base_url": "http://localhost:11434",
          "models": {
            "deepseek-coder-v2": {"id": "deepseek-coder-v2:latest"}
          }
        },
        "my-custom": {
          "type": "openai",
          "name": "My Custom Provider",
          "api_key_env": "MY_API_KEY",
          "base_url": "https://my-api.example.com/v1",
          "models": {
            "my-model": {"id": "my-model-v1"}
          }
        }
      },
      "models": {
        "default": "anthropic/claude-sonnet-4-5",
        "small": "anthropic/claude-haiku-4-5"
      }
    }
    """
    providers_data = config_data.get("providers", {})
    for key, pdata in providers_data.items():
        if not isinstance(pdata, dict):
            continue

        # If this extends a built-in, start from that base
        existing = _providers.get(key)
        if existing:
            # Merge: user config overrides built-in fields
            if "name" in pdata:
                existing.name = pdata["name"]
            if "api_key_env" in pdata:
                existing.api_key_env = pdata["api_key_env"]
            if "base_url" in pdata:
                existing.base_url = pdata["base_url"]
            if "default_model" in pdata:
                existing.default_model = pdata["default_model"]
            if "models" in pdata:
                existing.models.update(pdata["models"])
            if "options" in pdata:
                existing.options.update(pdata["options"])
        else:
            # New provider - "type" field tells us which Agno class to use
            _providers[key] = ProviderConfig(
                name=pdata.get("name", key),
                api_key_env=pdata.get("api_key_env"),
                base_url=pdata.get("base_url"),
                default_model=pdata.get("default_model"),
                models=pdata.get("models", {}),
                options=pdata.get("options", {}),
            )
            # Store the type hint for model creation
            if "type" in pdata:
                _providers[key].options["_provider_type"] = pdata["type"]


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def resolve_model_ref(model_ref: str) -> tuple[str, str]:
    """Resolve a model reference to (provider_key, model_id).

    Accepts:
      - "anthropic/claude-sonnet-4-5"  → ("anthropic", "claude-sonnet-4-5")
      - "ollama/llama3.1"              → ("ollama", "llama3.1")
      - "sonnet"                       → ("anthropic", "claude-sonnet-4-5") (legacy alias)
      - "anthropic"                    → ("anthropic", <default_model>)
    """
    # Check legacy aliases first
    if model_ref in MODEL_ALIASES:
        model_ref = MODEL_ALIASES[model_ref]

    if "/" in model_ref:
        provider_key, model_name = model_ref.split("/", 1)
    else:
        # Could be a provider name (use its default) or unknown
        if model_ref in _providers:
            provider_key = model_ref
            provider = _providers[provider_key]
            model_name = provider.default_model or ""
        else:
            # Assume anthropic for backward compatibility
            provider_key = "anthropic"
            model_name = model_ref

    return provider_key, model_name


def _get_actual_model_id(provider: ProviderConfig, model_name: str) -> str:
    """Get the actual model ID to send to the API.

    If the model name is in the provider's model registry, use its 'id' field.
    Otherwise, pass the model name through as-is (supports dynamic models like Ollama).
    """
    if model_name in provider.models:
        return provider.models[model_name].get("id", model_name)
    return model_name


def _get_max_tokens(provider: ProviderConfig, model_name: str, default: int = 4096) -> int:
    """Get max_tokens for a model, falling back to default."""
    if model_name in provider.models:
        return provider.models[model_name].get("max_tokens", default)
    return default


# ---------------------------------------------------------------------------
# Model creation — the core function
# ---------------------------------------------------------------------------

def create_model(
    model_ref: str,
    max_tokens: int | None = None,
    cache_system_prompt: bool = True,
    **kwargs,
):
    """Create an Agno model instance from a provider/model reference.

    Args:
        model_ref: Provider/model string (e.g., "anthropic/claude-sonnet-4-5", "ollama/llama3.1")
        max_tokens: Override max tokens (uses provider default if None)
        cache_system_prompt: Whether to cache system prompt (Anthropic-specific)
        **kwargs: Extra provider-specific parameters

    Returns:
        An Agno model instance ready for use with Agent()

    Raises:
        ValueError: If provider is unknown or required dependencies are missing.
    """
    provider_key, model_name = resolve_model_ref(model_ref)
    provider = _providers.get(provider_key)

    if provider is None:
        available = ", ".join(sorted(_providers.keys()))
        raise ValueError(f"Unknown provider '{provider_key}'. Available: {available}")

    model_id = _get_actual_model_id(provider, model_name)
    effective_max_tokens = max_tokens or _get_max_tokens(provider, model_name, 4096)

    # Determine the actual provider type (for custom providers with "type" field)
    provider_type = provider.options.get("_provider_type", provider_key)

    return _create_provider_model(
        provider_type=provider_type,
        provider=provider,
        model_id=model_id,
        max_tokens=effective_max_tokens,
        cache_system_prompt=cache_system_prompt,
        **kwargs,
    )


def _create_provider_model(
    provider_type: str,
    provider: ProviderConfig,
    model_id: str,
    max_tokens: int,
    cache_system_prompt: bool,
    **kwargs,
):
    """Instantiate the correct Agno model class based on provider type."""

    if provider_type == "anthropic":
        from agno.models.anthropic import Claude
        api_key = _resolve_api_key(provider)
        params = {"id": model_id, "max_tokens": max_tokens}
        if cache_system_prompt:
            params["cache_system_prompt"] = True
        if api_key:
            params["api_key"] = api_key
        params.update(kwargs)
        return Claude(**params)

    elif provider_type == "openai":
        from agno.models.openai import OpenAIChat
        api_key = _resolve_api_key(provider)
        params = {"id": model_id, "max_tokens": max_tokens}
        if api_key:
            params["api_key"] = api_key
        if provider.base_url:
            params["base_url"] = provider.base_url
        params.update(kwargs)
        return OpenAIChat(**params)

    elif provider_type == "ollama":
        from agno.models.ollama import Ollama
        params = {"id": model_id}
        host = provider.base_url or "http://localhost:11434"
        params["host"] = host
        # Ollama uses 'options' dict for num_ctx, temperature, etc.
        if provider.options:
            ollama_opts = {k: v for k, v in provider.options.items() if not k.startswith("_")}
            if ollama_opts:
                params["options"] = ollama_opts
        params.update(kwargs)
        return Ollama(**params)

    elif provider_type == "groq":
        from agno.models.groq import Groq
        api_key = _resolve_api_key(provider)
        params = {"id": model_id, "max_tokens": max_tokens}
        if api_key:
            params["api_key"] = api_key
        params.update(kwargs)
        return Groq(**params)

    elif provider_type == "openrouter":
        from agno.models.openrouter import OpenRouter
        api_key = _resolve_api_key(provider)
        params = {"id": model_id, "max_tokens": max_tokens}
        if api_key:
            params["api_key"] = api_key
        params.update(kwargs)
        return OpenRouter(**params)

    elif provider_type == "deepseek":
        from agno.models.deepseek import DeepSeek
        api_key = _resolve_api_key(provider)
        params = {"id": model_id, "max_tokens": max_tokens}
        if api_key:
            params["api_key"] = api_key
        params.update(kwargs)
        return DeepSeek(**params)

    else:
        # Fallback: try OpenAI-compatible (works for many providers)
        from agno.models.openai import OpenAIChat
        api_key = _resolve_api_key(provider)
        params = {"id": model_id, "max_tokens": max_tokens}
        if api_key:
            params["api_key"] = api_key
        if provider.base_url:
            params["base_url"] = provider.base_url
        params.update(kwargs)
        return OpenAIChat(**params)


def _resolve_api_key(provider: ProviderConfig) -> str | None:
    """Resolve API key from environment variable."""
    if provider.api_key_env:
        return os.environ.get(provider.api_key_env)
    return None


# ---------------------------------------------------------------------------
# Convenience: list available models for display
# ---------------------------------------------------------------------------

def get_available_models() -> dict[str, str]:
    """Return a flat dict of model_ref → display_name for all registered providers.

    Includes legacy aliases.
    """
    models: dict[str, str] = {}

    # Legacy aliases
    for alias, ref in MODEL_ALIASES.items():
        provider_key, model_name = resolve_model_ref(ref)
        provider = _providers.get(provider_key)
        if provider:
            actual_id = _get_actual_model_id(provider, model_name)
            models[alias] = f"{provider.name}/{actual_id}"

    # All provider models
    for pkey, provider in _providers.items():
        if provider.models:
            for mname in provider.models:
                ref = f"{pkey}/{mname}"
                if ref not in models:
                    models[ref] = f"{provider.name}/{mname}"
        if provider.default_model:
            ref = f"{pkey}/{provider.default_model}"
            if ref not in models:
                models[ref] = f"{provider.name}/{provider.default_model}"

    return models


def get_model_display(model_ref: str) -> str:
    """Get a human-readable display string for a model reference."""
    provider_key, model_name = resolve_model_ref(model_ref)
    provider = _providers.get(provider_key)
    if provider:
        return f"{provider.name}/{model_name}"
    return model_ref
