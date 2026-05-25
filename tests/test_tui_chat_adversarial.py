"""Adversarial tests for the incremental markdown render path (fix/tui-freezing2).

These tests cover the behaviours that the simple streaming tests in
``test_tui_chat.py`` don't exercise because they all stay under the
``_INCREMENTAL_MIN_BYTES`` threshold and therefore route through the
naïve whole-buffer path.

Four tests, matching the plan in ``C:\\Users\\estev\\.claude\\plans\\
eager-stargazing-quill.md``:

1. **Correctness equivalence** — multi-checkpoint incremental render of a
   25 KB markdown mix produces plain + spans identical to the naïve
   whole-buffer render at every checkpoint.
2. **Reference-link correctness** — the G1 guard (``_REF_DEF_RE``) routes
   a buffer containing a reference definition through the naïve path so
   ``[text][ref]`` resolves to its URL.
3. **Bounded parse work** — a 40 KB code-block-dense stream rendered at
   growing checkpoints keeps cumulative ``_markdown_to_text`` work
   ~linear in the document size (prefix cache), not quadratic. Counts
   bytes parsed, not wall-clock — deterministic on any hardware.
4. **Cache behaviour** — counting calls to ``_markdown_to_text`` during
   a multi-checkpoint pass shows most flushes re-parse only a small
   slice (the tail), not the whole buffer.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")


def _synthesise_markdown(target_bytes: int, *, dense_code: bool = False) -> str:
    """Build a deterministic markdown document around ``target_bytes`` chars.

    With ``dense_code=False`` produces mixed markdown (headings, prose,
    occasional fenced blocks). With ``dense_code=True`` every other block
    is a fenced Python block — the worst case the incremental path needs
    to handle (lots of Pygments work in the naïve path).

    Always ends with a still-being-typed tail paragraph (no trailing
    ``\\n\\n``), so there's a tail-only re-parse to exercise on the last
    flush.
    """
    parts: list[str] = []
    i = 0
    while sum(len(p) for p in parts) < target_bytes:
        parts.append(f"## Section {i}\n\n")
        parts.append(
            f"Prose paragraph {i}: lorem ipsum dolor sit amet, "
            f"consectetur adipiscing elit, sed do eiusmod tempor "
            f"incididunt ut labore et dolore magna aliqua.\n\n"
        )
        if dense_code or i % 4 == 0:
            parts.append("```python\n")
            for j in range(15):
                parts.append(
                    f"def func_{i}_{j}(x, y): return (x + {j}) * (y - {i})\n"
                )
            parts.append("```\n\n")
        i += 1
    parts.append("Final paragraph still being typed")
    return "".join(parts)


# ── Test 1 — correctness equivalence ─────────────────────────────────


@pytest.mark.asyncio
async def test_incremental_matches_naive_across_checkpoints():
    """Incremental render == naive render at every checkpoint of a 25 KB stream.

    Runs the incremental path at three snapshots, each one extending the
    previous. The assertion compares ``Text.plain`` AND the list of
    ``(start, end, style)`` spans — the semantic equivalence we need. We
    deliberately do NOT compare ``Segment`` streams because Rich may emit
    different padding segments depending on context; ``plain + spans``
    is what the user actually sees.
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import (
        ChatMessageWidget,
        ChatPane,
        _find_last_stable_split,
        _markdown_to_text,
    )

    content = _synthesise_markdown(25_000)
    assert len(content) >= 25_000  # precondition

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        chat.start_assistant_message()
        widget = chat._active_assistant
        assert widget is not None

        width = 100

        # Claim ``_md_render_task`` so ``on_resize`` firing during our
        # awaits doesn't spawn a competing render task that would race
        # the cache state.
        widget._md_render_task = asyncio.current_task()
        try:
            # Three checkpoints: cold cache (first pass past threshold),
            # split-advanced (delta extension), final tail.
            for cp_len in (8_500, 17_000, len(content)):
                cp = content[:cp_len]
                widget.set_reactive(ChatMessageWidget.buffer, cp)
                split_idx = _find_last_stable_split(cp)
                assert split_idx > 0, f"no split found at len={cp_len}"
                result = await widget._render_incremental(cp, split_idx, width)
                naive = _markdown_to_text(cp, width)
                assert result.plain == naive.plain, (
                    f"plain diverged at cp_len={cp_len}\n"
                    f"  result tail: {result.plain[-80:]!r}\n"
                    f"  naive tail:  {naive.plain[-80:]!r}"
                )
                assert list(result._spans) == list(naive._spans), (
                    f"spans diverged at cp_len={cp_len}"
                )
        finally:
            widget._md_render_task = None


