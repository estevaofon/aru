# Plano: Orquestrador mais inteligente — prompt + AgentSpec dedicado

**Criado:** 2026-04-19
**Status:** Proposta
**Depende de:** #I (recursão destravada — já entregue)
**Objetivo:** Fazer o orquestrador de agents do Aru decidir melhor entre single-agent e multi-agent, synthesizar ao invés de delegar entendimento, e reconhecer as fases de trabalho (research / synthesis / implementation / verification).

---

## 1. Contexto

Com #I entregue, um `AgentSpec` pode ter `delegate_task` no toolset e se comportar como orquestrador. Mas o prompt atual (`aru/tools/delegate_prompt.txt`) é genérico — diz "quando usar / quando não usar" em bullets curtos e não ensina as heurísticas que fazem Claude Code escolher bem.

Comparação concreta contra `claude-code/src/tools/AgentTool/prompt.ts` + `claude-code/src/coordinator/coordinatorMode.ts` identifica 7 padrões ausentes:

| Padrão | Aru | CC |
|---|:-:|:-:|
| Synthesis é do orquestrador (nunca delegada), com anti-exemplos | parcial | sim |
| Matriz Continue-vs-Spawn por overlap de contexto | não | sim |
| Phases framework (Research → Synthesis → Impl → Verify) | não | sim |
| Concurrency por tipo de trabalho (read-only vs write-heavy) | genérico | específico |
| Verificação = provar que funciona, não confirmar que existe | não | sim |
| Don't peek / don't race (não fabricar resultados mid-flight) | não | sim |
| Bad examples concretos de prompts ruins | não | sim |

Adicionalmente, duas frases do `coordinatorMode.ts` resolvem ~60% do problema de "não delegar quando não deve":

- `"Answer questions directly when possible — don't delegate work that you can handle without tools."` (linha 124)
- `"Do not use workers to trivially report file contents or run commands. Give them higher-level tasks."` (linhas 137-138)

---

## 2. Escopo

Dois deliverables + uma validação. **Total: 4-5h reais.**

| # | Item | Esforço | Depende |
|:-:|---|:-:|---|
| #O | Reescrita do `delegate_prompt.txt` | 2-3h | nada |
| #P | `AgentSpec` `orchestrator` com extra_instructions | 1h | #O |
| #Q | Smoke test em tarefa real (validação) | 0.5-1h | #P |

**Não é objetivo:** criar modo coordinator de sessão inteira (como `CLAUDE_CODE_COORDINATOR_MODE`). Isso só se justifica depois que `orchestrator` como AgentSpec provar valor em uso real.

**Não é objetivo:** subir nota externa de critério 7. Os padrões acima melhoram qualidade do orquestrador, não cobrem os 4 gaps estruturais (SQLite, worktree, swarm, dynamic catalog). Isso é melhoria **dentro** da capacidade existente, não nova capacidade.

---

## 3. Items detalhados

### #O — Reescrita do `delegate_prompt.txt`

**Esforço:** 2-3h · **Prioridade:** alta · **Depende de:** nada

**Goal:** incorporar os 7 padrões do Claude Code no prompt que todo caller de `delegate_task` lê. Isso afeta tanto o primary agent quanto qualquer subagent com `can_delegate`.

**Arquivo atual:** `aru/tools/delegate_prompt.txt` (81 linhas, 8 seções).

**Nova estrutura proposta** (~150-180 linhas):

```
1. Launch a new sub-agent to handle complex, multi-step tasks. (≈ hoje)
2. {agent_list}
3. ## When to use this tool
4. ## When NOT to use this tool   ← expandir com o gate do coordinator
5. ## Phases framework            ← NOVO
6. ## Writing the prompt          ← ampliar com "never delegate understanding" + bad examples
7. ## Continue vs Spawn           ← NOVO (matriz)
8. ## Concurrency                 ← refinar por tipo
9. ## Verification                ← NOVO
10. ## While agents run            ← NOVO (don't peek / don't race)
11. ## Resume mechanics           (≈ hoje)
```

**Seções-chave (conteúdo):**

**#4 — When NOT to use** (expandir os bullets atuais com):
```
- If you can answer from context or one Read/Grep call, do it directly — don't
  spawn a sub-agent for what you can handle without tools.
- Do not use sub-agents to trivially report file contents or run a single
  command. Give them higher-level tasks (research, implementation, verification).
- One-line factual questions ("what's in README?") — just read it.
```

