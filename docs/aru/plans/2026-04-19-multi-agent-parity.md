# Plano: Multi-agent / Paralelismo — Paridade OpenCode + aproximação Claude Code

**Criado:** 2026-04-19
**Status:** Proposta
**Objetivo:** Subir o critério 7 (Multi-agente / Paralelismo) de 5.5 para 8.5+, atingindo paridade funcional com OpenCode (8.0) e fechando parte do gap para Claude Code (9.0).

---

## 1. Contexto

Aru hoje tem **a capability core** (paralelismo via asyncio + fork_ctx isolando contexto + custom agents + métricas propagadas), mas falta um conjunto de primitivas maduras que OpenCode e Claude Code oferecem. A análise comparativa está em sessão anterior; o resumo:

| Primitiva | Aru (hoje) | OpenCode (8.0) | Claude Code (9.0) |
|---|:-:|:-:|:-:|
| Paralelismo de tool calls | ✅ implícito | ✅ documentado | ✅ documentado |
| Isolamento de contexto | ✅ `fork_ctx()` | ✅ `Session.create(parentID)` | ✅ agentId + replacementState |
| Custom agents | ✅ `AgentSpec` | ✅ | ✅ |
| Métricas propagadas | ✅ | ✅ | ✅ TTFT + OTPS |
| Retry em falha | ✅ (único!) | ❌ | ❌ |
| Permissão por subagent_type | ⚠️ infra pronta (Fase 3 do refactor), falta wire | ✅ `task:explorer` | ✅ `disallowedTools`/`tools` |
| Prompt de coordenação extenso | ❌ (2 linhas) | ✅ (~60 linhas) | ✅ (~200 linhas) |
| Abort propagation explícita | ⚠️ implícita via asyncio | ✅ `AbortSignal` | ✅ `AbortController` (sync/async split) |
| Resume via task_id | ❌ | ✅ | ✅ `resumeAgentBackground` |
| task_id observable | ❌ | ✅ (no output) | ✅ (registrado, visível via trace) |
| Sessão filha persistida | ❌ (só in-memory) | ✅ SQLite | ✅ JSONL + `.meta.json` |
| Built-in agents variados | ⚠️ só `explorer` | ⚠️ variável | ✅ explore, plan, verification, guide, general-purpose |
| Background mode | ❌ | ❌ | ✅ `run_in_background: true` |
| Multi-agent spawn coordenado | ❌ | ❌ | ⚠️ via N calls paralelos (não separado) |
| Trace estruturado de subagents | ❌ | ❌ | ✅ Perfetto com parentId |

### Referências de código nas duas bases

