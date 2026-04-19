# Plano: Paridade multi-agent com Claude Code — 4 estágios completos

**Criado:** 2026-04-19
**Status:** Proposta — implementação completa em uma passada
**Depende de:** #I (recursão destravada — entregue), #H (background tasks — entregue), #G (trace persist — entregue)
**Objetivo:** Subir a capacidade multi-agent do Aru para ~70-80% da sofisticação de Claude Code cobrindo 10 primitivas em 4 estágios. Implementação integral; validação ao final em tarefas reais.

---

## 1. Contexto

Claude Code não tem um "segredo" monolítico de multi-agent — tem 10 primitivas pequenas compostas. Leitura direta de:

- `src/tools/AgentTool/AgentTool.tsx` (tool schema + lifecycle)
- `src/tools/AgentTool/prompt.ts` (getPrompt + matrizes)
- `src/tools/AgentTool/loadAgentsDir.ts` (AgentDefinition schema)
- `src/tools/AgentTool/forkSubagent.ts` (fork mechanics)
- `src/tools/SendMessageTool/SendMessageTool.ts` (continue)
- `src/tasks/LocalAgentTask/LocalAgentTask.tsx` (lifecycle + queue)
- `src/coordinator/coordinatorMode.ts` (coordinator system prompt)
- `src/utils/worktree.ts` (createAgentWorktree)

… identificou 10 primitivas. Aru já tem 3-4 delas parciais. Este plano cobre as demais em estágios dependentes.

### Mapa das primitivas

| # | Primitiva | Estado Aru | Estágio |
|:-:|---|:-:|:-:|
| A | `<task-notification>` estruturada (status/summary/usage) | parcial | 1 |
| B | Queue + drain em tool-round (inject mid-flight) | não | 3 |
| C | AgentDefinition rica (max_turns, critical_reminder, isolation, background, initial_prompt, required_mcps) | parcial | 2 |
| D | Continue vs Spawn discoverable (resume via task_id) | parcial | 1 |
| E | Stop per-task | não (só global) | 3 |
| F | Coordinator system prompt | não | 1 |
| G | Worktree per-agent | não | 4 |
| H | Auto-background + progress tracker | parcial | 4 |
| I | Fork (inherit context) | não | **fora de escopo** |
| J | MCP filter per-agent + deny rules | parcial | 2 |

### Não-objetivos explícitos

1. **Fork subagent (primitiva I)** — CC usa para preservar prompt cache Anthropic; Aru roda via Agno sem o mesmo layer de cache. Ganho não transfere.
2. **Swarm / teams / tmux backends** — custo alto (15-25h adicionais) e baixo sinal de uso single-user.
3. **Coordinator mode global (env var)** — orquestração vira capacidade via `orchestrator` AgentSpec (opt-in). Modo global só se orchestrator pegar tração.
4. **Remote backends (CCR, UDS, bridge)** — escopo TS/ecosystem Anthropic.
5. **Subir nota externa** — objetivo é capacidade real, não métrica.

---

## 2. Estágio 1 — Decisão e notificação (~4-5h)

**Primitivas:** A (enriquecer), D (reforçar resume), F (coordinator prompt)

**Goal:** primary decide single-vs-multi corretamente; orchestrator existe para tarefas grandes; `<task-notification>` carrega status/custo.

### #1.1 — `delegate_prompt.txt` enxuto

**Esforço:** 1h · **Arquivo:** `aru/tools/delegate_prompt.txt` (reescrita completa)

**Conteúdo alvo** (~100 linhas, tool-description only):

```
Launch a new sub-agent to handle complex, multi-step tasks autonomously.

Each sub-agent runs in its own isolated context (fresh task store, read
cache, and agent scope) so noisy exploration stays out of your own
conversation.

{agent_list}

## When to use

- Complex research tasks requiring multiple file reads + searches
- Independent subtasks that can run in parallel (fan-out pattern)
- Tasks requiring a specialised agent (pass `agent_name`)
- Keeping your own context clean — delegate noise-heavy exploration

## When NOT to use

- If you can answer from context or one Read/Grep call, do it directly
- Do not use sub-agents to trivially report file contents or run a single
  command. Give them higher-level tasks.
- If searching for a specific symbol ("class Foo", "def bar"), use
  `grep_search` directly
- If reading 2-3 known files, use `read_files` directly
- One-line factual questions — just read it

## Writing the prompt

Brief the sub-agent like a colleague walking into the room — they haven't
seen this conversation, don't know what you've tried, don't understand
why the task matters.

- Explain what you're trying to accomplish and why
- Describe what you've already learned or ruled out
- Give enough context for judgement calls
- If you need a short response, say so ("report in under 200 words")
- Specify exactly what information to return — it returns ONE message

**Never delegate understanding.** Phrases like "based on your findings,
fix the bug" or "apply the research" push synthesis onto the worker.
Read findings, identify the approach, hand the worker a spec with file
paths and line numbers.

### Bad examples

- "Fix the bug we discussed" — worker can't see your conversation
- "Based on your findings, implement the fix" — lazy delegation
- "Create a PR for the recent changes" — ambiguous scope
- "Something went wrong with the tests, can you look?" — no error, no path

## Parallelism

Launch multiple delegate_task calls in a single message when subtasks are
independent:

- Read-only (research): parallelize freely, cover multiple angles
- Write-heavy (implementation): one worker at a time per set of files
- Verification: can overlap with implementation in different file areas

## Resume vs fresh

Each delegate_task returns a task_id in its header:
    [SubAgent-abc123 | task_id=abc123] <result>

Pass `task_id=abc123` back to continue the same sub-agent (full context
preserved). Fresh delegation (no task_id) starts blank.

Choose resume when: correcting a failure from the same worker; research
overlaps with the next step's files; iterating on the same topic.
Choose fresh when: verifying another worker's code (fresh eyes);
completely unrelated task; previous attempt took wrong approach.

## While agents run

Do NOT fabricate, predict, or summarize results before the task
notification arrives. If the user asks mid-flight, report status ("still
running"), not a guess. The result is delivered as a user-role
<task-notification> message in a later turn — never something you write.

## Expecting output

The sub-agent returns ONE summary message not directly visible to the
user — you must relay the findings back. Sub-agent outputs should
generally be trusted.
```

