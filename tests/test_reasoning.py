"""Unit tests for reasoning/thinking parameter routing in aru.providers."""

from unittest.mock import patch, MagicMock

import pytest

from aru.providers import (
    ProviderConfig,
    ReasoningConfig,
    _EFFORT_TO_BUDGET,
    _get_reasoning_config,
    _merge_reasoning,
    _resolve_reasoning_params,
    create_model,
    register_provider,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _provider(
    base_url: str | None = None,
    reasoning_effort: str | None = None,
    models: dict | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name="Test",
        models=models or {},
        base_url=base_url,
        reasoning_effort=reasoning_effort,
    )


# ---------------------------------------------------------------------------
# _resolve_reasoning_params
# ---------------------------------------------------------------------------

class TestResolveReasoningParams:
    """Each test asserts the exact dict that should reach the Agno constructor."""

    def _r(self, effort="high", budget_tokens=None, enabled=True):
        return ReasoningConfig(effort=effort, budget_tokens=budget_tokens, enabled=enabled)

    # --- anthropic adaptive (Sonnet 4+, Opus 4+) ---

    def test_anthropic_adaptive_model(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "anthropic", provider, "claude-sonnet-4-5-20250929", self._r(), max_tokens=64000
        )
        assert result == {
            "thinking": {"type": "adaptive"},
            "betas": ["interleaved-thinking-2025-05-14"],
        }

    def test_anthropic_adaptive_opus(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "anthropic", provider, "claude-opus-4-6", self._r(), max_tokens=128000
        )
        assert result["thinking"] == {"type": "adaptive"}
        assert "interleaved-thinking-2025-05-14" in result["betas"]

    # --- anthropic budget (Sonnet 3.7) ---

    def test_anthropic_budget_model_uses_effort_table(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "anthropic", provider, "claude-3-7-sonnet-20250219", self._r(effort="high"), max_tokens=64000
        )
        assert result["thinking"]["type"] == "enabled"
        assert result["thinking"]["budget_tokens"] == _EFFORT_TO_BUDGET["high"]

    def test_anthropic_budget_model_explicit_budget_overrides_effort(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "anthropic", provider, "claude-3-7-sonnet-20250219",
            self._r(effort="low", budget_tokens=12345), max_tokens=64000
        )
        assert result["thinking"]["budget_tokens"] == 12345

    def test_anthropic_budget_clamped_to_max_tokens(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "anthropic", provider, "claude-3-7-sonnet-20250219",
            self._r(effort="max"), max_tokens=1000
        )
        # budget must be < max_tokens
        assert result["thinking"]["budget_tokens"] < 1000

    # --- anthropic non-thinking (Haiku 3.5) ---

    def test_anthropic_non_thinking_model_returns_empty(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "anthropic", provider, "claude-3-5-haiku-20241022", self._r(), max_tokens=8192
        )
        assert result == {}

    def test_anthropic_non_thinking_alias_returns_empty(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "anthropic", provider, "claude-haiku-3-5-20241022", self._r(), max_tokens=8192
        )
        assert result == {}

    # --- openrouter ---

    def test_openrouter_returns_reasoning_effort_in_extra_body(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "openrouter", provider, "anthropic/claude-sonnet-4-5", self._r(effort="medium"), max_tokens=64000
        )
        assert result == {"extra_body": {"reasoning": {"effort": "medium"}}}

    def test_openrouter_low_effort(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "openrouter", provider, "qwen/qwen3.6-plus", self._r(effort="low"), max_tokens=16384
        )
        assert result["extra_body"]["reasoning"]["effort"] == "low"

    # --- dashscope (openai type with dashscope base_url) ---

    def test_dashscope_returns_enable_thinking(self):
        provider = _provider(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
        result = _resolve_reasoning_params(
            "openai", provider, "qwen3.6-plus", self._r(effort="high"), max_tokens=16384
        )
        assert result == {
            "extra_body": {
                "enable_thinking": True,
                "thinking_budget": _EFFORT_TO_BUDGET["high"],
                "preserve_thinking": True,
            }
        }

    def test_dashscope_explicit_budget_tokens(self):
        provider = _provider(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
        result = _resolve_reasoning_params(
            "openai", provider, "qwen3.6-plus", self._r(budget_tokens=5000), max_tokens=16384
        )
        assert result["extra_body"]["thinking_budget"] == 5000

    def test_dashscope_preserve_thinking_always_set(self):
        provider = _provider(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
        result = _resolve_reasoning_params(
            "openai", provider, "qwen3-plus", self._r(effort="medium"), max_tokens=16384
        )
        assert result["extra_body"]["preserve_thinking"] is True

    def test_dashscope_intl_domain_detected(self):
        """International DashScope endpoint (-intl) must also route to enable_thinking."""
        provider = _provider(base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
        result = _resolve_reasoning_params(
            "openai", provider, "qwen3.6-plus", self._r(effort="high"), max_tokens=16384
        )
        assert result["extra_body"]["enable_thinking"] is True
        assert result["extra_body"]["preserve_thinking"] is True

    # --- deepseek no-op ---

    def test_deepseek_returns_empty(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "deepseek", provider, "deepseek-reasoner", self._r(), max_tokens=16384
        )
        assert result == {}

    # --- disabled ---

    def test_disabled_config_returns_empty_for_any_provider(self):
        provider = _provider()
        cfg = ReasoningConfig(enabled=False)
        for pt, mid in [
            ("anthropic", "claude-sonnet-4-5-20250929"),
            ("openrouter", "some/model"),
            ("openai", "gpt-4o"),
            ("deepseek", "deepseek-reasoner"),
        ]:
            assert _resolve_reasoning_params(pt, provider, mid, cfg, 64000) == {}

    # --- generic openai-compat (no DashScope base_url) ---

    def test_generic_openai_compat_returns_reasoning_effort(self):
        provider = _provider(base_url="https://my-proxy.example.com/v1")
        result = _resolve_reasoning_params(
            "openai", provider, "o3-mini", self._r(effort="high"), max_tokens=4096
        )
        assert result == {"reasoning_effort": "high"}

    def test_openai_direct_no_base_url_returns_reasoning_effort(self):
        provider = _provider()
        result = _resolve_reasoning_params(
            "openai", provider, "o3-mini", self._r(effort="low"), max_tokens=4096
        )
        assert result == {"reasoning_effort": "low"}


# ---------------------------------------------------------------------------
# _get_reasoning_config
# ---------------------------------------------------------------------------

class TestGetReasoningConfig:
    def test_model_level_effort(self):
        provider = _provider(models={
            "qwen3.6-plus": {"id": "qwen3.6-plus", "reasoning": {"effort": "high"}}
        })
        cfg = _get_reasoning_config(provider, "qwen3.6-plus")
        assert cfg is not None
        assert cfg.effort == "high"
        assert cfg.enabled is True

    def test_model_level_effort_and_budget(self):
        provider = _provider(models={
            "qwen3.6-plus": {"id": "qwen3.6-plus", "reasoning": {"effort": "medium", "budget_tokens": 5000}}
        })
        cfg = _get_reasoning_config(provider, "qwen3.6-plus")
        assert cfg.effort == "medium"
        assert cfg.budget_tokens == 5000

    def test_model_level_disabled(self):
        provider = _provider(models={
            "tiny": {"id": "tiny-v1", "reasoning": {"enabled": False}}
        })
        cfg = _get_reasoning_config(provider, "tiny")
        assert cfg is not None
        assert cfg.enabled is False

    def test_model_level_false_shorthand(self):
        provider = _provider(models={
            "tiny": {"id": "tiny-v1", "reasoning": False}
        })
        cfg = _get_reasoning_config(provider, "tiny")
        assert cfg is not None
        assert cfg.enabled is False

    def test_provider_level_fallback(self):
        provider = _provider(reasoning_effort="medium")
        cfg = _get_reasoning_config(provider, "some-model-not-in-registry")
        assert cfg is not None
        assert cfg.effort == "medium"

    def test_model_config_takes_precedence_over_provider(self):
        provider = _provider(
            reasoning_effort="low",
            models={"special": {"id": "special-v1", "reasoning": {"effort": "max"}}}
        )
        cfg = _get_reasoning_config(provider, "special")
        assert cfg.effort == "max"

    def test_no_config_returns_none(self):
        provider = _provider()
        cfg = _get_reasoning_config(provider, "unknown-model")
        assert cfg is None

    def test_model_without_reasoning_key_falls_back_to_provider(self):
        provider = _provider(
            reasoning_effort="high",
            models={"plain": {"id": "plain-v1", "max_tokens": 4096}}
        )
        cfg = _get_reasoning_config(provider, "plain")
        assert cfg.effort == "high"


# ---------------------------------------------------------------------------
# _merge_reasoning — extra_body merge safety
# ---------------------------------------------------------------------------

class TestMergeReasoning:
    def test_plain_key_added(self):
        params: dict = {"id": "m", "max_tokens": 4096}
        _merge_reasoning(params, {"reasoning_effort": "high"})
        assert params["reasoning_effort"] == "high"

    def test_extra_body_merged_not_replaced(self):
        params: dict = {"id": "m", "extra_body": {"models": ["fallback-1"]}}
        _merge_reasoning(params, {"extra_body": {"enable_thinking": True}})
        assert params["extra_body"]["models"] == ["fallback-1"]
        assert params["extra_body"]["enable_thinking"] is True

    def test_extra_body_created_when_absent(self):
        params: dict = {"id": "m"}
        _merge_reasoning(params, {"extra_body": {"enable_thinking": True}})
        assert params["extra_body"] == {"enable_thinking": True}

    def test_empty_reasoning_params_noop(self):
        params: dict = {"id": "m", "max_tokens": 100}
        _merge_reasoning(params, {})
        assert params == {"id": "m", "max_tokens": 100}

    def test_non_dict_extra_body_in_params_is_replaced(self):
        params: dict = {"id": "m", "extra_body": "bad-string"}
        _merge_reasoning(params, {"extra_body": {"enable_thinking": True}})
        # string extra_body can't be merged — replace it
        assert params["extra_body"] == {"enable_thinking": True}


# ---------------------------------------------------------------------------
# create_model reasoning_override — session-level effort override
# ---------------------------------------------------------------------------

class TestReasoningOverride:
    """create_model's reasoning_override wins over provider/model config."""

    def _register_test_provider(self, base_url: str | None = None, model_reasoning: dict | None = None):
        """Register an isolated test provider so we don't mutate BUILTIN ones."""
        models: dict = {"test-model": {"id": "test-model", "max_tokens": 8192}}
        if model_reasoning is not None:
            models["test-model"]["reasoning"] = model_reasoning
        provider = ProviderConfig(
            name="TestOverride",
            api_key_env="TEST_KEY",
            base_url=base_url,
            default_model="test-model",
            models=models,
            options={"_provider_type": "openai"},
        )
        register_provider("test-override", provider)
        return provider

    def test_override_beats_model_config(self):
        """Session override 'high' should win even when model config says 'low'."""
        self._register_test_provider(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_reasoning={"effort": "low"},
        )
        with patch("aru.providers._make_cached_openai_chat_class") as mock_cls:
            created_params = {}
            def _capture(**kwargs):
                nonlocal created_params
                created_params.update(kwargs)
                return MagicMock()
            mock_cls.return_value = _capture
            create_model("test-override/test-model", reasoning_override="high")
            # DashScope path → extra_body with enable_thinking + high budget
            assert "extra_body" in created_params
            assert created_params["extra_body"]["enable_thinking"] is True
            assert created_params["extra_body"]["thinking_budget"] == _EFFORT_TO_BUDGET["high"]

    def test_override_off_disables_thinking(self):
        """Session override 'off' suppresses thinking even with model config set."""
        self._register_test_provider(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_reasoning={"effort": "high"},
        )
        with patch("aru.providers._make_cached_openai_chat_class") as mock_cls:
            created_params = {}
            def _capture(**kwargs):
                created_params.update(kwargs)
                return MagicMock()
            mock_cls.return_value = _capture
            create_model("test-override/test-model", reasoning_override="off")
            # No reasoning params should leak through
            assert "extra_body" not in created_params or "enable_thinking" not in created_params.get("extra_body", {})

    def test_override_none_uses_model_config(self):
        """reasoning_override=None falls back to model's configured reasoning."""
        self._register_test_provider(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_reasoning={"effort": "medium"},
        )
        with patch("aru.providers._make_cached_openai_chat_class") as mock_cls:
            created_params = {}
            def _capture(**kwargs):
                created_params.update(kwargs)
                return MagicMock()
            mock_cls.return_value = _capture
            create_model("test-override/test-model", reasoning_override=None)
            assert created_params["extra_body"]["thinking_budget"] == _EFFORT_TO_BUDGET["medium"]

    def test_override_case_insensitive(self):
        """Override accepts 'HIGH' / 'Off' casing."""
        self._register_test_provider(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        with patch("aru.providers._make_cached_openai_chat_class") as mock_cls:
            created_params = {}
            def _capture(**kwargs):
                created_params.update(kwargs)
                return MagicMock()
            mock_cls.return_value = _capture
            create_model("test-override/test-model", reasoning_override="HIGH")
            assert created_params["extra_body"]["thinking_budget"] == _EFFORT_TO_BUDGET["high"]

    def test_use_reasoning_false_skips_override(self):
        """use_reasoning=False (e.g. explorer) bypasses the override entirely."""
        self._register_test_provider(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_reasoning={"effort": "high"},
        )
        with patch("aru.providers._make_cached_openai_chat_class") as mock_cls:
            created_params = {}
            def _capture(**kwargs):
                created_params.update(kwargs)
                return MagicMock()
            mock_cls.return_value = _capture
            create_model("test-override/test-model", use_reasoning=False, reasoning_override="max")
            assert "extra_body" not in created_params or "enable_thinking" not in created_params.get("extra_body", {})


# ---------------------------------------------------------------------------
# Session.reasoning_override persistence round-trip
# ---------------------------------------------------------------------------

class TestSessionPersistence:
    def test_default_is_none(self):
        from aru.session import Session
        s = Session()
        assert s.reasoning_override is None

    def test_round_trip_preserves_override(self):
        from aru.session import Session
        s = Session()
        s.reasoning_override = "high"
        data = s.to_dict()
        assert data["reasoning_override"] == "high"
        loaded = Session.from_dict(data)
        assert loaded.reasoning_override == "high"

    def test_round_trip_preserves_off(self):
        from aru.session import Session
        s = Session()
        s.reasoning_override = "off"
        loaded = Session.from_dict(s.to_dict())
        assert loaded.reasoning_override == "off"

    def test_round_trip_none_stays_none(self):
        from aru.session import Session
        s = Session()
        loaded = Session.from_dict(s.to_dict())
        assert loaded.reasoning_override is None

    def test_legacy_session_without_field_loads_as_none(self):
        """Sessions saved before this feature existed load cleanly."""
        from aru.session import Session
        legacy_data = {
            "session_id": "test-123",
            "history": [],
            "current_plan": None,
            "plan_task": None,
            "plan_steps": [],
            "plan_mode": False,
            "active_skills": {},
            "invoked_skills": {},
            "model_ref": "anthropic/claude-sonnet-4-5",
            "cwd": "/tmp",
            "created_at": "2026-04-19T00:00:00",
            "updated_at": "2026-04-19T00:00:00",
            # no reasoning_override key
        }
        loaded = Session.from_dict(legacy_data)
        assert loaded.reasoning_override is None