**OpenCode** (`D:\OneDrive\Documentos\ts_projects\opencode\packages\opencode\src\`):
- `tool/task.ts:15-156` — tool completa (task_id, permission check, session child, abort)
- `tool/task.txt:1-60` — prompt de coordenação
- `session/prompt.ts` — `SessionPrompt.prompt()`, `SessionPrompt.cancel()`
- `session/index.ts` — `Session.create({parentID})`
- `permission/index.ts:83-103` — DeniedError/RejectedError/CorrectedError

**Claude Code** (`D:\OneDrive\Documentos\ts_projects\claude-code\src\`):
- `tools/AgentTool/AgentTool.tsx:82-125` — schema (description, prompt, subagent_type, model, run_in_background)
- `tools/AgentTool/AgentTool.tsx:239-549` — execute (fork routing, async detection, teammate spawn)
- `tools/AgentTool/AgentTool.tsx:548-550` — `shouldRunAsync` decision
- `tools/AgentTool/builtInAgents.ts:22-72` — catálogo
- `tools/AgentTool/built-in/exploreAgent.ts:13-83` — Explore agent (haiku, `omitClaudeMd`, tools denylist)
- `tools/AgentTool/prompt.ts:66-287` — prompt de coordenação (~200 linhas, condicional)
- `tools/AgentTool/runAgent.ts:520-528` — AbortController (sync shared, async unlinked)
- `tools/AgentTool/runAgent.ts:808-858` — cleanup on abort (kill shell tasks, unregister perfetto)
- `tools/AgentTool/runAgent.ts:759-768` — TTFT forwarding to parent
- `tools/AgentTool/resumeAgent.ts:42-265` — resume com JSONL + meta.json
- `services/agent/LocalAgentTask.tsx:466-500` — `registerAsyncAgent` (fire-and-forget)
- `services/agent/sessionStorage.ts:1451-1461` — `recordSidechainTranscript`
- `services/agent/sessionStorage.ts:283-303` — `AgentMetadata` type + writer

**Aru** (este repo):
- `aru/tools/delegate.py:1-237` — `delegate_task`, `_SUBAGENT_TOOLS`, `set_custom_agents`
- `aru/agents/catalog.py` — `AgentSpec` para build/plan/executor/explorer
- `aru/runtime.py` — `fork_ctx()`
- `aru/permissions.py` — após refactor Fase 1-6 (Rule/Ruleset/evaluate/canonical_permission/disabled)

---

## 2. Escopo e targets

**Não-objetivos** (intencionalmente fora deste plano):
- Reescrever o loop do Agno (delegação de execução permanece)
- Migrar para outro framework
- Refactor de providers.py
- Multi-agent "swarm" (teammates) como Claude Code tem — raro caso de uso

**Objetivos por tier:**

| Tier | Delta nota 7 | Esforço | Entregas |
|:-:|:-:|:-:|---|
| 1 | 5.5 → 8.0 | ~5h | #A #B #C #D (permission + prompt + abort + resume in-memory) |
| 2 | 8.0 → 8.5 | +6h | #E #F (built-in agents + trace estruturado) |
| 3 | 8.5 → 9.0 | +8h | #G #H (persistência em disco + background mode) |

O plano é ordenado por ROI prático (não por tier), para que parar em qualquer ponto entregue valor concreto.

---

## 3. Items detalhados

### #A — Wire permissão por `subagent_type`
**Esforço:** 30-45min · **Prioridade:** média · **Depende de:** refactor Fase 3 (já feito)

**Goal:** habilitar `{"permission": {"task": {"explorer": "allow", "custom_dangerous": "ask"}}}` em `aru.json`.

**Estado atual:** Aru tem a infraestrutura pronta (`canonical_permission("delegate_task") -> "task"`, `evaluate()` com Ruleset). Falta apenas wire no `delegate_task`.

**Referência OpenCode** (`tool/task.ts:42-51`):
```typescript
if (!ctx.extra?.bypassAgentCheck) {
  await ctx.ask({
    permission: "task",
    patterns: [params.subagent_type],
    always: ["*"],
    metadata: {
      description: params.description,
      subagent_type: params.subagent_type,
    },
  })
}
```

**Implementação:**

1. Em `aru/tools/delegate.py`, antes do branch de builtin/custom/generic (linha ~62):
```python
# Resolve agent_name to canonical form for permission check
agent_for_permission = _agent_name or "generic"

from aru.permissions import check_permission, canonical_permission
# canonical_permission("delegate_task") returns "task"
# check_permission resolves via evaluate() with task as permission, agent_name as pattern
if not check_permission("delegate_task", agent_for_permission,
                         f"delegate to: {agent_for_permission}"):
    return f"[DENIED] Permission denied for delegating to agent: {agent_for_permission}"
```

2. Nota: `resolve_permission` hoje usa o argumento `category` como chave direta. Para que `{"task": ...}` no aru.json funcione, `delegate_task` precisa passar pela versão canonical. Duas opções:
   - **Opção 1 (rápida)**: mudar a chamada para `check_permission("task", ...)` diretamente
   - **Opção 2 (limpa)**: `check_permission` internamente aplicar `canonical_permission` quando resolver a regra
   
   Opção 2 é mais consistente com o modelo unificado. Adicionar em `resolve_permission`:
   ```python
   # After skip_permissions check
   canonical = canonical_permission(category)
   # ... rest uses `canonical` for rule lookup
   ```

**Testes:**
```python
def test_delegate_task_checks_subagent_permission():
    set_config(parse_permission_config({"task": {"explorer": "allow", "*": "ask"}}))
    # Mock the subagent execution
    # Verify check_permission called with ("delegate_task", "explorer") or ("task", "explorer")

def test_delegate_task_denied_subagent_type():
    set_config(parse_permission_config({"task": {"custom": "deny"}}))
    result = await delegate_task("do X", agent_name="custom")
    assert "DENIED" in result
```

**Arquivos tocados:** `aru/tools/delegate.py`, `aru/permissions.py` (resolve_permission aplica canonical), `tests/test_delegate.py` (novo ou existente).

---

### #B — Prompt de coordenação extenso
**Esforço:** 1-1.5h · **Prioridade:** ALTA · **Depende de:** nada

**Goal:** ensinar o modelo a usar `delegate_task` com competência — quando paralelizar, quando não delegar, como formular prompts.

**Referência Claude Code** (`tools/AgentTool/prompt.ts:66-287`, ~200 linhas condicionais):
- Core narrative (o que é o Agent tool)
- When-to-use / when-NOT-to-use (com exemplos: "read known file → use Read, not Agent")
- Usage notes numeradas (description curto, launch N agents em 1 mensagem, etc.)
- Writing prompts section ("brief like a colleague walking in", "never delegate understanding")
- Fork section (se feature flag on)
- Concurrency note

**Referência OpenCode** (`tool/task.txt:1-60`, conciso):
- "Launch a new agent to handle complex, multistep tasks autonomously"
- When to / NOT to use (3-5 bullets cada)
- "Launch multiple agents concurrently whenever possible, to maximize performance"
- "Each agent invocation starts with a fresh context unless you provide task_id"
- "Clearly tell the agent whether you expect it to write code or just research"

**Proposta para Aru** — criar `aru/tools/delegate_prompt.txt` com estrutura híbrida (mais enxuta que Claude Code, mais rica que docstring atual):

```
Launch a new agent to handle complex, multi-step tasks autonomously.

Each agent type has specific capabilities and tools available to it.
{agent_list}  <- injected via _update_delegate_task_docstring

## When to use

- Complex research tasks requiring multiple file reads + searches
- Independent subtasks that can run in parallel (fan-out pattern)
- Tasks requiring specialized tools (use agent_name to pick specialist)
- Keeping your own context clean — delegate noise-heavy exploration

## When NOT to use

- If you want to read a specific file path, use `read_file` or `glob_search` directly
- If you are searching for a specific symbol (e.g. "class Foo"), use `grep_search`
- If you are searching within 2-3 known files, use `read_files`
- One-line questions ("what's in README.md?") — just read it

## Parallelism

Launch multiple `delegate_task` calls in a single message whenever the
subtasks are independent. The runtime fans out via asyncio.create_task —
subagents run concurrently. Example:

    # Good: one turn, three parallel subagents
    delegate_task(task="find auth code", agent_name="explorer")
    delegate_task(task="find DB code", agent_name="explorer")
    delegate_task(task="find API code", agent_name="explorer")

    # Bad: three sequential turns
    # (do this only when subtask 2 depends on subtask 1's output)

## Writing the prompt

Brief the agent like a colleague walking into the room — they haven't
seen this conversation, don't know what you've tried, don't understand
why the task matters.

- Explain what you're trying to accomplish and why
- Describe what you've already learned or ruled out
- Give enough context for judgment calls (not just narrow instruction)
- If you need a short response, say so ("report in under 200 words")

**Never delegate understanding.** Don't write "based on your findings,
fix the bug" — that pushes synthesis onto the subagent. Include file
paths, line numbers, specific changes instead.

## Built-in agents

- `explorer`: fast read-only codebase exploration. Thoroughness levels
  "quick" | "medium" | "very thorough" specifiable in the task text.

## Expecting output

The subagent returns ONE summary message. It is NOT visible to the user —
you must relay the findings back. Tell the agent exactly what
information to return.
```

**Implementação:**

1. Criar arquivo `aru/tools/delegate_prompt.txt` com o conteúdo acima.

2. Modificar `_update_delegate_task_docstring()` em `aru/tools/delegate.py` (linha 204):
```python
def _update_delegate_task_docstring():
    """Dynamically update delegate_task's docstring to list available subagents."""
    import importlib.resources
    try:
        base_doc = (importlib.resources.files("aru.tools") / "delegate_prompt.txt").read_text()
    except Exception:
        base_doc = _FALLBACK_DOC  # short fallback

    # Inject built-in + custom agents into {agent_list} placeholder
    lines = ["- `explorer`: Fast read-only codebase exploration..."]
    custom_agent_defs = get_ctx().custom_agent_defs
    for name, agent_def in (custom_agent_defs or {}).items():
        lines.append(f"- `{name}`: {agent_def.description}")
    agent_list = "\n".join(lines)

    delegate_task.__doc__ = base_doc.replace("{agent_list}", agent_list)
```

3. Incluir `delegate_prompt.txt` em `pyproject.toml` como package data:
```toml
[tool.hatch.build.targets.wheel.force-include]
"aru/tools/delegate_prompt.txt" = "aru/tools/delegate_prompt.txt"
```

**Testes:**
```python
def test_delegate_task_docstring_contains_parallelism_guidance():
    _update_delegate_task_docstring()
    assert "parallel" in delegate_task.__doc__.lower()
    assert "single message" in delegate_task.__doc__.lower()

def test_delegate_task_docstring_lists_custom_agents():
    set_custom_agents({"reviewer": AgentDef(description="code review", ...)})
    assert "reviewer" in delegate_task.__doc__
    assert "code review" in delegate_task.__doc__
```

**Arquivos tocados:** `aru/tools/delegate_prompt.txt` (novo), `aru/tools/delegate.py`, `pyproject.toml`, `tests/test_delegate.py`.

---

### #C — Abort propagation explícita
**Esforço:** 1.5-2h · **Prioridade:** ALTA · **Depende de:** nada

**Goal:** quando o usuário dá Ctrl+C, subagents param limpamente. Sem zombies.

**Referência Claude Code** (`runAgent.ts:520-528`):
```typescript
const agentAbortController = override?.abortController
  ? override.abortController
  : isAsync
    ? new AbortController()          // Async: unlinked
    : toolUseContext.abortController  // Sync: shared
```

**Referência OpenCode** (`tool/task.ts:113-117`):
```typescript
function cancel() {
  SessionPrompt.cancel(session.id)
}
ctx.abort.addEventListener("abort", cancel)
using _ = defer(() => ctx.abort.removeEventListener("abort", cancel))
```

**Implementação:**

1. Adicionar `abort_event: asyncio.Event` em `RuntimeContext` (`aru/runtime.py`):
```python
@dataclass
class RuntimeContext:
    # ... existing fields
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
```

2. Em `fork_ctx()`, subagent herda o `abort_event` do pai (para propagação de cancelamento):
```python
def fork_ctx() -> RuntimeContext:
    parent = get_ctx()
    return RuntimeContext(
        # ... other fields
        abort_event=parent.abort_event,  # shared
        # ... other fields isolated
    )
```

3. Em `aru/tools/delegate.py`, checar o event durante o streaming:
```python
async def _execute_with_streaming(agent_instance) -> str:
    # ... existing setup
    abort_event = get_ctx().abort_event

    async for event in agent_instance.arun(task, stream=True, ...):
        if abort_event.is_set():
            return f"[{label}] Cancelled by user."
        # ... handle event
```

4. No REPL (`aru/cli.py`), ligar SIGINT ao `abort_event`:
```python
import signal
def _on_sigint(*_):
    ctx = get_ctx()
    ctx.abort_event.set()

# In main loop setup
signal.signal(signal.SIGINT, _on_sigint)
```

5. Após cancelamento bem-sucedido, limpar o event (`abort_event.clear()`) antes do próximo prompt.

**Nota sobre bash subprocess:** shell.py já tem tracking de processos background (`aru/tools/shell.py`). Adicionar cleanup de subprocessos do subagent no caminho de abort — matar processos cujos PIDs foram spawned por tasks cancelled.

**Testes:**
```python
@pytest.mark.asyncio
async def test_subagent_stops_on_abort():
    ctx = init_ctx()
    # Start a delegate_task in background
    task = asyncio.create_task(delegate_task("long task", agent_name="explorer"))
    await asyncio.sleep(0.1)
    ctx.abort_event.set()
    result = await task
    assert "Cancelled" in result

@pytest.mark.asyncio
async def test_abort_propagates_to_nested_subagent():
    # Primary → subagent A → abort → A stops
    # (fork_ctx shares abort_event)
    pass
```

**Arquivos tocados:** `aru/runtime.py`, `aru/tools/delegate.py`, `aru/cli.py`, `tests/test_delegate_abort.py` (novo).

---

### #D — Resume via `task_id` (in-memory)
**Esforço:** 2-3h · **Prioridade:** média · **Depende de:** nada

**Goal:** permitir continuar um subagent sem pagar setup + contexto de novo. Sobrevive dentro da sessão primária; expira quando ela termina.

**Referência OpenCode** (`tool/task.ts:29-34, 60-63, 138-144`):
```typescript
// Schema
task_id: z.string()
  .describe("pass a prior task_id and the task will continue the same subagent session")
  .optional(),

// Lookup
if (params.task_id) {
  const found = await Session.get(SessionID.make(params.task_id)).catch(() => {})
  if (found) return found
}
return await Session.create({ parentID: ctx.sessionID, ... })

// Output
const output = [
  `task_id: ${session.id} (for resuming to continue this task if needed)`,
  "",
  "<task_result>",
  text,
  "</task_result>",
].join("\n")
```

**Referência Claude Code** (`resumeAgent.ts:42-265`):
- Função separada `resumeAgentBackground({agentId, prompt, ...})`
- Carrega transcript de disco via `getAgentTranscript(agentId)`
- Filtra mensagens órfãs (unresolved tool uses, whitespace-only, thinking-only)
- Reconstroi `contentReplacementState` para estabilidade de prompt cache
- Para Aru (in-memory first), não precisa da complexidade de replacement state.

**Implementação:**

1. Adicionar storage in-memory em `RuntimeContext`:
```python
# aru/runtime.py
@dataclass
class RuntimeContext:
    # ...
    subagent_instances: dict[str, Any] = field(default_factory=dict)
    # task_id -> Agent instance (live reference)
```

2. Modificar signature de `delegate_task` (`aru/tools/delegate.py:36`):
```python
async def delegate_task(
    task: str,
    context: str = "",
    agent_name: str = "",
    task_id: str = "",
) -> str:
    """
    ...
    Args:
        task: What the sub-agent should do.
        context: Optional extra context (file paths, constraints).
        agent_name: Name of a specialized agent to use.
        task_id: If set, resume a prior sub-agent session instead of creating
                 a fresh one. Use the task_id returned from a previous
                 delegate_task call to continue that conversation.
    """
```

3. No branch de execução, checar task_id primeiro:
```python
import uuid

# In _run()
resumed = False
if task_id:
    existing = get_ctx().subagent_instances.get(task_id)
    if existing is not None:
        sub = existing
        resumed = True
        label = sub.name
        agent_id = task_id  # preserve original id
    # else: task_id not found → silently create fresh

if not resumed:
    # Existing branches (builtin/custom/generic)
    # ...
    agent_id = uuid.uuid4().hex[:8]
    # After creating sub:
    get_ctx().subagent_instances[agent_id] = sub
```

4. Output carrega o task_id para o modelo:
```python
final_text = run_output.content if run_output and run_output.content else result_content
if final_text:
    return _truncate_output(
        f"[{label} | task_id={agent_id}] {final_text}"
    )
```

5. Atualizar docstring (em `delegate_prompt.txt`) para explicar:
```
## Resume

Each delegate_task returns a task_id in its header:
    [SubAgent-abc123 | task_id=abc123] ...

To continue the same sub-agent session (with its prior context and tool outputs),
pass that task_id back:

    delegate_task(task="also check X", task_id="abc123")

Fresh delegation (no task_id) starts with a blank slate — cheaper the first time
but loses conversation history. Resume when you need iterative refinement on
the same topic.
```

**Testes:**
```python
@pytest.mark.asyncio
async def test_delegate_task_new_session_has_task_id():
    result = await delegate_task("test", agent_name="explorer")
    assert "task_id=" in result

@pytest.mark.asyncio
async def test_delegate_task_resume_reuses_instance():
    # First call
    result1 = await delegate_task("remember X", agent_name="explorer")
    task_id = _extract_task_id(result1)
    # Check instance registered
    assert task_id in get_ctx().subagent_instances
    # Resume
    sub_before = get_ctx().subagent_instances[task_id]
    result2 = await delegate_task("what did I say about X?", task_id=task_id)
    sub_after = get_ctx().subagent_instances[task_id]
    assert sub_before is sub_after  # same instance reused

@pytest.mark.asyncio
async def test_delegate_task_unknown_task_id_creates_fresh():
    result = await delegate_task("test", task_id="nonexistent")
    # Should silently create new, not error
    assert "task_id=" in result
    assert "Error" not in result
```

**Arquivos tocados:** `aru/runtime.py`, `aru/tools/delegate.py`, `aru/tools/delegate_prompt.txt`, `tests/test_delegate_resume.py` (novo).

---

### #E — Built-in agents adicionais
**Esforço:** 2-3h · **Prioridade:** alta · **Depende de:** nada

**Goal:** expandir de 1 (`explorer`) para 4-5 built-ins cobrindo os casos mais comuns.

**Referência Claude Code** (`builtInAgents.ts:22-72`):
```typescript
const agents: AgentDefinition[] = [GENERAL_PURPOSE_AGENT, STATUSLINE_SETUP_AGENT]
if (areExplorePlanAgentsEnabled()) {
  agents.push(EXPLORE_AGENT, PLAN_AGENT)
}
if (isNonSdkEntrypoint) agents.push(CLAUDE_CODE_GUIDE_AGENT)
if (feature('VERIFICATION_AGENT') && ...) agents.push(VERIFICATION_AGENT)
```

**Exploradores e suas características** (ref `exploreAgent.ts:13-83`):
- **Explore**: tool set = read-only (grep, glob, read, list), model = haiku (fast), omitClaudeMd=true
- **Plan**: similar a explore mas com foco em planejar; permissionMode='plan'
- **General-purpose**: full tool set, sem restrição
- **Verification**: lê diff produzido, valida. disallowedTools = [Edit, Write, Bash]
- **Guide**: responde sobre o próprio tool (meta-agent)

**Proposta para Aru** — adicionar ao `aru/agents/catalog.py`:

```python
# Existing: build, plan, executor, explorer

# NEW: verification — double-check after large edits
AGENTS["verification"] = AgentSpec(
    key="verification",
    role="verifier",
    mode="subagent",
    tools_factory=lambda: EXPLORER_TOOLS,  # read-only
    max_tokens=4096,
    description=(
        "Review a recent batch of edits for correctness. Runs read-only: "
        "greps for affected call sites, reads changed files, reports issues. "
        "Use after completing non-trivial multi-file edits to catch "
        "inconsistencies before the user sees them."
    ),
    instructions_extra="""\
You are a verification subagent. Your job:
1. Read the files mentioned in the task
2. Look for call sites / references to changed APIs
3. Report: (a) inconsistencies found, (b) missing follow-up edits,
   (c) things that look suspicious but you're not sure
Never edit or write files. Never run mutating shell commands.
""",
    small_model=True,  # use small_model_ref
)

# NEW: code-reviewer — structural review of a diff
AGENTS["reviewer"] = AgentSpec(
    key="reviewer",
    role="reviewer",
    mode="subagent",
    tools_factory=lambda: EXPLORER_TOOLS,
    max_tokens=4096,
    description=(
        "Code review against common quality heuristics: naming, error "
        "handling, test coverage gaps, security smells. Runs read-only."
    ),
    instructions_extra="""\
You are a code reviewer. For each file mentioned in the task:
- Naming: are identifiers clear and consistent with the codebase?
- Error handling: are edge cases covered? any swallowed exceptions?
- Tests: are there tests covering the new code paths?
- Security: any obvious injection, path traversal, secret leaks?
Report actionable findings with file:line refs. Skip nits.
""",
    small_model=True,
)

# NEW: guide — answers questions about aru itself
AGENTS["guide"] = AgentSpec(
    key="guide",
    role="guide",
    mode="subagent",
    tools_factory=lambda: EXPLORER_TOOLS,
    max_tokens=4096,
    description=(
        "Answer questions about using Aru itself — slash commands, "
        "permission config, skills, plugins, tool catalog. Reads AGENTS.md "
        "and docs/ to ground answers."
    ),
    instructions_extra="""\
You are the Aru user-guide subagent. Questions are about how to use or
configure Aru, not about the user's codebase. Sources of truth:
- AGENTS.md (architecture reference)
- docs/*.md (user-facing docs)
- aru.json schema (default config examples)
Never invent features. If docs don't cover it, say so and suggest the
closest alternative.
""",
    small_model=True,
)
```

**Delegação automática via docstring** — `_update_delegate_task_docstring` já injeta a lista. Nenhuma mudança adicional.

**Testes:**
```python
def test_builtin_agent_verification_registered():
    from aru.agents.catalog import AGENTS
    assert "verification" in AGENTS
    spec = AGENTS["verification"]
    assert spec.mode == "subagent"
    # Tools should be read-only
    tools = spec.tools_factory()
    assert all(t.__name__ not in {"write_file", "edit_file", "bash"}
               for t in tools)

@pytest.mark.asyncio
async def test_delegate_to_verification_uses_readonly_tools():
    result = await delegate_task(
        "review changes in foo.py",
        agent_name="verification"
    )
    # Should not error, result format preserved
    assert "[Verification" in result or "task_id=" in result
```

**Arquivos tocados:** `aru/agents/catalog.py`, `aru/tools/delegate.py` (branch para cada built-in — ou refatorar para lookup genérico), `tests/test_catalog.py`.

**Nota de refactor:** hoje o branch em `delegate.py:63-107` trata `explorer` específicamente via `create_agent_from_spec(AGENTS["explorer"], ...)`. Refatorar para lookup genérico por built-in — qualquer key em AGENTS com mode="subagent" funciona. Reduz duplicação e novos built-ins ficam "grátis".

---

### #F — Trace estruturado de subagents
**Esforço:** 3-4h · **Prioridade:** alta (overlap com critério 9) · **Depende de:** nada

**Goal:** registrar cada invocação de subagent com (id, parent_id, agent_name, task, tool_calls, tokens, duration, result_preview). Comandos `/subagents` e `/subagent <id>` para inspecionar.

**Referência Claude Code** — Perfetto tracing:
```typescript
// runAgent.ts:355-359
if (isPerfettoTracingEnabled()) {
  const parentId = toolUseContext.agentId ?? getSessionId()
  registerPerfettoAgent(agentId, agentDefinition.agentType, parentId)
}

// runAgent.ts:832 (cleanup)
unregisterPerfettoAgent(agentId)
```

**Implementação:**

1. Novo dataclass em `aru/session.py`:
```python
@dataclass
class SubagentTrace:
    task_id: str
    parent_id: str | None
    agent_name: str
    task: str  # truncated to first 200 chars
    started_at: float
    ended_at: float | None
    tokens_in: int
    tokens_out: int
    tool_calls: list[dict]  # [{tool, args_preview, duration, result_preview}]
    result: str  # truncated
    status: Literal["running", "completed", "cancelled", "error"]
```

2. Popular em `aru/tools/delegate.py:_execute_with_streaming`:
```python
import time

async def _execute_with_streaming(agent_instance) -> str:
    start = time.monotonic()
    trace = SubagentTrace(
        task_id=agent_id,
        parent_id=get_ctx().parent_agent_id,  # add this field to ctx
        agent_name=sub.name,
        task=task[:200],
        started_at=start,
        ended_at=None,
        tokens_in=0, tokens_out=0,
        tool_calls=[],
        result="",
        status="running",
    )
    session = get_ctx().session
    if session is not None:
        session.subagent_traces.append(trace)

    try:
        async for event in agent_instance.arun(...):
            # ... existing handling
            if isinstance(event, ToolCallCompletedEvent):
                trace.tool_calls.append({
                    "tool": event.tool.tool_name,
                    "args_preview": str(event.tool.tool_args)[:100],
                    "duration": dur,
                })
            # ... etc

        trace.ended_at = time.monotonic()
        trace.status = "completed"
        trace.result = final_text[:500] if final_text else ""
        trace.tokens_in = getattr(m, "input_tokens", 0)
        trace.tokens_out = getattr(m, "output_tokens", 0)
        # ... return
    except Exception:
        trace.status = "error"
        trace.ended_at = time.monotonic()
        raise
```

3. Novos comandos em `aru/commands.py`:
```python
def cmd_subagents(args: str):
    """List subagents from the current session as a tree."""
    session = get_ctx().session
    traces = session.subagent_traces
    if not traces:
        print("No subagents in this session.")
        return
    # Render as tree: parent_id links children
    tree = _build_trace_tree(traces)
    _print_trace_tree(tree)  # uses rich.tree

def cmd_subagent(args: str):
    """Show detailed trace for a single subagent by task_id."""
    task_id = args.strip()
    session = get_ctx().session
    trace = next((t for t in session.subagent_traces if t.task_id == task_id), None)
    if not trace:
        print(f"No subagent with task_id={task_id}")
        return
    # Print full trace: header, task, each tool call, result
    ...
```

4. Registrar em `aru/commands.py` no dict de handlers.

**Testes:**
```python
@pytest.mark.asyncio
async def test_subagent_trace_registered_on_execution():
    session = Session()
    ctx = init_ctx()
    ctx.session = session
    await delegate_task("test", agent_name="explorer")
    assert len(session.subagent_traces) == 1
    trace = session.subagent_traces[0]
    assert trace.status == "completed"
    assert trace.agent_name.startswith("Explorer")
    assert trace.ended_at is not None
    assert trace.tokens_in >= 0
```

**Arquivos tocados:** `aru/session.py`, `aru/tools/delegate.py`, `aru/commands.py`, `aru/runtime.py` (add parent_agent_id field), `tests/test_subagent_trace.py` (novo).

---

### #G — Sessão filha persistida em disco
**Esforço:** 3-4h · **Prioridade:** baixa-média · **Depende de:** #D, #F

**Goal:** traces e task_id sobrevivem a restart do REPL. Pode retomar subagent de sessão anterior.

**Referência Claude Code** (`sessionStorage.ts:1451-1461` + `sessionStorage.ts:283-303`):
- JSONL transcript: `.claude/subagents/{sessionId}/transcript-{agentId}.jsonl`
- `.meta.json`: `{agentType, worktreePath, description}`
- Fire-and-forget writes

**Referência OpenCode**: usa SQLite com tabelas de sessão (parentID na linha).

**Implementação:**

1. Extender `.aru/sessions/<session_id>/subagents/<task_id>/` com:
   - `trace.json` — SubagentTrace serializado
   - `messages.jsonl` — mensagens do Agno (para resume completo)
   - `metadata.json` — `{agent_name, parent_task_id, created_at}`

2. No `_execute_with_streaming`, escrever fire-and-forget após completion:
```python
async def _persist_subagent(trace, messages, session_dir):
    path = Path(session_dir) / "subagents" / trace.task_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "trace.json").write_text(json.dumps(asdict(trace)))
    (path / "messages.jsonl").write_text(
        "\n".join(json.dumps(m) for m in messages)
    )
    (path / "metadata.json").write_text(json.dumps({
        "agent_name": trace.agent_name,
        "parent_task_id": trace.parent_id,
        "created_at": trace.started_at,
    }))

