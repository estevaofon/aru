"""Tests for the AskUserQuestion tool (kimi-code parity).

The tool exists so the agent loop *waits* for the user's answer: the
question is a tool call that blocks on ``ctx.ui`` and returns the answers
as the tool result. Motivating bug (sessions 82619f98 / 9dcec80a,
kimi-k2.6): the model asked questions in plain text while also emitting
``update_task`` calls — the loop continued past the question and the
model "answered" itself instead of waiting for the user.

The PascalCase name and the ``questions`` schema are deliberate: they
match the tool kimi-k2.6 (kimi-code) and Claude (Claude Code) were
trained with, which is what makes the models actually call it instead of
asking in chat text.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from aru.runtime import get_ctx, install_permission_wait_gate, reset_permission_wait_gate
from aru.tools.ask_user import (
    DISMISSED_NOTE,
    OTHER_LABEL,
    UNSUPPORTED_MESSAGE,
    AskUserQuestion,
)


class FakeUI:
    """Minimal UIAdapter stand-in that scripts answers and records calls."""

    def __init__(self, choices=None, texts=None, on_prompt=None):
        # ``choices`` / ``texts`` are consumed in order across questions.
        self.choices = list(choices or [])
        self.texts = list(texts or [])
        self.on_prompt = on_prompt
        self.choice_calls: list[dict] = []
        self.text_calls: list[str] = []

    def ask_choice(self, options, *, title=None, default=0, cancel_value=None, details=None):
        if self.on_prompt is not None:
            self.on_prompt()
        self.choice_calls.append(
            {"options": list(options), "title": title, "details": details}
        )
        return self.choices.pop(0) if self.choices else None

    def ask_text(self, prompt, *, default="", multiline=False, password=False):
        if self.on_prompt is not None:
            self.on_prompt()
        self.text_calls.append(prompt)
        return self.texts.pop(0) if self.texts else ""

    def print(self, renderable):
        pass

    def notify(self, message, severity="info"):
        pass


def q(question, options=None, header="", multi=False):
    item = {"question": question, "header": header, "multi_select": multi}
    if options is not None:
        item["options"] = options
    return item


def answers_of(result: str) -> dict:
    return json.loads(result)["answers"]


@pytest.fixture
def interactive_ctx():
    """Make the autouse test ctx interactive (fixture defaults to YOLO)."""
    ctx = get_ctx()
    ctx.skip_permissions = False
    ctx.permission_mode = "default"
    return ctx


class TestOptionsFlow:
    def test_selected_option_is_returned_as_answers_json(self, interactive_ctx):
        ui = FakeUI(choices=[1])
        interactive_ctx.ui = ui
        result = AskUserQuestion(
            [q("Qual estilo visual?", [{"label": "Pixel art"}, {"label": "Vetorial"}])]
        )
        assert answers_of(result) == {"Qual estilo visual?": "Vetorial"}

    def test_menu_gets_other_entry_appended(self, interactive_ctx):
        ui = FakeUI(choices=[0])
        interactive_ctx.ui = ui
        AskUserQuestion([q("Escopo?", [{"label": "MVP"}, {"label": "Completo"}])])
        assert ui.choice_calls, "ask_choice was never called"
        menu = ui.choice_calls[0]["options"]
        assert menu[:2] == ["MVP", "Completo"]
        assert menu[-1] == OTHER_LABEL

    def test_other_falls_back_to_free_text(self, interactive_ctx):
        # Index == len(options) selects the synthetic "Other" entry.
        ui = FakeUI(choices=[2], texts=["algo customizado"])
        interactive_ctx.ui = ui
        result = AskUserQuestion([q("Escopo?", [{"label": "MVP"}, {"label": "Completo"}])])
        assert ui.text_calls, "ask_text fallback was never reached"
        assert answers_of(result) == {"Escopo?": "algo customizado"}

    def test_escape_returns_dismissed_json(self, interactive_ctx):
        ui = FakeUI(choices=[None])
        interactive_ctx.ui = ui
        result = AskUserQuestion([q("Escopo?", [{"label": "MVP"}, {"label": "Completo"}])])
        payload = json.loads(result)
        assert payload["answers"] == {}
        assert payload["note"] == DISMISSED_NOTE


class TestMultiQuestionFlow:
    def test_questions_are_asked_sequentially(self, interactive_ctx):
        ui = FakeUI(choices=[0, 1])
        interactive_ctx.ui = ui
        result = AskUserQuestion(
            [
                q("Estilo?", [{"label": "Pixel"}, {"label": "Clean"}], header="Visual"),
                q("Telas?", [{"label": "Mínimo"}, {"label": "Completo"}]),
            ]
        )
        assert answers_of(result) == {"Estilo?": "Pixel", "Telas?": "Completo"}
        assert len(ui.choice_calls) == 2

    def test_dismissal_midway_returns_partial_answers(self, interactive_ctx):
        ui = FakeUI(choices=[0, None])
        interactive_ctx.ui = ui
        result = AskUserQuestion(
            [
                q("Estilo?", [{"label": "Pixel"}, {"label": "Clean"}]),
                q("Telas?", [{"label": "Mínimo"}, {"label": "Completo"}]),
            ]
        )
        payload = json.loads(result)
        assert payload["answers"] == {"Estilo?": "Pixel"}
        assert payload["note"] == DISMISSED_NOTE

    def test_multi_select_degrades_to_listed_free_text(self, interactive_ctx):
        ui = FakeUI(texts=["Escudo, Velocidade"])
        interactive_ctx.ui = ui
        result = AskUserQuestion(
            [q("Power-ups?", [{"label": "Escudo"}, {"label": "Velocidade"}], multi=True)]
        )
        assert answers_of(result) == {"Power-ups?": "Escudo, Velocidade"}
        assert "Escudo" in ui.text_calls[0]

    def test_more_than_four_questions_rejected(self, interactive_ctx):
        interactive_ctx.ui = FakeUI()
        result = AskUserQuestion([q(f"P{i}?") for i in range(5)])
        assert result.startswith("Error:")


class TestFreeTextFlow:
    def test_question_without_options_uses_free_text(self, interactive_ctx):
        ui = FakeUI(texts=["Infinito horizontal"])
        interactive_ctx.ui = ui
        result = AskUserQuestion([q("Qual o estilo de gameplay?")])
        assert ui.text_calls, "ask_text was never called"
        assert answers_of(result) == {"Qual o estilo de gameplay?": "Infinito horizontal"}

    def test_empty_answer_counts_as_dismissed(self, interactive_ctx):
        ui = FakeUI(texts=["   "])
        interactive_ctx.ui = ui
        result = AskUserQuestion([q("Qual o estilo de gameplay?")])
        assert json.loads(result)["note"] == DISMISSED_NOTE

    def test_ui_exception_degrades_to_dismissed(self, interactive_ctx):
        # TuiUI raises RuntimeError when its modal times out (300s) — the
        # tool must hand the model a calm dismissal, not a traceback.
        class ExplodingUI(FakeUI):
            def ask_text(self, *a, **k):
                raise RuntimeError("TuiUI modal timed out after 300s")

        interactive_ctx.ui = ExplodingUI()
        result = AskUserQuestion([q("Qual o estilo de gameplay?")])
        assert json.loads(result)["note"] == DISMISSED_NOTE


class TestInputCoercion:
    """The write_files lesson: models mis-call nested schemas — salvage them."""

    def test_bare_string_becomes_free_text_question(self, interactive_ctx):
        ui = FakeUI(texts=["sim"])
        interactive_ctx.ui = ui
        result = AskUserQuestion("Posso seguir?")
        assert answers_of(result) == {"Posso seguir?": "sim"}

    def test_single_dict_instead_of_list(self, interactive_ctx):
        ui = FakeUI(texts=["ok"])
        interactive_ctx.ui = ui
        result = AskUserQuestion({"question": "Posso seguir?"})
        assert answers_of(result) == {"Posso seguir?": "ok"}

    def test_options_as_plain_strings(self, interactive_ctx):
        ui = FakeUI(choices=[1])
        interactive_ctx.ui = ui
        result = AskUserQuestion([q("Escopo?", ["MVP", "Completo"])])
        assert answers_of(result) == {"Escopo?": "Completo"}

    def test_camel_case_multi_select_accepted(self, interactive_ctx):
        ui = FakeUI(texts=["ambos"])
        interactive_ctx.ui = ui
        result = AskUserQuestion(
            [{"question": "Power-ups?", "options": ["Escudo", "Velocidade"], "multiSelect": True}]
        )
        assert answers_of(result) == {"Power-ups?": "ambos"}

    def test_background_flag_is_ignored(self, interactive_ctx):
        ui = FakeUI(texts=["resposta"])
        interactive_ctx.ui = ui
        result = AskUserQuestion([q("Pergunta?")], background=True)
        assert answers_of(result) == {"Pergunta?": "resposta"}

    def test_empty_questions_rejected(self, interactive_ctx):
        interactive_ctx.ui = FakeUI()
        assert AskUserQuestion([]).startswith("Error:")

    def test_question_without_text_rejected(self, interactive_ctx):
        interactive_ctx.ui = FakeUI()
        assert AskUserQuestion([{"header": "X"}]).startswith("Error:")


class TestModeGuards:
    def test_questions_still_work_in_yolo_mode(self):
        """Deliberate divergence from kimi-code's auto-mode deny: aru's YOLO
        skips *permission prompts*, not conversation. Users live in YOLO and
        still expect brainstorming/planning questions (session a7fccb5f:
        the deny made the model "decide itself" mid-brainstorm)."""
        ctx = get_ctx()
        ctx.skip_permissions = True
        ctx.permission_mode = "yolo"
        ctx.ui = FakeUI(choices=[0])
        result = AskUserQuestion([q("Continuar?", ["Sim", "Não"])])
        assert answers_of(result) == {"Continuar?": "Sim"}
        assert ctx.ui.choice_calls, "must prompt normally in YOLO mode"

    def test_non_interactive_session_is_refused(self, interactive_ctx, monkeypatch):
        # Piped one-shot: ReplUI installed, stdin is not a tty.
        from aru.ui import ReplUI

        interactive_ctx.ui = ReplUI(console=interactive_ctx.console)
        interactive_ctx.tui_app = None

        class NotATty:
            def isatty(self):
                return False

        monkeypatch.setattr("sys.stdin", NotATty())
        assert AskUserQuestion([q("Continuar?")]) == UNSUPPORTED_MESSAGE

    def test_no_ui_and_no_tty_is_refused(self, interactive_ctx, monkeypatch):
        interactive_ctx.ui = None
        interactive_ctx.tui_app = None

        class NotATty:
            def isatty(self):
                return False

        monkeypatch.setattr("sys.stdin", NotATty())
        assert AskUserQuestion([q("Continuar?")]) == UNSUPPORTED_MESSAGE


class TestWaitGate:
    def test_gate_is_held_while_blocked_on_user(self, interactive_ctx):
        """While the user thinks, the permission-wait gate must be active so
        ``_thread_tool`` timeouts are suspended (same machinery as
        ``check_permission`` — human time is not tool-execution budget)."""
        gate, token = install_permission_wait_gate()
        seen = {}

        def on_prompt():
            seen["active"] = gate.active

        try:
            ui = FakeUI(choices=[0], on_prompt=on_prompt)
            interactive_ctx.ui = ui
            AskUserQuestion([q("Escopo?", ["MVP", "Completo"])])
        finally:
            reset_permission_wait_gate(token)

        assert seen.get("active") is True
        assert gate.active is False


class TestBatchSerialization:
    """Agno gathers every tool call of one LLM response concurrently
    (agno/models/base.py arun_function_calls → asyncio.gather). Without a
    latch, the agent visibly keeps working — panels updating, files being
    read — while AskUserQuestion waits on the user (session 2750a12c).
    Kimi-code serializes this via ToolScheduler resource conflicts; aru's
    equivalent is runtime.UserQuestionGate applied in the universal tool
    wrapper."""

    @staticmethod
    def _make_tools(order, release):
        async def AskUserQuestion(questions: list) -> str:
            order.append("question:start")
            await release.wait()
            order.append("question:end")
            return '{"answers": {}}'

        async def update_task(index: int, status: str) -> str:
            order.append("update_task")
            return "ok"

        from aru.agent_factory import _wrap_tools_with_hooks

        return _wrap_tools_with_hooks([AskUserQuestion, update_task])

    async def test_tools_after_question_wait_for_the_answer(self):
        order: list[str] = []
        release = asyncio.Event()
        wrapped_q, wrapped_u = self._make_tools(order, release)

        batch = asyncio.gather(
            wrapped_q(questions=[{"question": "Escopo?"}]),
            wrapped_u(index=1, status="in_progress"),
        )
        await asyncio.sleep(0.05)
        assert order == ["question:start"], (
            "update_task must be parked while the question is open"
        )
        release.set()
        await batch
        assert order == ["question:start", "question:end", "update_task"]

    async def test_tools_before_question_are_not_blocked(self):
        # Provider order is preserved (kimi parity): a tool scheduled
        # BEFORE the question in the same batch executes immediately.
        order: list[str] = []
        release = asyncio.Event()
        wrapped_q, wrapped_u = self._make_tools(order, release)

        batch = asyncio.gather(
            wrapped_u(index=1, status="in_progress"),
            wrapped_q(questions=[{"question": "Escopo?"}]),
        )
        await asyncio.sleep(0.05)
        assert order == ["update_task", "question:start"]
        release.set()
        await batch

    async def test_gate_released_when_question_tool_raises(self):
        from aru.agent_factory import _wrap_tools_with_hooks
        from aru.runtime import get_user_question_gate

        async def AskUserQuestion(questions: list) -> str:
            raise RuntimeError("prompt machinery exploded")

        (wrapped_q,) = _wrap_tools_with_hooks([AskUserQuestion])
        with pytest.raises(RuntimeError):
            await wrapped_q(questions=[])
        assert get_user_question_gate().active is False

    def test_gate_depth_counts_nested_questions(self):
        from aru.runtime import UserQuestionGate

        gate = UserQuestionGate()
        assert gate.active is False
        gate.begin()
        gate.begin()
        gate.end()
        assert gate.active is True, "still one question open"
        gate.end()
        assert gate.active is False

    def test_fork_ctx_gets_its_own_gate(self):
        from aru.runtime import fork_ctx, get_user_question_gate

        parent_gate = get_user_question_gate()
        parent_gate.begin()
        try:
            forked = fork_ctx()
            forked_gate = getattr(forked, "user_question_gate", None)
            assert forked_gate is not parent_gate, (
                "a parent's open question must not freeze subagent tools"
            )
        finally:
            parent_gate.end()


class TestWiring:
    def test_registered_as_async_thread_tool(self):
        from aru.tools.registry import ALL_TOOLS, GENERAL_TOOLS, TOOL_REGISTRY

        assert "AskUserQuestion" in TOOL_REGISTRY
        wrapper = TOOL_REGISTRY["AskUserQuestion"]
        assert inspect.iscoroutinefunction(wrapper), (
            "AskUserQuestion must be wrapped by _thread_tool — it blocks on "
            "the user and would deadlock the event loop if run inline"
        )
        names = {getattr(t, "__name__", "") for t in ALL_TOOLS}
        assert "AskUserQuestion" in names
        names = {getattr(t, "__name__", "") for t in GENERAL_TOOLS}
        assert "AskUserQuestion" in names

    def test_excluded_from_subagent_planner_explorer_sets(self):
        import aru.tools.registry as reg
        from aru.tools.delegate import _DEFAULT_SUBAGENT_TOOLS

        for toolset in (_DEFAULT_SUBAGENT_TOOLS, reg.PLANNER_TOOLS, reg.EXPLORER_TOOLS):
            names = {getattr(t, "__name__", "") for t in toolset}
            assert "AskUserQuestion" not in names

    def test_allowed_in_plan_mode(self):
        # Clarifying questions are the whole point of planning — the tool
        # must pass the plan-mode gate (kimi parity: AskUserQuestion is one
        # of the two valid ways to end a plan-mode turn).
        class StubSession:
            plan_mode = True

        ctx = get_ctx()
        ctx.session = StubSession()
        from aru.tool_policy import evaluate_tool_policy

        assert evaluate_tool_policy("AskUserQuestion").allowed
        assert not evaluate_tool_policy("edit_file").allowed  # sanity

    def test_base_instructions_teach_the_wait_rule(self):
        from aru.agents.base import BASE_INSTRUCTIONS

        assert "AskUserQuestion" in BASE_INSTRUCTIONS