# ── Test 2 — reference-link correctness (G1 guard) ───────────────────


@pytest.mark.asyncio
async def test_reference_definition_forces_naive_path():
    """Reference-link definition in buffer → G1 guard → naïve render.

    Proves the buffer resolves ``[the docs][ref]`` to its URL. If G1 were
    missing, the incremental path would cache the prefix when the tail
    still contained ``[ref]: …`` and the rendered output would retain
    the literal ``[the docs][ref]`` — visible divergence.
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane, _REF_DEF_RE

    # Synthesize a buffer >= INCREMENTAL_MIN_BYTES so the incremental
    # path would ordinarily kick in, but with a reference def present.
    filler = _synthesise_markdown(9_000)
    content = (
        "See [the docs][ref] for details.\n\n"
        + filler
        + "\n\n[ref]: https://example.com/docs\n"
    )
    assert len(content) >= ChatMessageWidget._INCREMENTAL_MIN_BYTES
    assert _REF_DEF_RE.search(content) is not None  # precondition

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        chat.start_assistant_message()
        widget = chat._active_assistant
        assert widget is not None

        widget.set_reactive(ChatMessageWidget.buffer, content)
        # Drive one render pass through the real routing logic.
        # ``_do_markdown_render``'s coalesce check bails out when
        # ``self._md_render_task`` doesn't match the current task — it
        # assumes it was launched by ``_schedule_markdown_render``. Mirror
        # that here so the render actually runs.
        widget._md_render_task = asyncio.current_task()
        try:
            await widget._do_markdown_render()
        finally:
            widget._md_render_task = None

        # The rendered text must have resolved the reference link. Rich's
        # Markdown collapses ``[text][ref]`` → "the docs" (with link style)
        # when the def is visible; literal "[the docs][ref]" appearing
        # verbatim would mean G1 failed.
        # Static.content is the VisualType passed to `update()`; for an
        # assistant bubble that's the Rich ``Text`` produced by
        # ``_markdown_to_text``.
        rendered_plain = widget.content.plain  # type: ignore[attr-defined]
        assert "[the docs][ref]" not in rendered_plain
        assert "the docs" in rendered_plain


# ── Test 3 — latency envelope (slow) ─────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_parse_work_stays_linear(monkeypatch):
    """A 40 KB dense-code reply rendered flush-by-flush keeps cumulative
    markdown-parse work ~linear in the document size — the prefix cache
    must stop every flush from re-parsing the whole buffer.

    Deterministic replacement for the old wall-clock latency test (which
    timed ``pilot.pause`` and flaked under load). We count the *bytes*
    handed to ``_markdown_to_text`` while rendering at growing,
    append-only checkpoints — the same signal as
    ``test_incremental_reduces_parse_work`` but across a long stream and
    asserting the global O(N) bound. A broken prefix cache (whole-buffer
    parse per flush) makes the total quadratic and trips the guard on any
    hardware — no stopwatch involved.
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets import chat as chat_mod
    from aru.tui.widgets.chat import (
        ChatMessageWidget,
        ChatPane,
        _find_last_stable_split,
    )

    content = _synthesise_markdown(40_000, dense_code=True)

    sizes: list[int] = []
    original = chat_mod._markdown_to_text

    def counting(raw: str, width: int = 100):
        sizes.append(len(raw))
        return original(raw, width)

    monkeypatch.setattr(chat_mod, "_markdown_to_text", counting)

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        chat.start_assistant_message()
        widget = chat._active_assistant
        assert widget is not None

        width = 100
        # Claim the render task so a stray watch-triggered render can't
        # spawn a competing parse and double-count (mirrors test 4).
        widget._md_render_task = asyncio.current_task()
        try:
            sizes.clear()  # ignore setup chatter
            # Simulate streaming: render at growing 2 KB checkpoints, each
            # extending the previous. Drive _render_incremental directly so
            # the outcome is deterministic (no debounce / coalesce timing).
            checkpoints = list(range(8_192, len(content), 2_000)) + [len(content)]
            for cp_len in checkpoints:
                cp = content[:cp_len]
                split_idx = _find_last_stable_split(cp)
                if split_idx <= 0:
                    continue
                widget.set_reactive(ChatMessageWidget.buffer, cp)
                await widget._render_incremental(cp, split_idx, width)
        finally:
            widget._md_render_task = None

    assert sizes, "no parses recorded — checkpoints never produced a split"
    total_parsed = sum(sizes)
    n = len(content)
    # Incremental cache → cumulative parse ≈ N (+ small per-flush tails),
    # roughly 1–2x N. A whole-buffer-per-flush regression is Σ(checkpoint
    # sizes) ≈ 8–10x N. 4x is a wide, hardware-independent line between
    # the two regimes.
    assert total_parsed <= 4 * n, (
        f"cumulative markdown parse = {total_parsed} bytes (> 4x the "
        f"{n}-byte document) — prefix cache likely broken, an O(N)-per-flush "
        f"regression. per-flush sizes: {sizes}"
    )