# After completion:
asyncio.create_task(_persist_subagent(trace, messages, session.dir))
```

3. No resume path (#D), se task_id não está em `subagent_instances` mas EXISTE no disco, reconstruir agent:
```python
if task_id and task_id not in ctx.subagent_instances:
    persisted = load_persisted_subagent(task_id, session.dir)
    if persisted:
        sub = rebuild_agent_from_messages(persisted)
        ctx.subagent_instances[task_id] = sub
        resumed = True
```

4. Novo comando `/subagents all` que mostra subagents de sessões anteriores do projeto.

**Testes:**
```python
@pytest.mark.asyncio
async def test_subagent_persists_to_disk():
    session = create_session(dir=tmp_path)
    ctx.session = session
    result = await delegate_task("test", agent_name="explorer")
    task_id = _extract_task_id(result)
    trace_file = tmp_path / "subagents" / task_id / "trace.json"
    assert trace_file.exists()

@pytest.mark.asyncio
async def test_resume_from_disk_after_restart():
    # Session 1: spawn subagent
    session1 = create_session(...)
    result = await delegate_task(...)
    task_id = _extract_task_id(result)

    # Simulate restart — new ctx, same session dir
    new_ctx = init_ctx()
    new_ctx.session = load_session(session1.dir)

    # Resume with task_id
    result2 = await delegate_task("continue", task_id=task_id)
    # Should work — loaded from disk
    assert "[SubAgent" in result2
