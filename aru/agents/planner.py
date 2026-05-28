"""Plan reviewer — one-shot scope check, no loop, no tools."""

from agno.agent import Agent

from aru.providers import create_model
from aru.runtime import get_ctx

REVIEWER_INSTRUCTIONS = """\
You are a plan scope reviewer. You receive a user request and a generated implementation plan.
Your ONLY job: ensure the plan does not add more deliverables than the user explicitly asked for.

Rules:
- Count EXACTLY how many deliverables the user asked for. "a function" = 1. "two endpoints" = 2. \
  Unquantified plurals = lean minimal.
- If the user said "a" or "one", the plan MUST have exactly 1 deliverable step. \
  Multiple steps that each produce a separate deliverable is scope creep — keep only the best one.
- Multiple steps are OK only when they implement different parts of the SAME deliverable \
  or when the user explicitly asked for multiple things.

CRITICAL — preserve the original plan text:
- You may ONLY delete entire steps that are scope creep. You must NOT rewrite, rephrase, \
  translate, summarize, or simplify any step you keep.
- Copy kept steps EXACTLY as they appear — same language, same wording, same detail level.
- Return the plan in the EXACT same markdown format (## Summary then ## Steps).
- If the plan is already correct, return it UNCHANGED — do not paraphrase it.

Return ONLY the markdown plan. No explanation, no preamble.\
"""


async def review_plan(request: str, plan: str) -> str:
    """Review a generated plan against the original request, trimming scope creep.

    Uses the small/fast model — no tools, single call, no iteration.
    Returns the corrected plan (or the original if the reviewer fails).
    """
    reviewer = Agent(
        name="Reviewer",
        model=create_model(get_ctx().small_model_ref, max_tokens=2048),
        instructions=REVIEWER_INSTRUCTIONS,
        markdown=True,
    )
    prompt = f"## User Request\n{request}\n\n## Generated Plan\n{plan}"
    try:
        from aru.runner import arun_text_only
        content = await arun_text_only(reviewer, prompt)
        if content and content.strip():
            return content.strip()
    except Exception:
        pass
    return plan