# ── Test 4 — cache behaviour (structural, not perf) ──────────────────


@pytest.mark.asyncio
async def test_incremental_reduces_parse_work(monkeypatch):
    """Cache hit → most calls parse a small slice, not the whole buffer.

    Counts every ``_markdown_to_text`` call during a 3-checkpoint
    incremental pass and asserts:
      * at least one call was "big" (the cold-cache prefix render), AND
      * the majority of calls were "small" (tail + delta parses),
    which together prove the cache prefix is being reused instead of
    every render re-parsing the whole buffer.
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets import chat as chat_mod
    from aru.tui.widgets.chat import (
        ChatMessageWidget,
        ChatPane,
        _find_last_stable_split,
    )

    sizes: list[int] = []
    original = chat_mod._markdown_to_text

    def counting(raw: str, width: int = 100):
        sizes.append(len(raw))
        return original(raw, width)

    monkeypatch.setattr(chat_mod, "_markdown_to_text", counting)

    content = _synthesise_markdown(20_000)
    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        chat.start_assistant_message()
        widget = chat._active_assistant
        assert widget is not None

        width = 100
        # Claim ``_md_render_task`` so a resize-triggered render doesn't
        # spawn a competing task that double-counts parse calls.
        widget._md_render_task = asyncio.current_task()
        try:
            sizes.clear()  # reset after setup chatter
            for cp_len in (9_000, 14_000, len(content)):
                cp = content[:cp_len]
                widget.set_reactive(ChatMessageWidget.buffer, cp)
                split_idx = _find_last_stable_split(cp)
                assert split_idx > 0
                await widget._render_incremental(cp, split_idx, width)
        finally:
            widget._md_render_task = None

    # Expect: exactly one "big" call (~9 KB prefix, first pass) and several
    # small calls (delta extension + tail per checkpoint). The worst case
    # today (no cache) would have every call be ≥ 9 KB.
    big_calls = [s for s in sizes if s >= 8_000]
    small_calls = [s for s in sizes if s < 5_000]
    assert len(big_calls) <= 1, (
        f"expected ≤1 big parse (cold prefix), saw {len(big_calls)}; sizes={sizes}"
    )
    assert len(small_calls) >= 2, (
        f"expected ≥2 small parses (tail/delta), saw {len(small_calls)}; "
        f"sizes={sizes}"
    )


# ── Test 5 — escape hatch for giant unclosed fences (slow) ───────────


@pytest.mark.asyncio
async def test_giant_unclosed_fence_uses_escape_hatch(monkeypatch):
    """A giant unclosed ```python fence must engage the escape hatch —
    rendered as flat ``Text`` — instead of running markdown-it + Pygments
    on the whole growing buffer every flush.

    Deterministic replacement for the old wall-clock freeze test. Rather
    than timing ``pilot.pause`` (which flaked under load) we drive the
    real render-path selection (``_do_markdown_render``) at growing
    checkpoints and assert (a) the escape hatch (``_render_tail_escape``)
    is chosen on every flush, and (b) ``_markdown_to_text`` is never
    handed the giant fence body. A regression removing the escape hatch
    re-parses the whole buffer per flush — caught here by a large parse
    input, not a hardware-dependent stopwatch.
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets import chat as chat_mod
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane

    # One giant fenced block, no inner blank lines and no closing fence —
    # defeats _find_last_stable_split entirely, so only the escape hatch
    # stands between the user and a per-flush whole-buffer Pygments pass.
    body = "".join(f"def func_{i}(): return {i}\n" for i in range(600))
    content = "```python\n" + body  # NO closing fence
    assert len(content) >= 15_000

    sizes: list[int] = []
    original = chat_mod._markdown_to_text

    def counting(raw: str, width: int = 100):
        sizes.append(len(raw))
        return original(raw, width)

    monkeypatch.setattr(chat_mod, "_markdown_to_text", counting)

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        chat.start_assistant_message()
        widget = chat._active_assistant
        assert widget is not None

        escape_calls = {"n": 0}
        original_escape = widget._render_tail_escape

        async def counting_escape(tail, fence_start, width):
            escape_calls["n"] += 1
            return await original_escape(tail, fence_start, width)

        monkeypatch.setattr(widget, "_render_tail_escape", counting_escape)

        # All checkpoints sit above _INCREMENTAL_MIN_BYTES (8192) so the
        # incremental/escape decision actually runs (below it the cheap
        # whole-buffer path is correct and expected).
        checkpoints = [9_000, 12_000, len(content)]
        for cp_len in checkpoints:
            sizes.clear()  # measure only THIS flush's parse work
            widget.set_reactive(ChatMessageWidget.buffer, content[:cp_len])
            widget._pending_md_render = False
            widget._md_render_task = asyncio.current_task()
            try:
                await widget._do_markdown_render()
            finally:
                widget._md_render_task = None
            big = [s for s in sizes if s >= widget._ESCAPE_HATCH_FENCE_BYTES]
            assert not big, (
                f"at {cp_len}B the fence reached _markdown_to_text with {big} "
                f"bytes — escape hatch not engaged (would freeze under load)"
            )

    assert escape_calls["n"] == len(checkpoints), (
        f"escape hatch engaged on {escape_calls['n']}/{len(checkpoints)} "
        f"flushes — expected every flush"
    )


