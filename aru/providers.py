"""Multi-provider LLM abstraction for aru.

Supports provider/model format (e.g., "anthropic/claude-sonnet-4-5", "ollama/llama3.1").
Maps provider names to Agno model classes and handles provider-specific configuration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("aru.providers")


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
        # max_tokens numbers mirror models.dev (see OpenCode's registry).
        # context_window is informational; MODEL_CONTEXT_LIMITS still owns the
        # authoritative per-model input budget used by compaction.
        models={
            # Haiku
            "claude-haiku-3-5":              {"id": "claude-haiku-3-5-20241022", "max_tokens": 8192,   "context_window": 200_000},
            "claude-haiku-3-5-20241022":     {"id": "claude-haiku-3-5-20241022", "max_tokens": 8192,   "context_window": 200_000},
            "claude-haiku-4-5":              {"id": "claude-haiku-4-5-20251001", "max_tokens": 64_000, "context_window": 200_000},
            "claude-haiku-4-5-20251001":     {"id": "claude-haiku-4-5-20251001", "max_tokens": 64_000, "context_window": 200_000},
            # Sonnet
            "claude-sonnet-3-7":             {"id": "claude-3-7-sonnet-20250219", "max_tokens": 64_000, "context_window": 200_000},
            "claude-sonnet-4":               {"id": "claude-sonnet-4-20250514",   "max_tokens": 64_000, "context_window": 200_000},
            "claude-sonnet-4-5":             {"id": "claude-sonnet-4-5-20250929", "max_tokens": 64_000, "context_window": 200_000},
            "claude-sonnet-4-5-20250929":    {"id": "claude-sonnet-4-5-20250929", "max_tokens": 64_000, "context_window": 200_000},
            "claude-sonnet-4-6":             {"id": "claude-sonnet-4-6",          "max_tokens": 64_000, "context_window": 1_000_000},
            # Opus
            "claude-opus-4":                 {"id": "claude-opus-4-20250514",     "max_tokens": 32_000, "context_window": 200_000},
            "claude-opus-4-20250514":        {"id": "claude-opus-4-20250514",     "max_tokens": 32_000, "context_window": 200_000},
            "claude-opus-4-1":               {"id": "claude-opus-4-1-20250805",   "max_tokens": 32_000, "context_window": 200_000},
            "claude-opus-4-1-20250805":      {"id": "claude-opus-4-1-20250805",   "max_tokens": 32_000, "context_window": 200_000},
            "claude-opus-4-5":               {"id": "claude-opus-4-5",            "max_tokens": 64_000, "context_window": 200_000},
            "claude-opus-4-6":               {"id": "claude-opus-4-6",            "max_tokens": 128_000, "context_window": 1_000_000},
            "claude-opus-4-7":               {"id": "claude-opus-4-7",            "max_tokens": 128_000, "context_window": 1_000_000},
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
      "default_model": "anthropic/claude-sonnet-4-5",
      "model_aliases": {
        "small": "anthropic/claude-haiku-4-5",
        "deepseek-v3": "openrouter/deepseek/deepseek-chat-v3-0324"
      },
      "providers": {
        "ollama": {
          "base_url": "http://localhost:11434",
          "models": {
            "deepseek-coder-v2": {"id": "deepseek-coder-v2:latest", "context_limit": 128000}
          }
        },
        "my-custom": {
          "type": "openai",
          "name": "My Custom Provider",
          "api_key_env": "MY_API_KEY",
          "base_url": "https://my-api.example.com/v1",
          "context_limit": 128000,
          "models": {
            "my-model": {"id": "my-model-v1", "context_limit": 64000}
          }
        }
      }
    }

    context_limit can be set per-model or per-provider (provider-level serves as
    default for all its models). Values are merged into MODEL_CONTEXT_LIMITS so
    compaction triggers at the correct threshold.
    """
    from aru.context import MODEL_CONTEXT_LIMITS

    providers_data = config_data.get("providers", {})
    for key, pdata in providers_data.items():
        if not isinstance(pdata, dict):
            continue

        # Provider-level context_limit (applies to all models as default)
        provider_context_limit = pdata.get("context_limit")

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

        # Register context_limit values into MODEL_CONTEXT_LIMITS
        models_data = pdata.get("models", {})
        for model_name, model_cfg in models_data.items():
            if not isinstance(model_cfg, dict):
                continue
            limit = model_cfg.get("context_limit") or provider_context_limit
            if isinstance(limit, int) and limit > 0:
                model_id = model_cfg.get("id", model_name)
                MODEL_CONTEXT_LIMITS[model_id] = limit

        # If provider has context_limit but no per-model overrides, register default model
        if isinstance(provider_context_limit, int) and provider_context_limit > 0:
            provider_obj = _providers.get(key)
            if provider_obj and provider_obj.default_model:
                default_id = _get_actual_model_id(provider_obj, provider_obj.default_model)
                MODEL_CONTEXT_LIMITS.setdefault(default_id, provider_context_limit)


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
        provider_key = provider_key.lower()
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