**#5 — Phases framework** (novo):
```
Most complex tasks break into four phases. Small tasks skip phases freely.

| Phase          | Who          | Purpose                                  |
|----------------|--------------|------------------------------------------|
| Research       | Sub-agents   | Map codebase, find files, understand     |
| Synthesis      | YOU          | Read findings, craft the implementation  |
|                |              | spec. NEVER delegate this step.          |
| Implementation | Sub-agents   | Make changes per your spec               |
| Verification   | Sub-agents   | Prove the change works                   |

A single-agent task skips Research (goes straight to Implementation) when the
scope is small and localised. A zero-agent task skips everything (you answer
directly). Choose the minimum.
```

**#6 — Writing the prompt** (ampliar com):
```
**Never delegate understanding.** Phrases like "based on your findings, fix
the bug" or "apply the research" push synthesis onto the worker. You must
read the findings, identify the approach, and hand the worker a spec with
file paths, line numbers, and exactly what to change.

### Bad examples
- "Fix the bug we discussed" — worker can't see your conversation
- "Based on your findings, implement the fix" — lazy delegation
- "Create a PR for the recent changes" — ambiguous scope
- "Something went wrong with the tests, can you look?" — no error, no path
```

**#7 — Continue vs Spawn** (novo; baseado em `coordinatorMode.ts:284-291`):
```
Each delegate_task returns a task_id. Use it with task_id=... to resume the
same sub-agent (full context preserved). Choose by context overlap:

| Situation                                        | Decision |
|--------------------------------------------------|----------|
| Research explored the files that need editing   | Continue |
| Research was broad, implementation is narrow    | Spawn    |
| Correcting a failure from the same worker       | Continue |
| Verifying code a different worker just wrote    | Spawn    |
| Previous attempt used the wrong approach        | Spawn    |
| Completely unrelated task                       | Spawn    |

No universal default. Think about what the existing context buys you.
```

**#8 — Concurrency** (refinar):
```
Launch multiple delegate_task calls in a single message when subtasks are
independent. Calibrate by task type:

- Read-only (research): parallelize freely, cover multiple angles
- Write-heavy (implementation): one worker at a time per set of files
- Verification: can overlap with implementation on different file areas
```

**#9 — Verification** (novo):
```
Verification means proving the code works, not confirming it exists.

- Run tests with the new feature enabled, not just "tests pass"
- Run typechecks and investigate errors — don't dismiss as "unrelated"
- Test edge cases and error paths, not just the happy path
- Be skeptical — if something looks off, dig in
```

**#10 — While agents run** (novo; baseado em `prompt.ts:91-93`):
```
After launching a sub-agent, you know nothing about what it found until it
returns. Do not fabricate, predict, or summarize results before the task
notification arrives.

If the user asks a follow-up mid-flight, report status ("still running"),
not a guess. The result is delivered as a user-role message in a later
turn — never something you write yourself.
```

**Testes:**

Prompt é texto; não há testes unitários diretos. Validação é via #Q. Único check programático:
```python
def test_delegate_prompt_renders():
    from aru.tools.delegate import _render_delegate_prompt
    out = _render_delegate_prompt()
    assert "{agent_list}" not in out  # format-string resolvido
    assert "Phases" in out
    assert "Continue vs Spawn" in out
    assert len(out) < 8000  # sanity — cabe no tool description budget
```

**Arquivos:** `aru/tools/delegate_prompt.txt`, `tests/test_delegate_prompt.py` (novo, 1 teste).

---

### #P — `AgentSpec` `orchestrator`

**Esforço:** 1h · **Prioridade:** média · **Depende de:** #O

**Goal:** criar um subagent especializado em orquestração — pegar tarefa complexa, decompor, delegar aos specialists existentes (explorer / verifier / reviewer / guide / executor), synthesizar, reportar.

**Por que existir:** o primary agent já orquestra, mas (a) seu system prompt precisa caber em tamanho razoável para tarefas de todo tipo, (b) orquestração consome contexto em decomposição que polui o raciocínio sobre o problema. Ter um `orchestrator` spec permite `delegate_task(task=X, agent_name="orchestrator")` quando a tarefa é grande — o primary preserva contexto, o orchestrator paga o overhead.

**Estrutura:**

