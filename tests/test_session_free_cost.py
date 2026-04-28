"""Free models must not accumulate dollar cost.

Regression: ``Session._get_pricing`` previously fell through to
``MODEL_PRICING["default"]`` (Sonnet-4.5 pricing) for any model id not
present in the table. OpenRouter's `:free` variants like
``openrouter/minimax/minimax-m2.5:free`` were therefore reported as
quite expensive in the status bar and `/cost` panel even though the
provider charges nothing.
"""

from __future__ import annotations

import pytest

from aru.session import Session


def _consume(session: Session, *, inp: int, out: int) -> None:
    session.total_input_tokens = inp
    session.total_output_tokens = out


def test_openrouter_free_variant_costs_zero():
    session = Session()
    session.model_ref = "openrouter/minimax/minimax-m2.5:free"
    _consume(session, inp=100_000, out=50_000)
    assert session.estimated_cost == 0.0


def test_openrouter_free_nemotron_costs_zero():
    session = Session()
    session.model_ref = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    _consume(session, inp=200_000, out=10_000)
    assert session.estimated_cost == 0.0


def test_paid_anthropic_still_costs_real_money():
    session = Session()
    session.model_ref = "anthropic/claude-sonnet-4-5"
    _consume(session, inp=100_000, out=50_000)
    assert session.estimated_cost > 0.0


def test_free_match_is_case_insensitive():
    session = Session()
    session.model_ref = "openrouter/minimax/minimax-m2.5:FREE"
    _consume(session, inp=10_000, out=5_000)
    assert session.estimated_cost == 0.0


def test_free_with_cache_tokens_still_zero():
    """All four price components must be zeroed, not just input/output."""
    session = Session()
    session.model_ref = "openrouter/minimax/minimax-m2.5:free"
    session.total_input_tokens = 100_000
    session.total_output_tokens = 50_000
    session.total_cache_read_tokens = 30_000
    session.total_cache_write_tokens = 20_000
    assert session.estimated_cost == 0.0