```

**Arquivos tocados:** `aru/session.py` (persistência), `aru/tools/delegate.py`, `aru/commands.py`, `tests/test_subagent_persistence.py` (novo).

---

### #H — Background mode
**Esforço:** 3-4h · **Prioridade:** baixa · **Depende de:** #D

**Goal:** `delegate_task(run_in_background=True)` retorna imediatamente com task_id; primary agent continua trabalhando; quando sub termina, um `<task-notification>` é inserido na próxima mensagem para o primary.

**Referência Claude Code**:
- `AgentTool.tsx:548-550`: `shouldRunAsync = run_in_background === true || selectedAgent.background`
- `LocalAgentTask.tsx:466-500`: `registerAsyncAgent` — fire-and-forget, returns `{agentId, abortController}` sem esperar
- Notification model: emit `<task-notification>` XML block routed as user-role message em turn posterior
- Parent **não faz polling** — recebe notificação

**Implementação:**

1. Signature estendida:
```python
async def delegate_task(
    task: str,
    context: str = "",
    agent_name: str = "",
    task_id: str = "",
    run_in_background: bool = False,
) -> str:
```

2. Se `run_in_background=True`:
```python
if run_in_background:
    # Register background task
    bg_task = asyncio.create_task(_run())
    bg_id = f"bg-{agent_id}"
    get_ctx().background_tasks[bg_id] = bg_task
    return (
        f"[BackgroundTask | task_id={bg_id}] "
        f"Dispatched to {label} in background. "
        f"You will receive a notification when complete. "
        f"Do NOT poll — continue with other work."
    )
