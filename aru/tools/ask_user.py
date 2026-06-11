"""AskUserQuestion — structured question tool that WAITS for the user's answer.

Kimi-code parity (``AskUserQuestionTool``): the question is a *tool call*,
so the agent loop physically blocks until the user answers and the answer
comes back as the tool result. This is the structural fix for models that
ask a question in running text while also emitting tool calls — the loop
keeps feeding tool results back, the model keeps going, and it ends up
"answering" itself instead of waiting for the user (observed with
kimi-k2.6 in /brainstorming sessions).

The tool name and schema deliberately match kimi-code / Claude Code's
``AskUserQuestion`` (``questions: [{question, header, options: [{label,
description}], multi_select}]``) because models trained alongside those
CLIs — kimi-k2.6 is trained with kimi-code's toolset, Claude with Claude
Code's — know that exact tool and call it spontaneously. A same-purpose
tool under a foreign name (the first iteration here was ``ask_user``)
gets ignored in favour of plain-text questions, which nothing can force
to wait. Result strings also mirror kimi-code: answers come back as
``{"answers": {...}}`` JSON, dismissals as ``{"answers": {}, "note": ...}``.

Behavioural contract:

* Interactive sessions (TUI, or REPL with a tty): renders each question
  via ``ctx.ui`` — option menu (``ask_choice``) when ``options`` are
  given, free-text prompt (``ask_text``) otherwise. An "Other" entry is
  always appended to menus so the user can type a custom answer.
* YOLO / skip-permissions mode: questions still work. This deliberately
  DIVERGES from kimi-code (whose auto mode denies AskUserQuestion): aru's
  YOLO governs *permission prompts for mutations*, not conversation — aru
  users live in YOLO to skip approval spam and still expect brainstorming
  /planning questions (session a7fccb5f: the deny made the model "decide
  itself" mid-brainstorm). Unattended runs are covered by the
  non-interactive guard below.
* Non-interactive sessions (piped stdin, ``--print``): refuses with
  kimi's "client does not support interactive questions" message.
* While blocked on the user, the permission-wait gate is held so a
  ``_thread_tool`` timeout can never fire mid-question (human decision
  time is not tool-execution budget — same machinery as
  ``check_permission``), and ``ctx.permission_lock`` serializes this
  prompt against permission prompts so two prompts never fight over the
  screen.

Input is coerced defensively (the ``write_files`` lesson — models
mis-call nested schemas): a bare question string, a single question dict,
or options given as plain strings are all accepted and normalized.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager

from aru.runtime import begin_permission_wait, end_permission_wait, get_ctx

_log = logging.getLogger("aru.tools.ask_user")

OTHER_LABEL = "Other — type a custom answer"

MAX_QUESTIONS = 4

# Kimi-code parity: QUESTION_UNSUPPORTED_FAILURE_MESSAGE.
UNSUPPORTED_MESSAGE = (
    "The connected client does not support interactive questions. Do NOT "
    "call this tool again. Ask the user directly in your text response "
    "instead, and end your turn so the user can reply."
)

DISMISSED_NOTE = "User dismissed the question without answering."


def _dismissed_result(answers: dict[str, str] | None = None) -> str:
    return json.dumps(
        {"answers": answers or {}, "note": DISMISSED_NOTE}, ensure_ascii=False
    )


@contextmanager
def _question_scope(ctx):
    """Hold ``permission_lock`` + mark the permission-wait gate while blocked.

    Mirrors ``aru.permissions._permission_prompt_scope``: the gate tells the
    surrounding ``_thread_tool`` wrapper to suspend any execution timeout for
    as long as we wait on the user, and the lock keeps this question from
    overlapping a permission prompt's modal.
    """
    begin_permission_wait()
    try:
        with ctx.permission_lock:
            yield
    finally:
        end_permission_wait()


def _interactive(ctx, ui) -> bool:
    """True when there is a human who can actually see and answer a prompt."""
    if getattr(ctx, "tui_app", None) is not None:
        return True
    if ui is not None:
        from aru.ui import ReplUI

        # A custom adapter (TuiUI, test fakes) knows how to reach its user.
        # ReplUI prompts on stdin, so it only counts when stdin is a tty.
        if not isinstance(ui, ReplUI):
            return True
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _coerce_questions(questions) -> list[dict] | str:
    """Normalize the model's input into a list of question dicts.

    Accepts the canonical kimi/CC shape plus the common mis-calls: a bare
    question string, a single dict instead of a list, options as plain
    strings, ``multiSelect`` camelCase. Returns an error string (for the
    model) when the input cannot be salvaged.
    """
    if isinstance(questions, str):
        questions = [{"question": questions}]
    elif isinstance(questions, dict):
        questions = [questions]
    if not isinstance(questions, list) or not questions:
        return (
            "Error: 'questions' must be a list of 1-4 question objects: "
            '[{"question": "...?", "header": "Topic", '
            '"options": [{"label": "...", "description": "..."}], '
            '"multi_select": false}]. Options are optional — omit them for '
            "a free-form question."
        )
    if len(questions) > MAX_QUESTIONS:
        return f"Error: at most {MAX_QUESTIONS} questions per call. Ask the most important ones first."

    normalized: list[dict] = []
    for q in questions:
        if isinstance(q, str):
            q = {"question": q}
        if not isinstance(q, dict):
            return "Error: each entry in 'questions' must be an object with a 'question' field."
        text = q.get("question") or q.get("prompt") or ""
        if not isinstance(text, str) or not text.strip():
            return "Error: each question object needs a non-empty 'question' string."
        header = q.get("header") or ""
        multi = bool(q.get("multi_select", q.get("multiSelect", False)))
        raw_options = q.get("options") or []
        labels: list[str] = []
        if isinstance(raw_options, (list, tuple)):
            for opt in raw_options:
                if isinstance(opt, str):
                    label = opt.strip()
                elif isinstance(opt, dict):
                    label = str(opt.get("label") or opt.get("title") or "").strip()
                else:
                    label = ""
                if label:
                    labels.append(label)
        normalized.append(
            {
                "question": text.strip(),
                "header": header if isinstance(header, str) else "",
                "options": labels,
                "multi_select": multi,
            }
        )
    return normalized


def _answer_one(ui, q: dict) -> str | None:
    """Ask a single normalized question. Returns the answer or None if dismissed."""
    question, header, options = q["question"], q["header"], q["options"]
    title = f"{header}: {question}" if header else question

    if q["multi_select"] and options:
        # ctx.ui menus are single-select; degrade to free text with the
        # options spelled out so the user can pick several.
        lines = "\n".join(f"  - {label}" for label in options)
        answer = ui.ask_text(
            f"{title}\nOptions (choose one or more, comma-separated, or answer freely):\n{lines}\n> "
        )
        answer = (answer or "").strip()
        return answer or None

    if options:
        from rich.panel import Panel

        details = Panel(question, title=header or "Question from the agent", border_style="cyan")
        menu = list(options) + [OTHER_LABEL]
        idx = ui.ask_choice(menu, title="Your answer?", cancel_value=None, details=details)
        if idx is None or not 0 <= idx < len(menu):
            return None
        if idx == len(options):
            answer = (ui.ask_text(f"{question} ") or "").strip()
            return answer or None
        return options[idx]

    answer = (ui.ask_text(f"{title} ") or "").strip()
    return answer or None


# NOTE: ``questions`` is annotated as a bare ``list`` on purpose. ``list[dict]``
# makes Agno emit ``items: {"type": "object", "properties": {}, "additionalProperties":
# false}`` — a strict-schema provider would then reject every real question
# object. The docstring documents the shape; ``_coerce_questions`` salvages
# loose inputs.
def AskUserQuestion(questions: list, background: bool = False) -> str:  # noqa: N802 — trained tool name
    """Ask the user questions with structured options and WAIT for the answers.

    This tool blocks until the user responds; the answers are returned as
    the tool result in the form {"answers": {"<question>": "<answer>"}}.
    Use it to: collect preferences or requirements before proceeding,
    resolve ambiguous or underspecified instructions, or let the user
    decide between approaches as you work.

    Do NOT ask questions in plain text while continuing to call tools —
    the user cannot answer running text. Call this tool and act on its
    result. When you can infer the answer from context, be decisive and
    proceed without asking; overusing this tool interrupts the user's flow.

    Usage notes:
    - Users always get an "Other" option for custom input — don't create
      one yourself.
    - Keep option labels concise (1-5 words); 2-4 distinct options per
      question. Omit options entirely for a free-form question.
    - You can ask 1-4 questions per call; group related questions to
      minimize interruptions. Each question may set "header" (short
      category tag) and "multi_select" (allow several answers).

    Args:
        questions: List of 1-4 question objects:
            [{"question": "...?", "header": "Topic",
              "options": [{"label": "...", "description": "..."}],
              "multi_select": false}]
        background: Ignored — questions are always asked in the foreground
            in this client.
    """
    ctx = get_ctx()
    normalized = _coerce_questions(questions)
    if isinstance(normalized, str):
        return normalized

    ui = getattr(ctx, "ui", None)
    if not _interactive(ctx, ui):
        return UNSUPPORTED_MESSAGE
    if ui is None:
        from aru.ui import install_repl_ui_on_ctx

        ui = install_repl_ui_on_ctx(ctx)

    answers: dict[str, str] = {}
    try:
        with _question_scope(ctx):
            for q in normalized:
                answer = _answer_one(ui, q)
                if answer is None:
                    # User dismissed — stop interrogating; report what we got.
                    return _dismissed_result(answers)
                answers[q["question"]] = answer
    except Exception:
        # TuiUI raises on modal timeout / app shutdown; treat any prompt
        # failure as a dismissal so the model gets calm guidance instead
        # of a traceback. Logged at ERROR so the TUI log bridge surfaces
        # the real cause — an instantly-dismissed question otherwise looks
        # like the agent "answering itself".
        _log.error("AskUserQuestion prompt failed; treating as dismissed", exc_info=True)
        return _dismissed_result(answers)

    if not answers:
        return _dismissed_result()
    return json.dumps({"answers": answers}, ensure_ascii=False)