**Mudanças-chave vs atual:**
- "Bad examples" é nova seção (anti-patterns concretos)
- "While agents run" é nova (don't peek/race)
- "When NOT to use" ganha o gate "answer directly"
- "Resume vs fresh" ganha critério de decisão
- Phases framework **não entra aqui** (vai no orchestrator em #1.3)

**Teste:**
```python
# tests/test_delegate_prompt.py (novo)
def test_delegate_prompt_renders():
    from aru.tools.delegate import _load_delegate_prompt, _render_agent_list
    base = _load_delegate_prompt()
    rendered = base.replace("{agent_list}", _render_agent_list())
    assert "Bad examples" in rendered
    assert "While agents run" in rendered
    assert "answer from context" in rendered
    assert len(rendered) < 10_000  # sanity cap
```

---

### #1.2 — Gate de "answer directly" no primary system prompt

**Esforço:** 0.5h · **Arquivo:** `aru/agents/instructions.py` (ou onde o primary system prompt é montado)

**Localizar** onde o system prompt do role "general" (primary build agent) é construído. Inserir seção nova antes de qualquer menção a delegate_task:

```python
COORDINATION_GATE = """
## Tool-use discipline

Answer questions directly when possible — don't delegate work that you
can handle without tools. Do not use sub-agents to trivially report file
contents or run a single command. Give them higher-level tasks
(research across multiple files, implementation with verification,
review).

Before delegating, ask yourself: "Can I do this in one Read/Grep/Edit?"
If yes, do it. Small tasks get a direct answer; complex multi-file or
multi-phase work gets delegated.

Mental model for complex tasks — four phases:
  1. Research — delegate to sub-agents (parallel when independent)
  2. Synthesis — YOU read findings and craft the implementation spec.
     Never delegate this step. Never say "based on your findings" to
     the next worker.
  3. Implementation — delegate with a precise spec (file:line, action)
  4. Verification — prove the code works, don't just confirm it exists
"""
```

**Integração:** append ao system prompt do role "general" (primary) e "executor" (que também pode delegar). **Não** aplica a subagents.

**Teste:**
```python
def test_primary_system_prompt_has_coordination_gate():
    from aru.agents.instructions import build_instructions
    prompt = build_instructions("general", ...)
    assert "answer questions directly" in prompt.lower()
    assert "four phases" in prompt.lower()

def test_subagent_system_prompt_lacks_coordination_gate():
    # Subagents should NOT see the gate — it's for primaries only
    prompt = build_instructions("explorer", ...)
    assert "four phases" not in prompt.lower()
```

---

### #1.3 — AgentSpec `orchestrator`

**Esforço:** 1h · **Arquivo:** `aru/agents/catalog.py`

**Adicionar:**

```python
ORCHESTRATOR_EXTRA = """
You are an orchestrator sub-agent. Your job: take a complex task,
decompose it into phases, delegate to specialists, synthesize their
results, return a single consolidated report.

## Your tools

- delegate_task — spawn specialists (explorer, verifier, reviewer,
  guide, executor)
- read_file / grep_search / glob_search — use sparingly, only when
  delegation would be overkill (single file, single symbol)
- todo_write — track phases for multi-step tasks

## Phases you orchestrate

| Phase          | Who          | Purpose                          |
|----------------|--------------|----------------------------------|
| Research       | Specialists  | Map codebase, find files         |
| Synthesis      | YOU          | Read findings, craft impl spec   |
| Implementation | Executor     | Make changes per your spec       |
| Verification   | Verifier     | Prove the change works           |

A small task skips Research. A localised fix goes straight to Executor
with a precise spec. A cross-module refactor runs all four phases.

## Continue vs spawn matrix

| Situation                                    | Decision |
|----------------------------------------------|----------|
| Research explored files that need editing    | Continue (task_id=) |
| Research was broad, impl is narrow           | Spawn fresh |
| Correcting same worker's failure             | Continue |
| Verifying another worker's code              | Spawn fresh |
| Previous attempt took wrong approach         | Spawn fresh |
| Unrelated task                               | Spawn fresh |

## Synthesis discipline — your most important job

When workers report findings, YOU read them and craft the next spec.
Never write "based on your findings" or "apply the research" — those
delegate understanding, which defeats the point.

Bad: delegate_task(task="Based on findings, fix the bug", agent_name="executor")
Good: delegate_task(task="Fix the null pointer in src/auth/validate.ts:42.
  The user field on Session is undefined when session expires. Add a
  null check; if null, return 401 'Session expired'.", agent_name="executor")

## Verification ≠ confirmation

Verification means proving the code works:
- Run tests with the new feature enabled, not just "tests pass"
- Run typechecks and investigate errors — don't dismiss as "unrelated"
- Test edge cases and error paths, not just happy path
- Be skeptical — if something looks off, dig in

## Anti-patterns

- Spawning a worker to read one file (do it yourself)
- Spawning 5 workers when 2 would cover the angles
- Passing worker output to next worker without synthesis
- Fabricating results mid-flight (while workers run)

## While workers run

You know nothing about worker findings until they return. Do not
fabricate, predict, or summarize results before <task-notification>
arrives. If asked mid-flight, report status, not a guess.

## Output contract

Return a single report with:
- What you delegated and why (1-2 sentences per worker)
- Consolidated findings / changes made
- Confidence level + what you did NOT verify
"""

AGENTS["orchestrator"] = AgentSpec(
    name="Orchestrator",
    role="orchestrator",
    mode="subagent",
    tools_factory=lambda: _orchestrator_tools(),
    max_tokens=8192,
    small_model=False,
    use_reasoning=True,
    description=(
        "Orchestrator sub-agent for complex multi-phase tasks. "
        "Decomposes the problem, delegates to specialists (explorer/"
        "executor/verifier/reviewer), synthesizes results. Use when a "
        "task spans multiple modules or requires both research and "
        "implementation. For localised tasks, delegate directly to "
        "executor."
    ),
    extra_instructions=ORCHESTRATOR_EXTRA,
)
```

**Adicionar** `role="orchestrator"` ao switch em `aru/agents/instructions.py` mapeando para role base apropriada (provavelmente "executor" ou "general", com ORCHESTRATOR_EXTRA appendado).

**`_orchestrator_tools()`** em `aru/tools/registry.py`:
```python
def _orchestrator_tools() -> list:
    from aru.tools.delegate import delegate_task
    from aru.tools.codebase import read_file, grep_search, glob_search
    from aru.tools.tasklist import todo_write
    return [delegate_task, read_file, grep_search, glob_search, todo_write]
```

Note: `can_delegate` conceptual — destravado por #I via inclusão de `delegate_task` no toolset.

**Testes:**
```python
def test_orchestrator_in_catalog():
    spec = AGENTS["orchestrator"]
    assert spec.mode == "subagent"
    tool_names = {t.__name__ for t in spec.tools_factory()}
    assert "delegate_task" in tool_names
    assert "read_file" in tool_names

def test_orchestrator_has_phases_and_synthesis_in_extra():
    spec = AGENTS["orchestrator"]
    extra = spec.extra_instructions.lower()
    assert "phases" in extra
    assert "synthesis" in extra
    assert "anti-pattern" in extra
```

---

### #1.4 — `<task-notification>` enriquecida

**Esforço:** 1h · **Arquivos:** `aru/tools/delegate.py`, `aru/session.py`

**Estado atual** (`delegate.py:118-123`):
```python
parts.append(
    f"<task-notification task_id=\"{n.get('task_id', '?')}\">\n"
    f"{n.get('result', '(no result)')}\n"
    f"</task-notification>"
)
```

**Novo formato** (alinhado com `coordinatorMode.ts:149-160`):
```python
def _format_notification(n: dict) -> str:
    """Format a completed background task as <task-notification>.

    Mirrors claude-code's XML structure (coordinatorMode.ts:149-160) so
    the orchestrator has status/usage signals, not just raw text. The
    <usage> block enables cost-aware decisions (e.g. "worker used 80k
    tokens — maybe don't dispatch another on the same topic").
    """
    task_id = n.get("task_id", "?")
    status = n.get("status", "completed")
    summary = n.get("summary", "")
    result = n.get("result", "(no result)")
    usage = n.get("usage", {})
    lines = [f'<task-notification>']
    lines.append(f'<task-id>{task_id}</task-id>')
    lines.append(f'<status>{status}</status>')
    if summary:
        lines.append(f'<summary>{summary}</summary>')
    lines.append(f'<result>{result}</result>')
    if usage:
        parts = []
        if "total_tokens" in usage:
            parts.append(f'  <total_tokens>{usage["total_tokens"]}</total_tokens>')
        if "tool_uses" in usage:
            parts.append(f'  <tool_uses>{usage["tool_uses"]}</tool_uses>')
        if "duration_ms" in usage:
            parts.append(f'  <duration_ms>{usage["duration_ms"]}</duration_ms>')
        if parts:
            lines.append('<usage>')
            lines.extend(parts)
            lines.append('</usage>')
    lines.append('</task-notification>')
    return "\n".join(lines)
```

**Popular usage** no `_bg_wrapper` (`delegate.py:488-499`):
```python
async def _bg_wrapper() -> None:
    import time as _time
    started = _time.monotonic()
    result = None
    status = "completed"
    summary = f'Agent "{agent_name or "generic"}" completed'
    try:
        result = await _run()
    except Exception as exc:
        result = f"[bg-{bg_id}] Error: {exc}"
        status = "failed"
        summary = f'Agent "{agent_name or "generic"}" failed'
    duration_ms = int((_time.monotonic() - started) * 1000)
    # Pull usage from the last trace (set by _execute_with_streaming)
    usage = {}
    if parent_session is not None:
        recent_trace = next(
            (t for t in reversed(parent_session.subagent_traces)
             if t.task_id == str(agent_id)),
            None,
        )
        if recent_trace:
            usage = {
                "total_tokens": recent_trace.tokens_in + recent_trace.tokens_out,
                "tool_uses": len(recent_trace.tool_calls),
                "duration_ms": duration_ms,
            }
    if parent_session is not None and hasattr(parent_session, "pending_notifications"):
        parent_session.pending_notifications.append({
            "task_id": bg_id,
            "status": status,
            "summary": summary,
            "result": result or f"[bg-{bg_id}] No output",
            "usage": usage,
            "at": asyncio.get_event_loop().time(),
        })
```

**Testes:**
```python
def test_notification_includes_usage():
    from aru.tools.delegate import _format_notification
    out = _format_notification({
        "task_id": "bg-abc",
        "status": "completed",
        "result": "done",
        "usage": {"total_tokens": 1500, "tool_uses": 3, "duration_ms": 2300},
    })
    assert "<total_tokens>1500</total_tokens>" in out
    assert "<status>completed</status>" in out

def test_notification_includes_failed_status():
    out = _format_notification({"task_id": "x", "status": "failed", "result": "boom"})
    assert "<status>failed</status>" in out
```

---

### #1.5 — Smoke tests E2E (pequena/média/grande)

**Esforço:** 1h · **Arquivo:** `tests/test_orchestrator_e2e.py` (novo)

Testes usando `_FakeAgent` que já existe em `tests/test_delegate.py`:

```python
@pytest.mark.asyncio
async def test_small_task_no_delegation(fake_primary, capture_calls):
    """Primary resolves single-line factual question without delegate_task."""
    result = await run_primary(fake_primary, "what's in README.md?")
    assert capture_calls.delegate_task_count == 0

@pytest.mark.asyncio
async def test_medium_task_one_executor(fake_primary, capture_calls):
    """Primary delegates once to executor with file:line in prompt."""
    result = await run_primary(
        fake_primary,
        "fix the null pointer in src/auth/validate.ts:42",
    )
    assert capture_calls.delegate_task_count == 1
    args = capture_calls.delegate_task_args[0]
    assert "validate.ts:42" in args["task"]

@pytest.mark.asyncio
async def test_large_task_uses_orchestrator(fake_primary, capture_calls):
    """Primary delegates to orchestrator for cross-module work."""
    result = await run_primary(
        fake_primary,
        "refactor the auth layer across all modules to use JWT",
    )
    delegations = capture_calls.delegate_task_args
    assert any(a.get("agent_name") == "orchestrator" for a in delegations)
```

**Arquivos estágio 1:**
- `aru/tools/delegate_prompt.txt` (reescrita)
- `aru/tools/delegate.py` (notification format + usage enrichment)
- `aru/agents/instructions.py` (COORDINATION_GATE)
- `aru/agents/catalog.py` (orchestrator AgentSpec)
- `aru/tools/registry.py` (_orchestrator_tools)
- `aru/session.py` (pending_notifications dict schema — already exists, just new fields)
- `tests/test_delegate_prompt.py` (novo)
- `tests/test_orchestrator_e2e.py` (novo)
- estender `tests/test_catalog.py`

---

## 3. Estágio 2 — AgentSpec rica (~5-7h)

**Primitivas:** C (max_turns, critical_reminder, isolation, background, initial_prompt), J (MCP filter per-agent)

**Goal:** AgentSpec deixa de ser minimalista. Cada campo novo tem uso claro.

### #2.1 — AgentSpec expandido

**Esforço:** 2h · **Arquivo:** `aru/agents/catalog.py`

**Novos campos** (com defaults que preservam comportamento atual):

```python
@dataclass(frozen=True)
class AgentSpec:
    # ... existing fields ...

    # Cap on agentic iterations before forced stop. Safety net for
    # agents that drift (e.g. verifier re-checking endlessly). None
    # means "no cap beyond what Agno enforces internally".
    max_turns: int | None = None

    # Permission mode applied inside the sub-agent scope:
    # "default" (prompt), "acceptEdits" (auto-allow edits), "plan"
    # (read-only). Inherits parent when None. Matches
    # claude-code/loadAgentsDir.ts:86 permissionMode field.
    permission_mode: str | None = None

    # Run in a git worktree when True. No-op (fail-soft) when cwd is
    # not a git repo. Implemented in Estágio 4 (#4.1).
    isolation: str | None = None  # None | "worktree"

    # Always dispatch as background task. Parent receives <task-
    # notification> when done instead of blocking. Overrides caller's
    # run_in_background=False for agents marked background=True in
    # the spec.
    background: bool = False

    # Prepended to the first user turn. Useful for specialists that
    # need a standard setup ("first, read PROJECT.md"). Mirrors
    # claude-code/loadAgentsDir.ts:124 initialPrompt.
    initial_prompt: str = ""

    # Short message re-injected as a system reminder at EVERY turn.
    # Anti-drift mechanism. Example for verifier: "Remember: you are
    # read-only. Never propose edits." Mirrors claude-code/loadAgentsDir.
    # ts:121 criticalSystemReminder_EXPERIMENTAL.
    critical_reminder: str = ""

    # MCP server patterns required for this agent to be available. If
    # the patterns aren't all satisfied, the agent is filtered from
    # the delegate_task agent list. Mirrors claude-code/loadAgentsDir.
    # ts:126 requiredMcpServers.
    required_mcps: tuple[str, ...] = ()

    # Skip CLAUDE.md hierarchy in the agent's userContext. For
    # read-only specialists (explorer, verifier) that don't need
    # commit/PR guidelines. Mirrors claude-code/loadAgentsDir.ts:131
    # omitClaudeMd.
    omit_claude_md: bool = False
```

**Anotar specs existentes** com os defaults apropriados (exemplos):

```python
"explorer": AgentSpec(
    ...,
    max_turns=20,           # exploration shouldn't need more
    omit_claude_md=True,    # read-only, no commit rules
    critical_reminder="Remember: you are read-only. Do not propose edits.",
),
"verification": AgentSpec(
    ...,
    max_turns=15,
    omit_claude_md=True,
    critical_reminder="Remember: you verify but never edit.",
),
"executor": AgentSpec(
    ...,
    permission_mode="acceptEdits",  # primary sub-task — auto-edits OK
    max_turns=40,
),
"orchestrator": AgentSpec(
    ...,
    max_turns=30,
    critical_reminder=(
        "Remember: synthesis is YOUR job. Never pass worker output "
        "to the next worker without synthesizing first."
    ),
),
```

**Teste:**
```python
def test_agentspec_new_fields_default():
    from aru.agents.catalog import AgentSpec
    spec = AgentSpec(
        name="x", role="r", mode="subagent",
        tools_factory=lambda: [], max_tokens=None,
    )
    assert spec.max_turns is None
    assert spec.permission_mode is None
    assert spec.isolation is None
    assert spec.background is False
    assert spec.required_mcps == ()
```

---

### #2.2 — YAML frontmatter ganha os mesmos campos

**Esforço:** 1h · **Arquivos:** `aru/config.py` (parser de `.agents/agents/*.md`)

Localizar `CustomAgentDef` e o parser que lê frontmatter. Adicionar campos:

```python
@dataclass
class CustomAgentDef:
    name: str
    description: str
    mode: str
    model: str | None = None
    tools: list[str] | dict[str, bool] | None = None
    permission: list | None = None
    system_prompt: str = ""
    # NEW fields — mirror AgentSpec
    max_turns: int | None = None
    permission_mode: str | None = None
    isolation: str | None = None
    background: bool = False
    initial_prompt: str = ""
    critical_reminder: str = ""
    required_mcps: tuple[str, ...] = ()
    omit_claude_md: bool = False
```

Parse from frontmatter:
```python
def _parse_agent_md(path: str) -> CustomAgentDef:
    # ... existing parse logic ...
    front = _parse_frontmatter(text)
    return CustomAgentDef(
        name=front["name"],
        ...,
        max_turns=int(front["max_turns"]) if "max_turns" in front else None,
        permission_mode=front.get("permission_mode"),
        isolation=front.get("isolation"),
        background=bool(front.get("background", False)),
        initial_prompt=front.get("initial_prompt", ""),
        critical_reminder=front.get("critical_reminder", ""),
        required_mcps=tuple(front.get("required_mcps", [])),
        omit_claude_md=bool(front.get("omit_claude_md", False)),
    )
```

**Teste:**
```python
def test_yaml_frontmatter_with_new_fields(tmp_path):
    agent_md = tmp_path / "my-agent.md"
    agent_md.write_text("""---
name: my-agent
mode: subagent
description: test
max_turns: 10
critical_reminder: stay focused
isolation: worktree
---
You are a test agent.
""")
    from aru.config import _parse_agent_md
    defn = _parse_agent_md(str(agent_md))
    assert defn.max_turns == 10
    assert defn.critical_reminder == "stay focused"
    assert defn.isolation == "worktree"
```

---

### #2.3 — `max_turns` enforcement

**Esforço:** 1.5h · **Arquivo:** `aru/tools/delegate.py`

**Investigação necessária:** Agno `Agent.arun()` aceita `max_iterations` ou similar? Checar `agno.agent.Agent` docs.

**Hipótese 1 (provável):** Agno tem `max_iterations` passable por config. Basta passar:
```python
sub = Agent(
    ...,
    max_iterations=builtin_spec.max_turns if builtin_spec else None,
)
```

**Hipótese 2 (fallback):** Agno não expõe o cap. Wrap o `_execute_with_streaming`:
```python
async def _execute_with_streaming(agent_instance, max_turns=None):
    turn_count = 0
    # ... existing streaming loop ...
    async for event in agent_instance.arun(...):
        if isinstance(event, ToolCallStartedEvent):
            turn_count += 1
            if max_turns is not None and turn_count > max_turns:
                _trace.status = "cancelled"
                return (
                    f"[{label}] Hit max_turns={max_turns} cap. Returning "
                    f"partial result. Consider breaking task into smaller steps."
                )
```

**Implementação:** tentar Hipótese 1 primeiro (~15min). Se não funcionar, Hipótese 2. Budget total 1.5h inclui investigação.

**Teste:**
```python
@pytest.mark.asyncio
async def test_max_turns_caps_execution():
    # Spec with max_turns=2, fake agent that wants to run 10 iterations
    spec = AgentSpec(..., max_turns=2)
    result = await delegate_task("run forever", agent_name="looper")
    assert "max_turns" in result
    assert "Returning partial" in result
```

---

### #2.4 — `critical_reminder` re-injetado a cada turno

**Esforço:** 1h · **Arquivo:** `aru/tools/delegate.py`

**Investigação:** Agno permite modificar `agent.instructions` dinamicamente entre turnos? Provavelmente sim — `instructions` é um field mutável.

**Mecanismo simples:** anexar o reminder ao final das instruções na criação:
```python
instructions = build_instructions(spec.role) + spec.extra_instructions
if spec.critical_reminder:
    instructions += f"\n\n⚠️ CRITICAL REMINDER (re-read each turn): {spec.critical_reminder}"
```

**Mecanismo robusto (se Agno drift cap for um problema real):** hook entre turnos no streaming loop que re-injeta como user-role system-reminder:
```python
async for event in agent_instance.arun(...):
    if isinstance(event, TurnCompleteEvent):
        if spec.critical_reminder:
            # Re-inject as system reminder before next turn
            agent_instance.add_message({
                "role": "user",
                "content": [{"type": "text",
                            "text": f"<system-reminder>\n{spec.critical_reminder}\n</system-reminder>"}]
            })
```

**Pragmática:** começar com o mecanismo simples. Se drift continua sendo um problema após #1.5 validação, subir pro robusto.

**Teste:**
```python
def test_critical_reminder_appears_in_instructions():
    spec = AgentSpec(..., critical_reminder="do not edit")
    instr = build_instructions(spec.role) + spec.extra_instructions
    if spec.critical_reminder:
        instr += f"\n\n⚠️ CRITICAL REMINDER (re-read each turn): {spec.critical_reminder}"
    assert "do not edit" in instr
    assert "CRITICAL REMINDER" in instr
```

---

### #2.5 — MCP filter per-agent

**Esforço:** 0.5h · **Arquivo:** `aru/tools/delegate.py`

Na renderização da lista de agents (`_render_agent_list`), filtrar agents cujos `required_mcps` não estão conectados:

```python
def _render_agent_list() -> str:
    from aru.mcp import get_connected_servers  # existing MCP layer
    connected = set(get_connected_servers())

    lines: list[str] = ["## Available sub-agents", ""]
    for key, spec in AGENTS.items():
        if spec.mode != "subagent":
            continue
        # Filter by MCP requirements
        if spec.required_mcps:
            if not all(req in connected for req in spec.required_mcps):
                continue  # MCP not available → hide this agent
        lines.append(f'- `agent_name="{key}"`: {spec.description}')
    # ... same for custom agents ...
    return "\n".join(lines)
```

**Teste:**
```python
def test_mcp_filter_hides_agent_when_required_mcp_missing(monkeypatch):
    from aru.agents.catalog import AGENTS
    from aru.tools.delegate import _render_agent_list
    AGENTS["_mcp_test"] = AgentSpec(
        ..., required_mcps=("slack",),
    )
    monkeypatch.setattr("aru.mcp.get_connected_servers", lambda: [])
    rendered = _render_agent_list()
    assert "_mcp_test" not in rendered
    monkeypatch.setattr("aru.mcp.get_connected_servers", lambda: ["slack"])
    rendered = _render_agent_list()
    assert "_mcp_test" in rendered
```

---

### #2.6 — Testes e ajustes finais

**Esforço:** 1h · **Arquivo:** `tests/test_catalog.py` (estender)

Validar que todos os built-in agents têm os campos novos populados coerentemente:
```python
def test_read_only_agents_have_critical_reminder():
    read_only = ["explorer", "verification", "reviewer", "guide"]
    for name in read_only:
        spec = AGENTS[name]
        assert spec.critical_reminder, f"{name} should have critical_reminder"

def test_all_agents_have_reasonable_max_turns():
    for name, spec in AGENTS.items():
        if spec.mode == "subagent":
            assert spec.max_turns is None or 5 <= spec.max_turns <= 50
```

**Arquivos estágio 2:**
- `aru/agents/catalog.py` (expansão + anotação dos specs)
- `aru/config.py` (parser YAML)
- `aru/tools/delegate.py` (wire max_turns, critical_reminder, MCP filter)
- `aru/agent_factory.py` (consome os novos campos em `create_agent_from_spec`)
- estender `tests/test_catalog.py`
- estender `tests/test_config.py`

---

## 4. Estágio 3 — Controle de tasks em voo (~4-6h)

**Primitivas:** B (queue + drain), E (stop per-task)

**Goal:** orquestrador consegue parar um worker específico e enviar mensagem a worker em execução.

### #3.1 — `stop_task` tool

**Esforço:** 2h · **Arquivos:** `aru/tools/stop_task.py` (novo), `aru/runtime.py`, `aru/tools/registry.py`

**Mudança no runtime** — cada task tem seu próprio abort_event (filho do global):

```python
# aru/runtime.py — estender RuntimeContext
@dataclass
class RuntimeContext:
    # ... existing ...
    # Per-task abort events, keyed by task_id. When stop_task(task_id) is
    # called, the specific event is set. The task's _execute_with_streaming
    # observes its own event OR the parent's global abort_event (OR'd).
    per_task_aborts: dict[str, threading.Event] = field(default_factory=dict)
```

**Em `fork_ctx`** — compartilhar o dict por referência:
```python
def fork_ctx() -> RuntimeContext:
    # ... existing ...
    # per_task_aborts is shared by reference so stop_task(id) from the
    # primary reaches the fork running that id
    forked.per_task_aborts = original.per_task_aborts  # same dict ref
    return forked
```

**No delegate.py** — registrar event antes da execução:
```python
# In _run()
_my_abort = threading.Event()
get_ctx().per_task_aborts[task_id_for_output] = _my_abort

# In _execute_with_streaming — observe BOTH events
async for event in agent_instance.arun(...):
    if is_aborted() or _my_abort.is_set():
        _trace.status = "cancelled"
        return f"[{label}] Cancelled."
```

**Tool:**
```python
# aru/tools/stop_task.py
async def stop_task(task_id: str) -> str:
    """Stop a running sub-agent task.

    Signals the specified task to cancel gracefully. The task will return
    with a "[task cancelled]" message on its next observation point
    (between tool calls or between streaming events). Use when:
    - You launched a worker in the wrong direction
    - User changed requirements mid-flight
    - A worker is clearly stuck (check via trace first)

    The task_id comes from the header of a prior delegate_task result:
        [SubAgent-abc | task_id=sa-7] ...
    Pass "sa-7" here.
    """
    from aru.runtime import get_ctx
    aborts = get_ctx().per_task_aborts
    if task_id not in aborts:
        return f"[stop_task] No running task with id '{task_id}'."
    aborts[task_id].set()
    return f"[stop_task] Signaled task '{task_id}' to cancel. It will return shortly."
```

**Teste:**
```python
@pytest.mark.asyncio
async def test_stop_task_aborts_specific_running_task():
    # Launch bg task; call stop_task with its id; await notification
    bg_result = await delegate_task("slow task", agent_name="explorer", run_in_background=True)
    task_id = _extract_task_id(bg_result)
    await asyncio.sleep(0.05)  # let it start
    result = await stop_task(task_id)
    assert "Signaled" in result
    # Verify the trace shows cancelled status after a short wait
    ...
```

---

### #3.2 — `send_message_to_task` tool

**Esforço:** 2h · **Arquivos:** `aru/tools/send_message.py` (novo)

**Queue per-task:**
```python
# aru/runtime.py — extend RuntimeContext
# Per-task inbound message queues. Drained at tool-round boundaries by
# the sub-agent's execution loop. Mirrors claude-code's
# queuePendingMessage/drainPendingMessages pattern.
per_task_inbox: dict[str, list[str]] = field(default_factory=dict)
```

**Tool:**
```python
# aru/tools/send_message.py
async def send_message_to_task(to: str, message: str) -> str:
    """Queue a message for a running sub-agent.

    The message is appended to the target's inbox and delivered to its
    next turn (as a user-role message). Delivery is async — the target
    receives the message when it next checks its inbox (between tool
    rounds). No synchronous reply channel.

    Use to:
    - Redirect a worker mid-flight ("stop JWT, do sessions instead")
    - Provide a correction or additional constraint
    - Answer a question the worker left pending (rare)

    Args:
        to: task_id of target (from the header of a prior delegate_task
          result). Use "*" to broadcast to all live tasks.
        message: Plain text to deliver.
    """
    from aru.runtime import get_ctx
    inbox = get_ctx().per_task_inbox
    aborts = get_ctx().per_task_aborts  # live tasks have abort events registered
    if to == "*":
        live_ids = list(aborts.keys())
        for tid in live_ids:
            inbox.setdefault(tid, []).append(message)
        return f"[send_message] Delivered to {len(live_ids)} live tasks."
    if to not in aborts:
        return f"[send_message] No live task with id '{to}'."
    inbox.setdefault(to, []).append(message)
    return f"[send_message] Queued for '{to}' — delivered on its next turn."
```

---

### #3.3 — Drain em tool-round boundary (Agno hook)

**Esforço:** 1-2h · **Arquivo:** `aru/tools/delegate.py` (_execute_with_streaming)

**Investigação crítica:** Agno expõe hook entre tool calls? Checkar no código de Agno:
- `agno.agent.Agent.arun(stream=True, stream_events=True)` — já emitimos `ToolCallStartedEvent` e `ToolCallCompletedEvent`. Podemos injetar mensagens **entre** eles.

**Implementação:**
```python
async def _execute_with_streaming(agent_instance, my_task_id: str):
    # ... existing setup ...
    async for event in agent_instance.arun(task, stream=True, stream_events=True, yield_run_output=True):
        # Drain inbox at tool-round boundaries — between tool completion
        # and the next tool start, inject any queued messages as user-
        # role content.
        if isinstance(event, ToolCallCompletedEvent):
            # ... existing recording ...
            inbox = get_ctx().per_task_inbox
            if my_task_id in inbox and inbox[my_task_id]:
                msgs = inbox[my_task_id]
                inbox[my_task_id] = []
                # Agno supports adding a user message to the current run
                # via agent.add_user_message() or similar. If not, we
                # accumulate and prepend to the next delegate call.
                try:
                    for msg in msgs:
                        agent_instance.add_user_message(
                            f"<system-reminder>Orchestrator sent: {msg}</system-reminder>"
                        )
                except AttributeError:
                    # Agno doesn't support mid-run injection; fall back
                    # to logging — the message will be lost. Document
                    # the limitation.
                    for msg in msgs:
                        logger.warning(f"send_message drop (no Agno hook): {msg}")
```

**Se Agno não suporta `add_user_message` mid-run:**
- Fallback A: deliver apenas na próxima delegação (se o mesmo task_id resume) — message entra na `pending_messages` lida por resume logic
- Fallback B: documentar como limitação; `send_message_to_task` só deliver para background tasks via notification (primary turn boundary)

**Orçamento de 1h para investigação.** Se após 1h ficar claro que Agno não oferece o hook:
1. Registrar decisão na footer do plano
2. Deliver `send_message_to_task` apenas para tasks em background (entrega no drain do primary)
3. `stop_task` continua funcional independente

---

### #3.4 — Tests

**Esforço:** 1h · **Arquivo:** `tests/test_task_control.py` (novo)

```python
@pytest.mark.asyncio
async def test_stop_task_signals_target():
    # ... setup bg task ...
    await stop_task(task_id)
    # Assert abort_event set
    assert get_ctx().per_task_aborts[task_id].is_set()

@pytest.mark.asyncio
async def test_send_message_queues_to_target_inbox():
    await send_message_to_task("sa-5", "please focus on src/auth only")
    assert "sa-5" in get_ctx().per_task_inbox
    assert "focus on src/auth" in get_ctx().per_task_inbox["sa-5"][0]

@pytest.mark.asyncio
async def test_send_message_broadcast_to_all_live():
    # Register two fake live tasks
    get_ctx().per_task_aborts["t1"] = threading.Event()
    get_ctx().per_task_aborts["t2"] = threading.Event()
    result = await send_message_to_task("*", "stop all")
    assert "2 live" in result
    assert get_ctx().per_task_inbox["t1"] == ["stop all"]
    assert get_ctx().per_task_inbox["t2"] == ["stop all"]

@pytest.mark.asyncio
async def test_send_message_no_live_task():
    result = await send_message_to_task("nonexistent", "x")
    assert "No live task" in result
```

**Arquivos estágio 3:**
- `aru/runtime.py` (per_task_aborts, per_task_inbox)
- `aru/tools/stop_task.py` (novo)
- `aru/tools/send_message.py` (novo)
- `aru/tools/registry.py` (registrar novas tools)
- `aru/tools/delegate.py` (registrar event + drain inbox)
- `tests/test_task_control.py` (novo)

**Exposição:** `stop_task` e `send_message_to_task` devem estar no toolset do primary e do orchestrator, NÃO no default subagent toolset (evitar mal-uso por agents inferiores).

---

## 5. Estágio 4 — Isolamento + observabilidade (~10-14h)

**Primitivas:** G (worktree), H (auto-background + progress)

**Goal:** paralelismo real de edição + observabilidade mid-flight.

### #4.1 — Worktree per-agent + cwd-aware tools

**Esforço:** 6-8h (inclui refactor cwd-aware) · **Arquivos:** `aru/worktree.py` (novo), todos os file tools

**Módulo `aru/worktree.py`:**
```python
"""Git worktree utilities for sub-agent isolation.

Each sub-agent can optionally run in its own worktree. Edits are
contained until the user decides to merge. Mirrors claude-code/utils/
worktree.ts:902 createAgentWorktree. Fail-soft — returns None when
cwd is not a git repo; the sub-agent runs in-place.

Storage layout:
    <git_root>/.aru/worktrees/<slug>/    ← worktree dir
    branch: aru-subagent/<slug>          ← isolated branch
"""

import os
import re
import subprocess
from pathlib import Path


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise ValueError(f"Invalid worktree slug: {slug!r}")
    return slug


def has_git_root(cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=cwd, timeout=2,
        )
        return out.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def create_agent_worktree(slug: str, base_cwd: str | None = None) -> dict | None:
    slug = _validate_slug(slug)
    cwd = base_cwd or os.getcwd()
    gitroot = has_git_root(cwd)
    if not gitroot:
        return None
    worktree_path = str(Path(gitroot) / ".aru" / "worktrees" / slug)
    branch = f"aru-subagent/{slug}"
    try:
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, worktree_path],
            check=True, cwd=gitroot, timeout=10,
        )
    except subprocess.CalledProcessError:
        try:
            subprocess.run(
                ["git", "worktree", "add", worktree_path, branch],
                check=True, cwd=gitroot, timeout=10,
            )
        except subprocess.CalledProcessError:
            return None
    return {"path": worktree_path, "branch": branch, "gitroot": gitroot}


def remove_agent_worktree(slug: str, delete_branch: bool = True) -> None:
    slug = _validate_slug(slug)
    gitroot = has_git_root(os.getcwd())
    if not gitroot:
        return
    worktree_path = str(Path(gitroot) / ".aru" / "worktrees" / slug)
    subprocess.run(
        ["git", "worktree", "remove", "--force", worktree_path],
        capture_output=True, cwd=gitroot, timeout=10,
    )
    if delete_branch:
        subprocess.run(
            ["git", "branch", "-D", f"aru-subagent/{slug}"],
            capture_output=True, cwd=gitroot, timeout=5,
        )


def gc_worktrees(days: int = 30) -> int:
    # ... prune by mtime ...
    pass
```

**Cwd contextvar** em `aru/runtime.py`:
```python
# New field on RuntimeContext
cwd_override: str | None = None  # when set, tools use this instead of os.getcwd()
```

**Helper em `aru/runtime.py`:**
```python
def current_cwd() -> str:
    """Return the effective cwd for tool operations.

    When a sub-agent runs in a worktree, ctx.cwd_override points to the
    worktree path; tools use this instead of os.getcwd() to avoid
    process-level chdir which would break asyncio concurrency.
    """
    try:
        override = get_ctx().cwd_override
    except LookupError:
        return os.getcwd()
    return override or os.getcwd()
```

**Refactor de tools** — todas as tools que usam `os.getcwd()` ou caminhos relativos passam a usar `current_cwd()`:

Audit-list (baseado em grep rápido do projeto):
- `aru/tools/codebase.py` (read_file, write_file, edit_file, grep_search, glob_search)
- `aru/tools/shell.py` (bash_tool, powershell_tool — passar `cwd=current_cwd()` ao subprocess)
- `aru/tools/checkpoints.py` (file history paths)
- `aru/tools/git.py` (se existir)
- `aru/tools/notebook.py` (se existir)

Padrão de refactor:
```python
# Antes
def read_file(path: str):
    abs_path = path if os.path.isabs(path) else os.path.abspath(path)
    with open(abs_path) as f:
        ...

# Depois
def read_file(path: str):
    from aru.runtime import current_cwd
    abs_path = path if os.path.isabs(path) else os.path.join(current_cwd(), path)
    abs_path = os.path.abspath(abs_path)
    with open(abs_path) as f:
        ...
```

**Integração em `delegate.py`:**
```python
worktree_info = None
if builtin_spec and builtin_spec.isolation == "worktree":
    from aru.worktree import create_agent_worktree
    worktree_info = create_agent_worktree(f"sa-{agent_id}")
    if worktree_info:
        get_ctx().cwd_override = worktree_info["path"]

try:
    # ... existing execution ...
finally:
    if worktree_info:
        # Annotate the trace with branch info; don't auto-remove the worktree
        # (user may want to inspect/merge). /worktree gc prunes later.
        _trace.result = f"[worktree: {worktree_info['branch']}]\n{_trace.result}"
```

**Slash commands** (`aru/commands.py`):
- `/worktree list` — lista worktrees existentes
- `/worktree gc` — chama `gc_worktrees()`
- `/worktree merge <slug>` — apresenta diff, pergunta se merge

**Testes** (`tests/test_worktree.py`):
```python
def test_create_worktree_succeeds(tmp_git_repo):
    info = create_agent_worktree("t1", base_cwd=tmp_git_repo)
    assert info is not None
    assert os.path.isdir(info["path"])

def test_create_fails_soft_outside_git(tmp_path):
    assert create_agent_worktree("x", base_cwd=str(tmp_path)) is None

def test_slug_validation_rejects_traversal():
    with pytest.raises(ValueError):
        _validate_slug("../etc")

def test_tools_honor_cwd_override(tmp_git_repo):
    # Create worktree, set cwd_override, call read_file with relative path,
    # verify it reads from worktree not primary cwd
    ...

@pytest.mark.asyncio
async def test_parallel_subagents_with_worktrees_dont_conflict(tmp_git_repo):
    # Two subagents with isolation="worktree" edit foo.py differently;
    # primary foo.py unchanged after both complete
    ...
```

---

### #4.2 — ProgressTracker per-task

**Esforço:** 2h · **Arquivo:** `aru/session.py` (estender SubagentTrace), `aru/tools/delegate.py`

**Estender SubagentTrace:**
```python
@dataclass
class SubagentTrace:
    # ... existing ...
    # Progress updated mid-run — used by /subagents --live and by
    # <task-notification> usage field.
    latest_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    tool_use_count: int = 0
    last_activity: str = ""   # "Reading src/foo.py"
    last_activity_at: float = 0.0
```

**Update no loop streaming:**
```python
elif isinstance(event, ToolCallStartedEvent):
    # ... existing ...
    _trace.tool_use_count += 1
    t_name = event.tool.tool_name if hasattr(event, "tool") else "?"
    t_args = getattr(event.tool, "tool_args", None) if hasattr(event, "tool") else None
    _trace.last_activity = _describe_activity(t_name, t_args)
    _trace.last_activity_at = _time.monotonic()

# Somewhere in usage plumbing
if run_output and run_output.metrics:
    _trace.latest_input_tokens = ...
    _trace.cumulative_output_tokens = ...
```

**Helper `_describe_activity`:**
```python
def _describe_activity(tool_name: str, args: dict | None) -> str:
    if not args:
        return tool_name
    if tool_name == "read_file":
        return f"Reading {args.get('path', '?')}"
    if tool_name == "grep_search":
        return f"Grep '{args.get('pattern', '?')}'"
    if tool_name == "write_file":
        return f"Writing {args.get('path', '?')}"
    if tool_name == "bash_tool":
        cmd = args.get('command', '')[:40]
        return f"Bash: {cmd}"
    return tool_name
```

---

### #4.3 — `/subagents --live` view

**Esforço:** 1h · **Arquivo:** `aru/commands.py`

Adicionar flag `--live` ao `/subagents`:
```python
def cmd_subagents(args: str):
    live = "--live" in args
    session = get_ctx().session
    if not session:
        return "No active session."
    rows = [t for t in session.subagent_traces]
    if live:
        # Filter to running + recently completed
        rows = [t for t in rows if t.status == "running" or (time.monotonic() - t.ended_at) < 60]
    table = Table(title="Sub-agents")
    table.add_column("task_id")
    table.add_column("agent")
    table.add_column("status")
    table.add_column("tokens in/out")
    table.add_column("tools")
    table.add_column("last activity")
    for t in rows:
        table.add_row(
            t.task_id,
            t.agent_name,
            t.status,
            f"{t.latest_input_tokens}/{t.cumulative_output_tokens}",
            str(t.tool_use_count),
            t.last_activity or "-",
        )
    ctx.console.print(table)
```

---

### #4.4 — Auto-background após 120s

**Esforço:** 1h · **Arquivo:** `aru/tools/delegate.py`

Timer no `_run`:
```python
AUTO_BACKGROUND_MS = 120_000  # 2 minutes

async def delegate_task(...):
    # ... existing ...
    if run_in_background:
        # ... existing bg path ...
        return ...
    # Non-background path: run with a timeout. If exceeded, migrate to bg.
    task = asyncio.create_task(_run())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=AUTO_BACKGROUND_MS / 1000)
    except asyncio.TimeoutError:
        # Migrate: task is still running; convert to background
        bg_id = f"bg-{uuid.uuid4().hex[:8]}"
        async def _migration_wrapper():
            try:
                result = await task
            except Exception as exc:
                result = f"[bg-{bg_id}] Errored: {exc}"
            parent_session.pending_notifications.append({
                "task_id": bg_id,
                "status": "completed",
                "result": result,
                ...
            })
        asyncio.create_task(_migration_wrapper())
        return (
            f"[AutoBackground | task_id={bg_id}] Sub-agent exceeded 120s "
            f"threshold — migrated to background. You'll receive a "
            f"<task-notification> when it completes. Continue other work."
        )
```

**Toggle** — permitir opt-out via env var ou session flag para testes / casos específicos.

---

### #4.5 — Tests estágio 4

**Esforço:** 2h · **Arquivos:** `tests/test_worktree.py`, `tests/test_progress.py`

Além dos testes de #4.1, adicionar:
```python
def test_progress_tracker_updates_on_tool_events():
    # Run fake subagent with 3 tool calls; assert trace.tool_use_count == 3
    ...

def test_auto_background_migrates_after_timeout(monkeypatch):
    monkeypatch.setattr("aru.tools.delegate.AUTO_BACKGROUND_MS", 100)
    # Launch fake slow task; assert it returns with AutoBackground header
    ...

def test_subagents_live_filters_running():
    # Create 2 traces (1 running, 1 old-completed); /subagents --live shows only 1
    ...
```

**Arquivos estágio 4:**
- `aru/worktree.py` (novo)
- `aru/runtime.py` (cwd_override, current_cwd helper)
- `aru/tools/codebase.py`, `aru/tools/shell.py`, etc (cwd-aware refactor)
- `aru/tools/delegate.py` (worktree wire, auto-background, progress)
- `aru/session.py` (SubagentTrace progress fields)
- `aru/commands.py` (/worktree *, /subagents --live)
- `tests/test_worktree.py` (novo)
- `tests/test_progress.py` (novo)

---

## 6. Ordem de implementação recomendada

**Sequencial por estágio** (dentro de um estágio, items podem parallelizar):

```
Estágio 1 (4-5h)
  #1.1 prompt lean           ┐
  #1.2 coordination gate     │  independentes — paralelizar
  #1.3 orchestrator AgentSpec│
  #1.4 notification enrich   ┘
  #1.5 smoke tests (precisa dos anteriores)

Estágio 2 (5-7h)
  #2.1 AgentSpec expand      ┐
  #2.2 YAML parser           │  independentes
  #2.3 max_turns             │
  #2.4 critical_reminder     │
  #2.5 MCP filter            ┘
  #2.6 tests consolidation

Estágio 3 (4-6h)
  #3.1 stop_task             ┐ independentes
  #3.2 send_message tool     ┘
  #3.3 Agno drain hook       ← investigação crítica; pode bloquear #3.2
  #3.4 tests

Estágio 4 (10-14h)
  #4.1 worktree + cwd-aware  ← maior individual; depende de auditar tools
  #4.2 progress tracker      ┐ independentes
  #4.3 --live                │
  #4.4 auto-background       ┘
  #4.5 tests
```

**Total estimado honesto:** 23-32h.

**Paralelismo real dentro de estágios:** até 40% ao usar múltiplas tasks simultâneas — implementador humano pode segmentar, Claude Code com várias agent invocations também.

---

## 7. Estratégia de testes

- **Sem regressão:** os 1287 testes atuais devem continuar passando a cada estágio. Rodar `pytest -q` ao final de cada item não-trivial.
- **Mocks do LLM:** usar `_FakeAgent` existente em `tests/test_delegate.py`. Estender se necessário para cenários novos.
- **#4.1 precisa `tmp_git_repo` fixture:** criar em `tests/conftest.py` — git init + commit inicial + lockfile.
- **Testes de timing** (auto-background, stop_task) precisam tolerância em Windows (10% slack).
- **Integração Agno:** para #2.3, #2.4, #3.3 — os tests devem mockar o Agno onde o hook é incerto. Se o mecanismo real falhar em runtime, teste cobre o fallback.

**Cobertura esperada** após tudo: 1287 → ~1380 (+90 testes novos).

---

## 8. Riscos e mitigações

| # | Risco | Impacto | Mitigação |
|---|---|:-:|---|
| R1 | Agno não suporta `max_iterations` nativo | médio | Hipótese 2 em #2.3 — guard manual no streaming loop |
| R2 | Agno não suporta injeção de user-msg mid-run | alto | Fallback em #3.3 — deliver só para bg tasks |
| R3 | `os.chdir` no worktree quebra asyncio concorrente | alto | #4.1 abandona chdir, usa cwd contextvar + tools cwd-aware |
| R4 | Refactor cwd-aware perde tools (silent bug) | alto | Audit-list completa em #4.1; teste parametrizado por tool-name verifica |
| R5 | `critical_reminder` infla contexto | baixo | Reminder é ≤200 chars; negligível vs system prompt |
| R6 | Auto-background surpreende usuário | médio | Mensagem explicita "AutoBackground"; env var para opt-out |
| R7 | `stop_task` gera race com agent em shutdown | baixo | abort_event é idempotent; graceful path já testado por #H |
| R8 | MCP filter esconde agents em erro transitório | médio | Apenas durante `_render_agent_list` (per-turn), sem side effect; MCP reconecta → agent volta |
| R9 | Recursão profunda (orchestrator → orchestrator) | médio | MAX_SUBAGENT_DEPTH=5 (já posto em #I); ajustar se necessário |
| R10 | Worktree em repo sem commits | baixo | `create_agent_worktree` detecta via `git rev-parse`, retorna None |

---

## 9. Arquivos tocados (sumário consolidado)

**Novos:**
- `aru/worktree.py`
- `aru/tools/stop_task.py`
- `aru/tools/send_message.py`
- `tests/test_delegate_prompt.py`
- `tests/test_orchestrator_e2e.py`
- `tests/test_task_control.py`
- `tests/test_worktree.py`
- `tests/test_progress.py`

**Modificados:**
- `aru/tools/delegate_prompt.txt` (reescrita)
- `aru/tools/delegate.py` (notification enrich, stop/send wire, worktree, auto-bg, progress)
- `aru/tools/registry.py` (novo tools, orchestrator tools)
- `aru/tools/codebase.py` (cwd-aware)
- `aru/tools/shell.py` (cwd-aware)
- demais tools com caminhos relativos (auditar)
- `aru/agents/catalog.py` (novos fields, orchestrator spec, annotations)
- `aru/agents/instructions.py` (COORDINATION_GATE)
- `aru/agent_factory.py` (consume new fields)
- `aru/config.py` (YAML parser expand)
- `aru/runtime.py` (per_task_aborts, per_task_inbox, cwd_override, current_cwd)
- `aru/session.py` (SubagentTrace progress fields)
- `aru/commands.py` (/worktree, /subagents --live)
- `tests/test_catalog.py` (estender)
- `tests/test_config.py` (estender)
- `tests/test_delegate.py` (ajustar para novos campos)

---

## 10. Referências

### Claude Code

- `src/tools/AgentTool/AgentTool.tsx` — tool schema (linhas 82-156) + isolation param (linhas 99)
- `src/tools/AgentTool/prompt.ts:66-287` — getPrompt com fork/non-fork
- `src/tools/AgentTool/prompt.ts:91-93` — don't peek / don't race
- `src/tools/AgentTool/loadAgentsDir.ts:106-155` — AgentDefinition full schema
- `src/tools/AgentTool/forkSubagent.ts:1-150` — fork mechanics (fora de escopo aqui)
- `src/tools/SendMessageTool/SendMessageTool.ts:67-87` — schema, recipient routing
- `src/tasks/LocalAgentTask/LocalAgentTask.tsx:41-103` — ProgressTracker
- `src/tasks/LocalAgentTask/LocalAgentTask.tsx:162-192` — queuePendingMessage / drainPendingMessages
- `src/coordinator/coordinatorMode.ts:111-368` — full coordinator system prompt
- `src/coordinator/coordinatorMode.ts:124` — "answer directly" gate
- `src/coordinator/coordinatorMode.ts:137-138` — "don't trivially report"
- `src/coordinator/coordinatorMode.ts:149-160` — `<task-notification>` schema
- `src/coordinator/coordinatorMode.ts:198-228` — phases + concurrency
- `src/coordinator/coordinatorMode.ts:283-291` — continue vs spawn matrix
- `src/utils/worktree.ts:902-952` — createAgentWorktree
- `src/tools/AgentTool/AgentTool.tsx:72-77` — getAutoBackgroundMs (120s default)

### Aru — pontos de mudança

- `aru/tools/delegate.py` — infra central; delegates, notifications, execution
- `aru/tools/delegate_prompt.txt` — prompt tool description
- `aru/agents/catalog.py` — AgentSpec catalog, novo orchestrator
- `aru/agents/instructions.py` — COORDINATION_GATE entry point
- `aru/agent_factory.py` — create_agent_from_spec, consumir novos campos
- `aru/runtime.py` — RuntimeContext expand + fork_ctx isolation
- `aru/session.py` — SubagentTrace, pending_notifications
- `aru/config.py` — YAML parser custom agents
- `aru/commands.py` — slash commands /worktree, /subagents

### Plano pai e entregas anteriores

- `docs/aru/plans/2026-04-19-multi-agent-parity.md` (items #A-#H — entregues)
- `docs/aru/plans/2026-04-19-multi-agent-tier2-orchestration.md` (plano anterior, Tier 1 #I entregue)
- `docs/aru/plans/2026-04-19-orchestrator-prompt-upgrade.md` (plano menor; super-set deste)

---

## 11. Decisões registradas

- **Fora de escopo:** fork (I), swarm/teams, coordinator mode global, remote backends — custos desproporcionais ao valor para single-user.
- **MAX_SUBAGENT_DEPTH=5** (já em vigor desde #I) — cap conservador; ajustar se orchestrator recursivo for padrão.
- **Fail-soft onipresente:** worktree sem git, MCP não conectado, Agno sem hook — todos degradam sem crash.
- **`<task-notification>` enrichment prioritária:** é a interface pela qual o orchestrator raciocina sobre cost/outcome; sem ela, as otimizações de decisão (estágio 1) ficam cegas.
- **Coordination gate mora no system prompt, não na tool description** — evita priming do primary para delegar em toda turn.
- **`orchestrator` como AgentSpec opt-in**, não modo global — permite validar em uso real sem comprometer fluxo simples do primary.
- **Per-task abort_event/inbox dicts compartilhados via fork_ctx** — evita propagação manual; um forked ctx enxerga o dict do pai por referência.
- **Tools cwd-aware via contextvar** (#4.1) — caminho correto para paralelismo real; `os.chdir` descartado.