```

3. Ao completar, injetar notificação na fila da sessão. Um ponto conveniente:
```python
# In the background task coroutine:
try:
    result = await _run()
finally:
    # Append to session's pending_notifications
    session.pending_notifications.append({
        "task_id": bg_id,
        "result": result,
        "at": time.time(),
    })
```

4. Antes de cada turn do primary agent (em `runner.py` ou `cli.py`), drain pending_notifications e injetar como mensagem user-role:
```python
def _drain_notifications(session):
    pending = session.pending_notifications
    if not pending:
        return []
    session.pending_notifications = []
    return [{
        "role": "user",
        "content": (
            f"<task-notification>\n"
            f"Background task {n['task_id']} completed:\n"
            f"{n['result']}\n"
            f"</task-notification>"
        ),
    } for n in pending]

# In main loop, before agent.arun:
notifications = _drain_notifications(session)
if notifications:
    # Add to conversation history
    ...
```

5. Novo comando `/bg` lista background tasks em execução / completed.

**Testes:**
```python
@pytest.mark.asyncio
async def test_background_returns_immediately():
    t0 = time.monotonic()
    # Use an artificially slow subagent for the test
    result = await delegate_task("slow task", run_in_background=True)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5  # must not wait
    assert "task_id=bg-" in result
    assert "notification" in result.lower()