def get_model_max_tokens(model_ref: str, default: int = 4096) -> int:
    """Public: return the output-token cap for `model_ref`.

    Used by agent factory / runner to size agents and to detect whether a
    response was truncated at the cap. Unknown models fall back to `default`.
    """
    try:
        provider_key, model_name = resolve_model_ref(model_ref)
    except Exception:
        return default
    provider = _providers.get(provider_key)
    if provider is None:
        return default
    return _get_max_tokens(provider, model_name, default)


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
    # Provider-declared ceiling for this model (source of truth).
    provider_cap = _get_max_tokens(provider, model_name, 4096)
    # Clamp caller request to the provider cap — callers can request *less*
    # (e.g. small/subagents) but never *more* than the model supports. If no
    # override is given, use the provider cap as-is.
    if max_tokens is None or max_tokens <= 0:
        effective_max_tokens = provider_cap
    else:
        effective_max_tokens = min(max_tokens, provider_cap)

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


def _apply_cache_control(formatted_msg: dict) -> bool:
    """Attach `cache_control: ephemeral` to a formatted OpenAI message.

    Returns True if the marker was applied (i.e., the message had cacheable
    content and wasn't already tagged). Skips messages whose content is not
    a string or block list, messages already marked, and empty content.

    Used by `CachedOpenAIChat` to tag system + last N user/assistant messages
    for providers that honor OpenAI-style content blocks with `cache_control`
    (DashScope/Qwen, and any OpenAI-compatible endpoint that mirrors the
    Anthropic cache_control convention).
    """
    content = formatted_msg.get("content")
    cache_tag = {"type": "ephemeral"}
    if isinstance(content, str):
        if not content:
            return False
        formatted_msg["content"] = [
            {"type": "text", "text": content, "cache_control": cache_tag}
        ]
        return True
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = cache_tag
            return True
    return False


