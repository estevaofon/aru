---
title: Subagentes Customizados
description: Como criar e usar subagentes para paralelizar tarefas sem poluir o contexto principal
---

# Subagentes Customizados

**Subagentes** são agentes que rodam em segundo plano via `delegate_task`. Eles têm seu próprio prompt, modelo, ferramentas e janela de contexto — e retornam ao agente que os invocou apenas o resultado final.

Use subagentes para **isolar trabalho** e **proteger o contexto** do agente `build`.

!!! info "`explorer` é um subagent nativo"
    O Aru já vem com o subagent `explorer` registrado no catálogo (`aru/agents/catalog.py`). Ele é usado pelo `build` para pesquisa read-only de codebase via `delegate_task(task, agent_name="explorer")`. Você não precisa criar o seu — mas pode criar outros subagents customizados (reviewer, documenter, etc.) da mesma forma descrita abaixo.

## Quando usar

- **Pesquisas amplas** — encontrar todos os usos de uma função em um codebase grande
- **Tarefas paralelizáveis** — rodar revisões, lints e buscas ao mesmo tempo
- **Investigações que consumiriam muitos tokens** na conversa principal
- **Encapsular comportamentos especializados** — um "explorer", um "reviewer", um "documenter"

## Criando um subagente

Crie um arquivo em `.agents/agents/` com `mode: subagent` no frontmatter:

```markdown
---
name: explorer
description: Exploração read-only do codebase — encontra arquivos, analisa estrutura
mode: subagent
model: anthropic/claude-haiku-4-5
tools: read_file, grep_search, glob_search, list_directory
max_turns: 10
---

Você é um explorador de codebase. Sua tarefa é procurar e retornar informação,
nunca modificar arquivos. Seja conciso e objetivo — retorne apenas paths, linhas
e explicações curtas.
```

!!! tip "Use modelos rápidos para subagentes"
    Subagentes de exploração ou revisão costumam ser chamados em paralelo — um modelo Haiku entrega respostas rápidas sem sacrificar qualidade no escopo limitado da tarefa delegada.

## Como um subagente é invocado

Subagentes têm **três formas de serem chamados**:

### 1. Automática (o LLM decide)

Nome e descrição do subagente são injetados no tool description de `delegate_task`. O General Agent vê a descrição e chama sozinho quando a tarefa encaixa:

```python
delegate_task(
    task="encontre todos os usos de useMemo neste projeto",
    agent="explorer"
)
```

Isso é o principal diferencial — você não precisa lembrar de invocar o subagente, basta ter uma descrição clara e o LLM decide.

### 2. Manual via `@mention`

```text
aru> @explorer procure todas as chamadas de FastAPI em src/
```

Útil quando você quer explicitamente delegar sem esperar que o LLM principal decida.

### 3. Listagem

```text
aru> /agents
```

Mostra todos os agentes (primary e subagent) com nome, descrição, modelo e ferramentas.

## Primary vs. Subagent

|  | Primary | Subagent |
|---|---------|----------|
| Invocação via `/name` | ✅ | ❌ (bloqueado com warning) |
| Invocação via `@name` | ✅ | ✅ |
| Invocação via `delegate_task` | ❌ | ✅ (automática) |
| Aparece em `/agents` | ✅ | ✅ |
| Memória de permissão isolada | ✅ | ✅ |
| Contexto separado do General Agent | Não | Sim |

A regra mental: **primary** é um agente que o usuário chama diretamente. **Subagent** é uma capacidade que o LLM principal pode invocar sob demanda.

## Permissões

Subagentes respeitam o mesmo sistema de permissões, e podem sobrescrever regras globais:

```markdown
---
name: reviewer
description: Revisa código sem modificar nada
mode: subagent
permission:
  edit: deny
  write: deny
  bash:
    "git diff *": allow
    "git log *": allow
---
```

Isso garante que um subagente `reviewer` **nunca** consiga modificar arquivos, mesmo que o General Agent tenha autorização global para editar.

## Dicas de design

- **Descrições acionáveis.** "Faz exploração" é vago. "Encontra arquivos relevantes para uma query e retorna top-5 com paths" é acionável — o LLM principal sabe exatamente quando delegar.
- **max_turns baixo** (5–10) para subagentes focados. Evita que divirjam do objetivo ou gastem tokens desnecessários.
- **Restrinja `tools`** ao mínimo necessário. Um explorer não precisa de `write_file` nem `bash`.
- **Teste a delegação.** Depois de criar o subagente, rode `/agents` e peça algo no General Agent que deveria ativar a delegação — observe se o LLM chama `delegate_task` sozinho. Se não chamar, a descrição provavelmente está vaga demais.

## Exemplos prontos

### Explorer (exploração rápida)

```markdown
---
name: explorer
description: Find files and analyze structure. Returns paths and short explanations.
mode: subagent
model: anthropic/claude-haiku-4-5
tools: read_file, grep_search, glob_search, list_directory
max_turns: 10
---

Find and return information. Never modify files. Be terse.
```

### Reviewer (revisão crítica)

```markdown
---
name: reviewer
description: Reviews a diff or file for bugs, security issues, and readability problems.
mode: subagent
model: anthropic/claude-sonnet-4-6
tools: read_file, grep_search
max_turns: 8
permission:
  edit: deny
  write: deny
---

Você é um revisor sênior. Aponte bugs, problemas de segurança e leitura.
Nunca modifique arquivos.
```

### Documenter (geração de docs)

```markdown
---
name: documenter
description: Generates docstrings for a function or module. Returns updated source as a diff.
mode: subagent
model: anthropic/claude-sonnet-4-6
tools: read_file
max_turns: 5
---

Gere docstrings no estilo Google para funções sem documentação.
Retorne o código atualizado, mas não escreva no disco.
```
