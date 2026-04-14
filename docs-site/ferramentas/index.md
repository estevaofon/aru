---
title: Ferramentas Integradas
description: As 17 ferramentas nativas disponíveis para os agentes do Aru
---

# Ferramentas Integradas

O Aru vem com 17 ferramentas nativas organizadas em cinco categorias. Cada ferramenta passa pelo sistema de [permissões](../configuracao/permissoes.md) antes de executar.

Os conjuntos de ferramentas são definidos em `aru/tools/codebase.py` e associados aos agentes via o catálogo (`aru/agents/catalog.py`):

| Conjunto | Agentes | O que contém |
|----------|---------|--------------|
| `GENERAL_TOOLS` | `build` | Todas — I/O, busca, shell, web, delegação, plan/task tracking |
| `EXECUTOR_TOOLS` | `executor` | Igual ao `GENERAL_TOOLS` |
| `PLANNER_TOOLS` | `plan` | Somente read/search (`read_file`, `read_files`, `glob_search`, `grep_search`, `list_directory`) |
| `EXPLORER_TOOLS` | `explorer` (subagent) | Read/search + `bash` read-only + `rank_files` |

## Operações de arquivo

### `read_file`

Lê arquivos com suporte a range de linhas e detecção de binários. Arquivos grandes retornam o primeiro chunk + um outline estrutural, para proteger o contexto.

**Parâmetros:** `file_path`, `start_line`, `end_line`, `max_size`

### `read_files`

Versão em batch do `read_file` — lê múltiplos arquivos em uma única chamada paralela. Essencial quando o agente precisa inspecionar vários arquivos relacionados sem pagar round-trips por cada um.

### `write_file`

Escreve conteúdo em um arquivo, criando diretórios conforme necessário. Sempre passa pelo gate de permissão `write`.

**Parâmetros:** `file_path`, `content`

### `write_files`

Escreve múltiplos arquivos de uma vez. Útil quando o executor cria um conjunto coeso de arquivos novos (ex: modelos + rotas + testes) no mesmo passo.

### `edit_file`

Edição find-and-replace em arquivos existentes. Falha se o texto a ser substituído não for único, o que força o agente a fornecer contexto suficiente.

**Parâmetros:** `file_path`, `old_string`, `new_string`, `replace_all`

### `edit_files`

Aplica múltiplas edições find-and-replace em vários arquivos em uma única chamada. Reduz tokens e round-trips quando uma mesma mudança precisa ser propagada.

## Busca e descoberta

### `glob_search`

Encontra arquivos por padrão glob, respeitando `.gitignore`. Resultados ordenados por data de modificação.

**Exemplos:** `src/**/*.ts`, `tests/test_*.py`

### `grep_search`

Busca por conteúdo em arquivos usando ripgrep. Suporta regex, filtros por tipo de arquivo e modos de output (linhas, só nomes, contagem). Parâmetro `context_lines` controla quantas linhas de contexto são retornadas ao redor de cada match.

### `list_directory`

Lista o conteúdo de um diretório, filtrado pelo `.gitignore`. Útil pra o agente entender a estrutura antes de mergulhar em arquivos específicos.

### `rank_files`

Ranqueia arquivos por relevância usando um score multi-fator:

```text
score = 0.50 × name_match + 0.30 × structural + 0.20 × recency
```

Disponível apenas para o **subagent `explorer`** — é a forma dele devolver uma lista curta de arquivos prováveis para o agente que o invocou.

## Shell e web

### `bash`

Executa comandos shell com gate de permissão. Output truncado a 10 KB. Prefixos seguros (leitura, git status, grep, pytest, etc.) são auto-permitidos por padrão.

### `web_search`

Busca na web via DuckDuckGo. Retorna título, URL e snippet dos resultados.

### `web_fetch`

Baixa uma URL e converte o HTML em texto legível. Útil para ler documentação, issues do GitHub, blog posts, etc.

## Planejamento e tracking

Estas ferramentas materializam o fluxo de plan mode descrito em [Planejamento](../agentes/planejamento.md).

### `enter_plan_mode`

Gera um plano estruturado via o agente `plan` e o armazena na sessão. O próprio `build` chama essa tool sozinho quando detecta uma tarefa que exige 3+ mudanças coordenadas.

**Parâmetros:** `task` (descrição de uma linha)

Retorna um resumo dizendo quantos passos foram armazenados — no turno seguinte, um `<system-reminder>` `PLAN ACTIVE` aparece no contexto do agente.

### `update_plan_step`

Marca um passo macro do plano ativo como `in_progress` / `completed` / `failed` / `skipped`. O `runner` atualiza o reminder automaticamente a cada turno, então o agente sempre vê o estado atual.

**Parâmetros:** `index`, `status`

### `create_task_list`

Primeira ferramenta obrigatória que o executor chama dentro de um passo: declara 1–10 subtarefas concretas.

**Parâmetros:** `tasks` (lista de strings)

### `update_task`

Atualiza o status de uma subtarefa (`in_progress` / `completed` / `failed`). Um painel Rich no terminal mostra o progresso em tempo real.

**Parâmetros:** `index`, `status`

## Delegação

### `delegate_task`

Cria sub-agentes autônomos para paralelizar trabalho. Cada sub-agente tem janela de contexto própria e retorna só o resultado final ao agente que o invocou.

Por padrão, delega para o subagent nativo `explorer` (read-only, rápido). Agentes customizados em modo `subagent` também são registrados aqui automaticamente e aparecem na descrição da tool, para que o LLM saiba quando invocá-los.

**Parâmetros:** `task`, `agent_name` (opcional, default `explorer`), `files` (opcional)

## Truncamento e limites

- **Shell output:** 10 KB (excesso é cortado)
- **Leitura de arquivo:** primeiro chunk + outline para arquivos grandes; `max_size=0` lê em chunks completos
- **Busca:** paginada por ripgrep, sem limite rígido
- **Task list:** máximo de 10 subtarefas por passo

## Estendendo

Você pode adicionar suas próprias ferramentas via:

- **[Ferramentas customizadas](customizadas.md)** — arquivos Python em `.aru/tools/`
- **[Plugins](../plugins/index.md)** — sistema completo de hooks com registro de tools
- **[MCP](../configuracao/mcp.md)** — servidores MCP externos