def _make_cached_openai_chat_class(mark_recent_messages: bool = False):
    """Create a CachedOpenAIChat subclass that injects prompt-cache markers.

    DashScope (Qwen) and other OpenAI-compatible APIs support explicit prompt
    caching via `cache_control: {"type": "ephemeral"}` on content blocks. This
    subclass tags:

    1. The **system message** — always. This is the minimum cache coverage
       and is safe for any OpenAI-compatible provider that supports the marker
       (unknown fields are ignored by providers that don't).

    2. The **last 2 non-system / non-tool messages** — only when
       `mark_recent_messages=True`. This unlocks prefix caching for the growing
       conversation history (the big win: 5-8× cost reduction on multi-turn
       sessions), but is gated because OpenAI's own API may not accept the
       marker on user/assistant messages. The flag is wired from
       `_create_provider_model` based on whether the provider has a custom
       `base_url` — a strong signal that we're talking to a non-official
       OpenAI endpoint (Qwen/DashScope/custom) that mirrors the Anthropic
       convention.

    Implementation: each of the 4 invoke methods (invoke/ainvoke plus stream
    variants) pre-formats the full batch using the parent's `_format_message`,
    tags the target messages via `_apply_cache_control`, stores the tagged
    versions in `self._current_cache_tag_map` keyed by `id(original)`, and
    then delegates to `super().<method>()`. The overridden `_format_message`
    consults the map and returns the pre-tagged version when present.
    """
    from agno.models.openai import OpenAIChat
    from agno.models.message import Message

    class CachedOpenAIChat(OpenAIChat):
        _cache_recent_messages: bool = mark_recent_messages

        # --- core hook ------------------------------------------------------

        def _format_message(self, message: Message, compress_tool_results: bool = False):
            # If an invoke-level pre-tag map is active, use the tagged version
            tag_map = getattr(self, "_current_cache_tag_map", None)
            if tag_map is not None:
                pre = tag_map.get(id(message))
                if pre is not None:
                    return pre

            # Otherwise fall back to parent format + always-tag system
            formatted = super()._format_message(message, compress_tool_results)
            if message.role == "system":
                _apply_cache_control(formatted)
            return formatted

        # --- batch pre-tagging ---------------------------------------------

        def _build_cache_tag_map(self, messages, compress_tool_results: bool) -> dict:
            """Format all messages up-front and tag system + last 2 recent.

            Returns id(original_message) -> tagged formatted dict so the
            overridden `_format_message` can substitute during super's
            inline list comprehension.

            Note: `OpenAIChat._format_message` rewrites `system` → `developer`
            for newer OpenAI models. We check `Message.role` on the ORIGINAL
            message (not the formatted dict) so the logic works regardless of
            that rewrite.
            """
            # Use OpenAIChat's format directly (not self's) so the tag_map
            # we're building doesn't cause recursive substitution.
            base = [
                OpenAIChat._format_message(self, m, compress_tool_results)
                for m in messages
            ]

            # Tag the first system message (first Message with role=="system")
            for orig, fmt in zip(messages, base):
                if orig.role == "system":
                    _apply_cache_control(fmt)
                    break

            # Optionally tag the last 2 non-system / non-tool messages.
            # Iterate original+formatted in reverse so role checks stay
            # on the unmodified Message role.
            if self._cache_recent_messages:
                marked = 0
                for orig, fmt in zip(reversed(messages), reversed(base)):
                    if marked >= 2:
                        break
                    if orig.role in ("system", "tool"):
                        continue
                    if _apply_cache_control(fmt):
                        marked += 1

            return {id(orig): fmt for orig, fmt in zip(messages, base)}

        # --- invoke overrides: set up tag map, delegate to parent -----------

        def invoke(self, messages, assistant_message, **kwargs):
            compress = kwargs.get("compress_tool_results", False)
            self._current_cache_tag_map = self._build_cache_tag_map(messages, compress)
            try:
                return super().invoke(messages, assistant_message, **kwargs)
            finally:
                self._current_cache_tag_map = None

        async def ainvoke(self, messages, assistant_message, **kwargs):
            compress = kwargs.get("compress_tool_results", False)
            self._current_cache_tag_map = self._build_cache_tag_map(messages, compress)
            try:
                return await super().ainvoke(messages, assistant_message, **kwargs)
            finally:
                self._current_cache_tag_map = None

        def invoke_stream(self, messages, assistant_message, **kwargs):
            compress = kwargs.get("compress_tool_results", False)
            self._current_cache_tag_map = self._build_cache_tag_map(messages, compress)
            try:
                yield from super().invoke_stream(messages, assistant_message, **kwargs)
            finally:
                self._current_cache_tag_map = None

        async def ainvoke_stream(self, messages, assistant_message, **kwargs):
            compress = kwargs.get("compress_tool_results", False)
            self._current_cache_tag_map = self._build_cache_tag_map(messages, compress)
            try:
                async for item in super().ainvoke_stream(messages, assistant_message, **kwargs):
                    yield item
            finally:
                self._current_cache_tag_map = None

    return CachedOpenAIChat


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
        api_key = _resolve_api_key(provider)
        params = {"id": model_id, "max_tokens": max_tokens}
        if api_key:
            params["api_key"] = api_key
        if provider.base_url:
            params["base_url"] = provider.base_url
        if provider.options.get("use_system_role"):
            params["role_map"] = {
                "system": "system",
                "user": "user",
                "assistant": "assistant",
                "tool": "tool",
                "model": "assistant",
            }
        params.update(kwargs)
        if cache_system_prompt:
            # Only mark recent messages with cache_control when the provider
            # has a custom base_url (DashScope/Qwen/custom OpenAI-compat).
            # Official OpenAI's API may reject the marker on user/assistant
            # messages — for them, keep system-only caching.
            mark_recent = bool(provider.base_url)
            CachedOpenAIChat = _make_cached_openai_chat_class(
                mark_recent_messages=mark_recent
            )
            return CachedOpenAIChat(**params)
        from agno.models.openai import OpenAIChat
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
        api_key = _resolve_api_key(provider)
        params = {"id": model_id, "max_tokens": max_tokens}
        if api_key:
            params["api_key"] = api_key
        if provider.base_url:
            params["base_url"] = provider.base_url
        if provider.options.get("use_system_role"):
            params["role_map"] = {
                "system": "system",
                "user": "user",
                "assistant": "assistant",
                "tool": "tool",
                "model": "assistant",
            }
        params.update(kwargs)
        if cache_system_prompt:
            # Fallback branch always means "unknown OpenAI-compat provider"
            # — if there's a base_url it's a custom endpoint that may honor
            # the cache_control marker. Without base_url we're in an odd
            # state (unknown type, no endpoint) — default to system-only.
            mark_recent = bool(provider.base_url)
            CachedOpenAIChat = _make_cached_openai_chat_class(
                mark_recent_messages=mark_recent
            )
            return CachedOpenAIChat(**params)
        from agno.models.openai import OpenAIChat
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
