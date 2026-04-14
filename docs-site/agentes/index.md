---
title: Agentes
description: Visão geral dos agentes nativos do Aru (catalog-driven)
---

# Agentes

O Aru é construído em torno de **agentes** — instâncias de LLM com prompt, conjunto de ferramentas e papel específicos. Os agentes nativos são declarados como `AgentSpec` em `aru/agents/catalog.py` e construídos por um factory único (`agent_factory.create_agent_from_spec`), que resolve o modelo, aplica hooks de plugin e anexa o compression manager.

## Agentes nativos

| Agente | Modo | Papel | Ferramentas |
|--------|------|-------|-------------|
| **`build`** (General) | primary | Conversa e execução direta. Invoca `enter_plan_mode` sozinho quando detecta tarefas multi-arquivo | Conjunto completo (write, edit, bash, delegate, plan/task tracking) |
| **`plan`** (Planner) | primary | Análise read-only → plano em Markdown (`## Summary` + `## Steps`) | Apenas read/search (`read_file`, `read_files`, `glob_search`, `grep_search`, `list_directory`) |
| **`executor`** | primary | Executa os passos de um plano armazenado, com tracking obrigatório de subtarefas | Conjunto completo |
| **`explorer`** | **subagent** | Pesquisa rápida e read-only em codebase. Invocado apenas via `delegate_task(task, agent_name="explorer")` | Read/search + `bash` read-only + `rank_files` |

!!! info "Explorer agora é subagent"
    O `explorer` não é mais invocável diretamente pela REPL. Ele é chamado pelo `build` via `delegate_task` quando a tarefa envolve explorar muitos arquivos — seu contexto isolado protege o contexto principal de ruído.

## `build` — agente geral

É o agente padrão da REPL. Resolve perguntas, edits pequenos e refatorações localizadas sem precisar de plano explícito. Tem acesso ao conjunto completo de ferramentas e segue duas heurísticas importantes:

- **Delega pesquisa ampla.** Para entender sistemas ou analisar vários arquivos, spawna múltiplos `delegate_task(..., agent_name="explorer")` em paralelo e sintetiza os retornos.
- **Entra em plan mode sozinho.** Para tarefas que exigem 3+ mudanças coordenadas, chama `enter_plan_mode(task)` antes de começar a editar.

## `plan` — planner

Ativado via `/plan <tarefa>` ou automaticamente pelo `build` através de `enter_plan_mode`. Lê o código em modo read-only e produz um plano estruturado:

```markdown
## Summary
- 1-3 bullets descrevendo a mudança

## Steps
- [ ] Step 1: [verbo] [o quê] em [arquivo] — [detalhe essencial]
- [ ] Step 2: ...
```

O plano é armazenado no estado da sessão (`session.plan_steps`) e reaparece a cada turno como um `<system-reminder>` com status (`○` pendente / `~` em progresso / `✓` concluído / `✗` falhou / `·` pulado).

### Reviewer (escopo)

`aru/agents/planner.py` expõe `review_plan(request, plan)` — um reviewer one-shot que roda no modelo pequeno, sem ferramentas, e **corta passos que ultrapassam o escopo pedido**. Preserva a redação dos passos mantidos. Ative com `plan_reviewer: true` no `aru.json`.

## `executor` — execução estruturada

Implementa os passos gerados pelo planner. Obriga o uso de duas ferramentas de tracking:

- **`create_task_list([...])`** — primeira chamada obrigatória. Declara 1–10 subtarefas concretas para o passo atual.
- **`update_task(index, status)`** — atualiza cada subtarefa (`in_progress` / `completed` / `failed`).
- **`update_plan_step(index, status)`** — marca o passo macro do plano como concluído antes de seguir para o próximo.

Isso mantém a execução disciplinada e visível: enquanto roda, um painel Rich atualiza o status das subtarefas no terminal.

## `explorer` — subagent read-only

Agente rápido e read-only para pesquisa de codebase. Como roda em contexto isolado, retorna apenas o resumo ao agente que o invocou. É registrado como `mode: subagent`, o que significa:

| Invocação | Disponível? |
|-----------|-------------|
| `/explorer ...` (slash) | ❌ (bloqueado com warning) |
| `@explorer ...` (mention) | ✅ |
| `delegate_task(..., agent_name="explorer")` | ✅ (uso padrão) |

Usa por padrão o modelo pequeno (`small_model_ref`), que costuma ser Haiku — o factory respeita `small_model=True` no spec.

## Agentes customizados

Fora do catálogo nativo, qualquer arquivo Markdown em `.agents/agents/` vira um agente customizado com seu próprio modelo, prompt e ferramentas. Eles seguem um caminho separado de instanciação (`create_custom_agent_instance`) e podem ser `primary` ou `subagent`.

## Próximo passo

- [Agentes Customizados](customizados.md) — Criar seus próprios agentes com `.agents/agents/`
- [Planejamento](planejamento.md) — O fluxo completo de plan mode (manual e autônomo)
- [Subagentes](subagentes.md) — Como registrar e invocar subagentes via `delegate_task`
