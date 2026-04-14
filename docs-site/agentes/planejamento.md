---
title: Planejamento
description: Plan mode do Aru — manual via /plan ou autônomo via enter_plan_mode
---

# Planejamento

O fluxo de planejamento do Aru separa **raciocínio** de **execução**. O agente `plan` gera um plano estruturado em modo read-only, e o `build` / `executor` implementam cada passo com acesso total às ferramentas, marcando progresso conforme avança.

## Duas formas de ativar

### 1. Manual — `/plan <tarefa>`

```text
aru> /plan adicionar autenticação JWT ao endpoint /api/users
```

O planner roda, produz o plano, o reviewer opcionalmente corta escopo, e o resultado é armazenado na sessão.

### 2. Autônoma — `enter_plan_mode(task)`

O agente `build` chama essa ferramenta **sozinho** quando detecta uma tarefa que exige 3+ mudanças coordenadas em vários arquivos. Você não precisa digitar `/plan` — basta descrever o que quer:

```text
aru> migrar todo o módulo de auth para JWT, atualizando testes e docs
```

O `build` reconhece o escopo, invoca `enter_plan_mode(...)` internamente, o plano é gerado e armazenado, e na próxima rodada o próprio agente já começa a executá-lo.

!!! tip "Tarefas simples não entram em plan mode"
    Para 1–2 edits localizados, o `build` executa direto sem planejar. Plan mode existe para proteger tarefas grandes de serem feitas de forma atropelada.

## Ciclo de vida do plano

```text
Usuário pede algo grande
         ↓
build.enter_plan_mode(task)
         ↓
plan agent (read-only, Sonnet 4K)
  - Explora arquivos relevantes
  - Produz ## Summary + ## Steps
         ↓
(opcional) review_plan — corta scope creep
         ↓
session.set_plan(task, plan_content)
         ↓
<system-reminder> PLAN ACTIVE aparece a cada turno:
  ○ 1. Criar auth/jwt_middleware.py
  ○ 2. Adicionar JWT_SECRET em config.py
  ○ 3. Aplicar @require_jwt em users.py
  ○ 4. Adicionar testes em test_auth.py
         ↓
Para cada passo:
  1. create_task_list([...])           # 1-10 subtarefas do passo
  2. Executa as subtarefas
  3. update_task(i, "completed")       # por subtarefa
  4. update_plan_step(i, "completed")  # ao terminar o passo
         ↓
Quando todos os passos estão marcados,
o reminder some e o agente imprime o resumo final
```

## O system-reminder `PLAN ACTIVE`

Enquanto houver passos pendentes, o `runner` injeta automaticamente no prompt do agente um bloco:

```text
<system-reminder>
PLAN ACTIVE - 4 steps total (1 completed, 3 pending):
✓ 1. Criar auth/jwt_middleware.py
~ 2. Adicionar JWT_SECRET em config.py
○ 3. Aplicar @require_jwt em users.py
○ 4. Adicionar testes em test_auth.py
Execute steps in order. For each: optionally call create_task_list to break
the step into subtasks, do the work, then call update_plan_step(index,
'completed') to mark progress. Do NOT skip steps silently.
</system-reminder>
```

Isso faz o agente **ver o estado atual toda rodada** — mesmo em sessões compactadas, o plano persiste via `session.plan_steps`.

## Subtarefas (`create_task_list` / `update_task`)

Dentro de cada passo macro, o executor quebra o trabalho em 1–10 subtarefas antes de tocar em qualquer arquivo:

```text
create_task_list([
  "Read backend/auth/models.py",
  "Write backend/auth/jwt_middleware.py",
  "Edit backend/config.py — add JWT_SECRET",
  "Run pytest backend/tests/test_auth.py",
])
```

Cada `update_task(i, "completed")` atualiza um painel Rich no terminal com ícones coloridos (`○` / `~` / `✓` / `✗`). Essa disciplina força o agente a planejar antes de agir e dá visibilidade do progresso em tempo real.

### Por que obrigatório?

Sem a task list, modelos tendem a "improvisar" — lêem um arquivo, editam no escuro, lêem outro, fazem retrabalho. A task list força um mini-plano antes de cada passo e impede que o agente continue adicionando ações além do que foi declarado.

## Quando usar plan mode

Use (ou deixe o `build` usar) quando:

- A tarefa envolve **3+ arquivos** ou camadas
- Você quer **revisar a estratégia** antes de aplicar mudanças
- A mudança é **arquitetural** (migração, refatoração ampla, novo feature)
- Você precisa de um **registro** do raciocínio para depois

Para edits pequenos e localizados, o `build` resolve direto.

## Comandos úteis

```text
aru> /plan <tarefa>        # força entrada em plan mode
aru> descarte o plano      # pede ao build para limpar session.plan_steps
aru> o passo 3 tá vago     # pede iteração sobre o plano antes de executar
```

## Limites

- **Planner:** Sonnet, 4K tokens de output
- **Executor / build:** Sonnet, 8K tokens de output
- **Reviewer:** modelo pequeno (Haiku), 2K tokens, sem ferramentas

Se um passo individual for grande demais, o planner deve quebrá-lo em sub-passos. Se você perceber passos muito grandes, peça um plano mais granular — ou o reviewer acabará cortando.