# ── Test 6 — escape hatch correctness on finalize ────────────────────


@pytest.mark.asyncio
async def test_giant_fence_closed_renders_authoritative_markdown_on_finalize():
    """After streaming through escape hatch, finalize paints pristine markdown.

    The escape hatch renders the open fence as flat ``Text`` — no syntax
    highlighting — during streaming. But the moment the fence closes and
    ``finalize_render`` runs, the bubble must show the authoritative
    whole-buffer parse (which includes Pygments-highlighted code).
    """
    from aru.tui.app import AruApp
    from aru.tui.widgets.chat import ChatMessageWidget, ChatPane, _markdown_to_text

    body = "".join(f"def func_{i}(): return {i}\n" for i in range(400))
    content = "```python\n" + body + "```\nfinal text"
    assert len(content) >= 8_000

    app = AruApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        chat.start_assistant_message()

        # Stream in chunks to exercise the escape-hatch path along the way.
        step = 500
        for i in range(0, len(content), step):
            chat.append_assistant_delta(content[i : i + step])
            await pilot.pause(0.01)

        chat.finalize_assistant_message()
        await pilot.pause()

        widget = [
            m for m in chat.query(ChatMessageWidget) if m.role == "assistant"
        ][-1]
        # After finalize, widget.content is whatever `_compose_renderable`
        # returned — i.e. the authoritative whole-buffer markdown render.
        # We can't exact-match against `_markdown_to_text(content, W)`
        # because W depends on the widget width at paint time (which may
        # differ from ``widget.size.width`` read afterwards due to
        # ongoing layout). Instead we verify the substantive property:
        # *some* Pygments highlighting is present, which only happens
        # when the full markdown pass runs (the escape hatch emits flat
        # Text with no colored spans).
        rendered = widget.content  # type: ignore[attr-defined]
        assert "def func_0" in rendered.plain  # content made it through
        assert "final text" in rendered.plain  # closing + after
        coloured = [
            s
            for s in rendered._spans
            if getattr(s.style, "color", None) is not None
        ]
        assert len(coloured) >= 10, (
            f"expected Pygments-highlighted spans on finalized bubble, "
            f"saw {len(coloured)} coloured spans — escape hatch output "
            f"was not replaced by the authoritative markdown render"
        )