@pytest.mark.asyncio
async def test_background_notification_injected_on_completion():
    session = Session()
    ctx.session = session
    await delegate_task("test", run_in_background=True)
    # Wait for completion
    await asyncio.sleep(2)
    assert len(session.pending_notifications) == 1
    # Drain
    msgs = _drain_notifications(session)
    assert "task-notification" in msgs[0]["content"]
    assert session.pending_notifications == []
```

**Arquivos tocados:** `aru/tools/delegate.py`, `aru/session.py` (pending_notifications), `aru/runner.py` (drain no loop), `aru/commands.py` (/bg), `tests/test_delegate_background.py` (novo).

---

## 4. Ordem de execução recomendada

Sequência por **ROI prático** (impacto observável / hora):

| # | Item | ROI | Horas | Nota após |
|:-:|---|:-:|:-:|:-:|
| 1 | #B Prompt de coordenação | ⭐⭐⭐⭐⭐ | 1-1.5 | 5.5 → 6.5 |
| 2 | #C Abort propagation | ⭐⭐⭐⭐ | 1.5-2 | 6.5 → 7.0 |
| 3 | #E Built-in agents | ⭐⭐⭐⭐ | 2-3 | 7.0 → 7.5 |
| 4 | #F Trace estruturado | ⭐⭐⭐⭐ | 3-4 | 7.5 → 8.0 (+0.3 em crit 9) |
| 5 | #A Wire permission | ⭐⭐ | 0.5-0.75 | 8.0 → 8.2 |
| 6 | #D Resume in-memory | ⭐⭐ | 2-3 | 8.2 → 8.5 |
| 7 | #G Persistência disco | ⭐ | 3-4 | 8.5 → 8.7 |
| 8 | #H Background mode | ⭐ | 3-4 | 8.7 → 9.0 |

**Parada natural em #4**: entrega 8.0 (paridade OpenCode) em ~8h. Os itens 5-8 são polimento com retornos decrescentes.

### Dependências

```
#A — independente
#B — independente
#C — independente
#D — independente (mas melhor depois de #C para integração limpa com abort)
#E — independente (mas depois de #B pra agentes aparecerem no prompt)
#F — independente (melhor depois de #D para ter task_id sólido)
#G — depende de #D e #F
#H — depende de #D
```

---

## 5. Estratégia de testes

### Princípios

1. **Nenhuma regressão nos 1221 testes existentes** — rodar suite completa após cada PR.
2. **Novos testes** por item: unit + integration (cenário end-to-end simulando uso real).
3. **Mocks sensíveis**: não chamar provedores LLM reais nos testes — usar `MockAgent` ou `monkeypatch` em `agent_factory.create_agent_from_spec`.
4. **Async tests**: usar `@pytest.mark.asyncio` (já configurado via `asyncio_mode = "auto"` no pyproject).
5. **Cobertura mínima**: cada função pública nova tem ao menos 3 testes (happy path, edge case, error path).

### Comandos para suite

```bash
# Teste do módulo afetado
./.venv/Scripts/python.exe -m pytest tests/test_delegate.py tests/test_delegate_resume.py tests/test_subagent_trace.py -q