```python
# aru/agents/catalog.py  (adicionar)
ORCHESTRATOR_EXTRA = """
You are an orchestrator sub-agent. Your job: take a complex task, decompose
it into phases, delegate to specialists, synthesize their results, and
return a single consolidated report.

## Your tools
- delegate_task — spawn specialists (explorer, verifier, reviewer, guide, executor)
- read_file / grep_search / glob_search — use sparingly, only when delegation
  would be overkill
- todo_write — track phases for multi-step tasks

## Decision rules
1. Can you answer from the task description alone? Answer directly, no delegation.
2. Is the task a single Read or Grep? Do it yourself.
3. Does it require mapping the codebase? Delegate to `explorer` (parallel if
   multiple angles).
4. After research, YOU synthesize. Never tell the next worker "based on the
   research" — craft a spec with file paths and line numbers.
5. Is the scope localised (1-3 files)? One `executor` worker with a precise
   spec. No research phase needed.
6. Is the scope large (multi-module)? Research → Synthesis (you) → Executor
   → Verifier in sequence.

## Anti-patterns
- Spawning a worker to read one file
- Spawning 5 workers "in parallel" when 2 would cover the same angles
- Passing worker output directly to the next worker without synthesis
- Fabricating worker results mid-flight

## Output contract
Return a single report with:
- What you delegated and why (1-2 sentences per worker)
- Consolidated findings / changes made
- Confidence level + what you did NOT verify
"""

AGENTS["orchestrator"] = AgentSpec(
    name="orchestrator",
    role="orchestrator",
    mode="subagent",
    tools_factory=lambda: [
        delegate_task, read_file, grep_search, glob_search, todo_write,
    ],
    max_tokens=8192,       # needs room for synthesis
    small_model=False,     # orchestration needs a capable model
    use_reasoning=True,    # decomposition benefits from reasoning
    can_delegate=True,     # destravado por #I
    description=(
        "Orchestrator sub-agent for complex multi-phase tasks. Decomposes the "
        "problem, delegates to specialists, synthesizes results. Use when the "
        "task spans multiple modules or requires both research and "
        "implementation. For localised tasks, delegate directly to executor."
    ),
    extra_instructions=ORCHESTRATOR_EXTRA,
)
```

**Teste:**
```python
def test_orchestrator_in_catalog():
    from aru.agents.catalog import AGENTS
    spec = AGENTS["orchestrator"]
    assert spec.can_delegate is True
    assert spec.mode == "subagent"
    tools = spec.tools_factory()
    tool_names = {t.__name__ for t in tools}
    assert "delegate_task" in tool_names

def test_orchestrator_description_mentions_use_case():
    spec = AGENTS["orchestrator"]
    desc = spec.description.lower()
    assert "complex" in desc or "multi" in desc
    assert "decomposes" in desc or "synthesiz" in desc
```

**Arquivos:** `aru/agents/catalog.py`, `tests/test_catalog.py` (estender).

---

### #Q — Smoke test em tarefa real

**Esforço:** 0.5-1h · **Prioridade:** alta · **Depende de:** #P

**Goal:** validar que o orquestrador de fato decide melhor depois de #O+#P. Não é teste automatizado — é usar em trabalho real e observar.

**Protocolo:**

1. Escolher 3 tarefas representativas do seu uso real:
   - **Pequena** ("atualize a versão no pyproject.toml") — expectativa: primary resolve direto, zero delegação.
   - **Média localizada** (fix focado em 1-3 arquivos) — expectativa: 1 delegação a `executor` com spec precisa. Sem research phase.
   - **Grande** (feature cross-module ou refactor) — expectativa: `delegate_task(..., agent_name="orchestrator")` que por sua vez delega a explorer → synthesiza → executor → verifier.

2. Rodar cada uma e verificar:
   - ✅ Tarefa pequena: zero `delegate_task` calls
   - ✅ Tarefa média: 1 call direto a `executor`, prompt contém file:line
   - ✅ Tarefa grande: orchestrator usado, synthesis feita pelo orchestrator (não pelo último worker), relato final consolidado

3. Registrar observações em `docs/aru/plans/2026-04-19-orchestrator-validation.md` — não pra mudar nota, pra ter base pra iteração.

**O que vai dar errado (expectativa honesta):**
- Primary pode continuar delegando demais em tarefas pequenas se o "answer directly" não estiver forte o suficiente. Pode precisar ajuste.
- Orchestrator pode synthesizar de forma rasa ("based on the findings...") — anti-padrão. Fortalecer anti-exemplo.
- Worker retorna sumário que o orchestrator passa adiante sem re-synthesizar. Sinal de que a seção de synthesis precisa ficar mais explícita.

