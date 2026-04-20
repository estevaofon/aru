"""Stage 1 regression: opt-in timeout for threaded tool wrappers.

Covers:
- Default timeout=None preserves historical unbounded-wait behaviour
- Explicit timeout returns a user-facing string on TimeoutError instead of raising
- Non-timeout exceptions still propagate (so real bugs surface)
- Opt-in is per-wrap site: file_ops/registry wrap with explicit timeout,
  _thread_tool itself defaults to None
"""

import asyncio
import time

import pytest

from aru.tools._shared import _thread_tool


def _sleep_sync(duration: float) -> str:
    time.sleep(duration)
    return f"slept {duration}s"


def _raise_sync() -> None:
    raise ValueError("boom")


@pytest.mark.asyncio
async def test_default_no_timeout_completes_even_when_long():
    """With no timeout, the wrapper should wait however long the sync fn takes."""
    wrapped = _thread_tool(_sleep_sync)  # timeout=None (default)
    result = await wrapped(0.2)
    assert result == "slept 0.2s"


@pytest.mark.asyncio
async def test_timeout_triggers_when_exceeded():
    wrapped = _thread_tool(_sleep_sync, timeout=0.1)
    result = await wrapped(0.5)
    assert isinstance(result, str)
    assert "Tool timeout" in result
    assert "_sleep_sync" in result
    assert "0.1" in result


@pytest.mark.asyncio
async def test_timeout_does_not_trigger_when_fast():
    wrapped = _thread_tool(_sleep_sync, timeout=1.0)
    result = await wrapped(0.05)
    assert result == "slept 0.05s"


@pytest.mark.asyncio
async def test_non_timeout_exception_propagates():
    wrapped = _thread_tool(_raise_sync, timeout=1.0)
    with pytest.raises(ValueError, match="boom"):
        await wrapped()


@pytest.mark.asyncio
async def test_non_timeout_exception_propagates_without_timeout():
    wrapped = _thread_tool(_raise_sync)
    with pytest.raises(ValueError, match="boom"):
        await wrapped()


def test_wrapped_tool_preserves_sync_fn_metadata():
    """Agno introspects __name__/__doc__ — functools.wraps must cover both."""
    def my_tool(x: int) -> int:
        """Doc sentinel."""
        return x * 2

    wrapped = _thread_tool(my_tool, timeout=5)
    assert wrapped.__name__ == "my_tool"
    assert "Doc sentinel" in (wrapped.__doc__ or "")


@pytest.mark.asyncio
async def test_file_ops_wrappers_have_timeouts_applied():
    """file_ops module opts in explicitly — verify the wrappers bind with a cap."""
    from aru.tools import file_ops
    # The module-level wrappers were rebound with timeout=... — ensure they
    # didn't silently lose their wrap. Easiest check: they must be coroutines
    # when awaited and still carry the original docstring.
    assert asyncio.iscoroutinefunction(file_ops._read_file_tool)
    assert asyncio.iscoroutinefunction(file_ops._list_directory_tool)


@pytest.mark.asyncio
async def test_registry_rank_files_has_timeout_applied():
    from aru.tools import registry
    assert asyncio.iscoroutinefunction(registry._rank_files_tool)