# Suite completa (~30s)
./.venv/Scripts/python.exe -m pytest -q

# Cobertura focada
./.venv/Scripts/python.exe -m pytest --cov=aru.tools.delegate --cov=aru.runtime --cov-report=term-missing
```

---

## 6. Riscos e mitigações

| Risco | Impacto | Mitigação |
|---|:-:|---|
| `fork_ctx()` herdando `abort_event` quebra isolamento existente | alto | Adicionar teste que verifica que OUTROS campos continuam isolados; só abort_event compartilhado |
| Resume in-memory vaza memória em sessões longas (N subagents) | médio | TTL ou LRU eviction: manter últimos 20 subagents; evict mais antigos |
| Docstring grande explode contexto do LLM | baixo | Medir tokens do prompt antes/depois; se >1k tokens, cortar seção "Writing the prompt" (deixar só no comment do schema) |
| Background mode causa race em `session.pending_notifications` | médio | Usar `asyncio.Lock` ou `queue.Queue` thread-safe |
| Persistência em disco quebra em Windows com paths longos | baixo | Usar `uuid.uuid4().hex[:8]` para task_id (8 chars), não full UUID |
| Built-in verification agent chama `delegate_task` recursivamente | médio | `_SUBAGENT_TOOLS` já exclui `delegate_task` — verificar que novos built-ins também não têm acesso |
| Trace struct cresce indefinidamente em sessões longas | baixo | Truncate histórico: manter últimas 100 traces por sessão |
| Abort deixa file locks em tools/shell.py | médio | Cleanup explícito em try/finally no `_execute_with_streaming` |
| Custom agents com mesmo nome de built-in são shadow | baixo | `set_custom_agents` já filtra por `mode="subagent"`; built-ins têm precedência ou warn ao shadow |

---

## 7. Métricas de sucesso

Após completar Tier 1 + 2 (itens #A-#F), esperamos:

1. **Comportamento observável do modelo**: delegate_task paralelizado em 2+ calls na mesma mensagem >30% das vezes em tarefas de exploração (hoje: ~10%). Medição via `session.subagent_traces` agrupando por `started_at` em janelas de 1s.

2. **Tempo de resposta em pesquisa**: tarefas de "encontre X, Y e Z" terminam em tempo proporcional ao item mais lento (paralelo), não à soma (sequencial). Speedup esperado: 2-3x em fan-outs de 3-5 itens.

3. **Zombie subagents após Ctrl+C**: zero. Verificar via `subagent_instances` esvaziado e nenhum asyncio.Task pendente após abort.

4. **Custom agents discoveráveis**: `/help` e o docstring de `delegate_task` listam todos os built-ins + custom. Usuários não precisam consultar código.

5. **Nota crit 7**: 8.0+ em auto-avaliação contra os 10 critérios da skill `agentic-cli-comparator`.

6. **Testes**: suite verde, ≥80% cobertura em `delegate.py`, `runtime.py`, `session.py` (áreas tocadas).

---

## 8. Referências

### Código-fonte consultado

**OpenCode** (`D:\OneDrive\Documentos\ts_projects\opencode\packages\opencode\src\`):
- `tool/task.ts:15-156`
- `tool/task.txt:1-60`
- `permission/index.ts:83-103, 297-308`
- `session/index.ts`, `session/prompt.ts`

**Claude Code** (`D:\OneDrive\Documentos\ts_projects\claude-code\src\`):
- `tools/AgentTool/AgentTool.tsx:82-125, 239-549, 548-550`
- `tools/AgentTool/builtInAgents.ts:22-72`
- `tools/AgentTool/built-in/exploreAgent.ts:13-83`
- `tools/AgentTool/prompt.ts:66-287`
- `tools/AgentTool/runAgent.ts:355-359, 520-528, 759-768, 808-858`
- `tools/AgentTool/resumeAgent.ts:42-265`
- `tools/AgentTool/forkSubagent.ts:32-168`
- `services/agent/LocalAgentTask.tsx:466-500`
- `services/agent/sessionStorage.ts:283-303, 1451-1461`
- `services/agent/agentToolUtils.ts:122-200`

**Aru** (este repo):
- `aru/tools/delegate.py:1-237`
- `aru/agents/catalog.py`
- `aru/runtime.py`
- `aru/permissions.py` (após refactor Fase 1-6)
- `aru/session.py`

### Documentos relacionados

- `AGENTS.md` — arquitetura geral
- `docs/aru/plans/2026-04-17-skill-disallowed-tools.md` — plano anterior mencionando `disallowed_tools` (consumido por `tool_policy.skill_rules`)
- Skill `agentic-cli-comparator` — framework de avaliação com os 10 critérios

### Decisões registradas na conversa original

- **Não renomear tools** do Aru (`delegate_task` continua, não vira `task`) — apenas canonical name em permissions.
- **Resume começa in-memory** (item #D) — persistência em disco (#G) é tier 3, opcional.
- **Aru preserva** diferenciais próprios: retry com recreate em falha, `fork_ctx` sofisticado, métricas propagadas para session pai. Não reimplementar por imitação.
- **Bash compound splitting** (aru-específico, `_resolve_bash_compound`) é feature positiva — preservar durante qualquer refactor de permissions.