Iteração curta esperada: 1-2 ciclos de ajuste no prompt após o primeiro uso real.

---

## 4. Ordem e paradas

Sequencial — #O → #P → #Q. Não paraleliza.

**Parada natural após #Q:** se os 3 smoke tests comportam como esperado, congelar. Não partir para modo coordinator global sem sinal de que a forma atual incomoda.

**Se #Q revelar comportamento ruim:** iterar no prompt (1-2h adicionais). Não adicionar infra nova.

---

## 5. Não-objetivos (explícitos)

1. **Modo coordinator de sessão** (análogo a `CLAUDE_CODE_COORDINATOR_MODE`). Justifica só se, após uso real, o padrão `delegate_task(..., agent_name="orchestrator")` ficar recorrente o suficiente pra valer um modo. Hoje não há sinal.

2. **Novo subagent-type "worker"** (como CC). O Aru já tem specialists tipados (explorer/verifier/reviewer/guide/executor). Acrescentar um "worker" genérico seria regressão, não progresso.

3. **Mudanças em `delegate.py` lógica.** Infra atual cobre o necessário após #I. Mudança é só texto (prompt) + catálogo (AgentSpec).

4. **Subir nota de critério 7.** Melhora qualidade de orquestração **dentro** da capacidade atual, não adiciona capacidade. Gaps estruturais (SQLite/worktree/swarm) continuam abertos.

---

## 6. Riscos e mitigações

| Risco | Impacto | Mitigação |
|---|:-:|---|
| Prompt mais longo estoura budget de tool description | baixo | Teste de len() < 8KB; hoje está em ~3KB, sobra |
| Orchestrator delega recursivamente em loop | médio | MAX_SUBAGENT_DEPTH=5 (já posto em #I) |
| Anti-patterns listados não são internalizados | médio | #Q pega isso; iteração curta no prompt |
| Primary ignora "answer directly" e continua delegando | médio | Gate é só uma linha — reforçar com exemplo explícito se precisar |
| Orchestrator synthesis é superficial | médio | Bad example + checklist no ORCHESTRATOR_EXTRA; ajustar após observar |

---

## 7. Métricas de sucesso (observáveis, não numéricas)

1. **Tarefa pequena**: primary resolve direto, sem chamar `delegate_task`. Hoje isso é inconsistente — às vezes delega read_file por hábito.

2. **Tarefa média**: uma delegação direta a `executor` com prompt contendo `file:line` e ação específica, sem research phase redundante.

3. **Tarefa grande**: `orchestrator` é usado, synthesis é feita por ele (não pelo último worker), relato final consolida informações sem "based on the findings".

4. **Nenhum caso de fabricação mid-flight**: se o usuário perguntar durante execução, resposta é "ainda rodando" e não um palpite.

---

## 8. Referências

### Claude Code
- `src/tools/AgentTool/prompt.ts:66-287` — getPrompt(), estrutura do prompt
- `src/tools/AgentTool/prompt.ts:91-93` — don't peek / don't race
- `src/tools/AgentTool/prompt.ts:99-113` — writing the prompt + anti-patterns
- `src/coordinator/coordinatorMode.ts:111-368` — system prompt completo do coordinator
- `src/coordinator/coordinatorMode.ts:124` — gate "answer directly"
- `src/coordinator/coordinatorMode.ts:137-138` — "don't trivially report"
- `src/coordinator/coordinatorMode.ts:198-228` — phases + concurrency
- `src/coordinator/coordinatorMode.ts:251-335` — writing worker prompts
- `src/coordinator/coordinatorMode.ts:283-291` — continue vs spawn matrix

### Aru (estado atual)
- `aru/tools/delegate_prompt.txt` — arquivo a reescrever
- `aru/tools/delegate.py` — infra que carrega o prompt; `_render_delegate_prompt()`
- `aru/agents/catalog.py` — onde o `AgentSpec` novo entra
- `docs/aru/plans/2026-04-19-multi-agent-tier2-orchestration.md` — plano pai (Tier 1 #I já feito)

### Decisões registradas
- **Não criar modo coordinator global:** validar primeiro que `orchestrator` como AgentSpec tem uso real
- **Specialists tipados ganham de "worker" genérico:** Aru já tem explorer/verifier/reviewer/guide
- **Prompt grows < 8KB:** orçamento confortável, sem risco de estourar tool description
- **Validação por uso real, não por benchmark:** #Q é protocolo, não teste automatizado
