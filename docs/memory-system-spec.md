# Memory System Specification

Spec reverso-engenheirado do sistema de memória persistente do Claude Code.
Objetivo: reproduzir a funcionalidade no Aru.

---

## 1. Visão Geral

O sistema de memória existe **exclusivamente para persistir informações entre sessões**. Dentro de uma mesma conversa o assistente já possui o histórico completo — memória não serve para isso. Memórias são arquivos `.md` em disco, carregados automaticamente no início de cada nova sessão futura.

### Quando é memória vs. quando não é

| Pergunta-chave | Sim → mecanismo | Não → mecanismo |
|---|---|---|
| Vou precisar disso na **próxima sessão**? | **Memory** (arquivos `.md` em disco) | Session history (já está no contexto) |
| É progresso de uma tarefa **em andamento agora**? | Tasks / Plans (escopos da conversa atual) | — |
| Posso derivar isso do **código, git ou docs**? | Não salvar — ler na hora | — |

**Regra prática:** se a informação morre junto com a conversa e isso é um problema, é memória. Se só importa agora, é task/plan/sessão.

### Princípios Centrais

> 1. Memória é para informações que **não podem ser derivadas** do código, git, ou docs existentes.
>    Se `git log`, `grep`, ou ler um arquivo responde a pergunta, **não salvar em memória**.
>
> 2. Memória é para informações que **precisam sobreviver ao fim da sessão**.
>    Se a informação só importa durante a conversa atual, usar tasks, plans ou o próprio histórico da sessão.

---

## 2. Estrutura de Armazenamento

### Diretório

```
~/.claude/projects/<project-hash>/memory/
```

Onde `<project-hash>` é o caminho do projeto com separadores substituídos por `--`.

Exemplo:
```
~/.claude/projects/D--OneDrive-Documentos-python_projects-aru/memory/
├── MEMORY.md          # Índice — sempre carregado no contexto
├── user_role.md       # Memória individual
├── feedback_testing.md
├── project_deadline.md
└── reference_jira.md
```

### Formato dos Arquivos de Memória

Cada memória é um arquivo `.md` com frontmatter YAML:

```markdown
---
name: Nome da memória
description: Descrição curta usada para decidir relevância em conversas futuras
type: user | feedback | project | reference
---

Conteúdo da memória aqui.

Para tipos feedback/project, estruturar como:
Regra ou fato principal.

**Why:** Motivação ou razão.

**How to apply:** Quando/onde aplicar esta informação.
```

### MEMORY.md — O Índice

- Arquivo especial que funciona como **índice** de todas as memórias
- **Sempre carregado** no contexto da conversa (injetado no system prompt)
- Não contém conteúdo de memória, apenas ponteiros
- Limite: ~200 linhas (truncado depois disso)
- Sem frontmatter

Formato:
```markdown
- [Título](arquivo.md) — descrição curta em uma linha (<150 chars)
- [User Role](user_role.md) — data scientist focused on observability
- [Testing Feedback](feedback_testing.md) — integration tests must hit real DB
```

---

## 3. Tipos de Memória

### 3.1 `user` — Perfil do Usuário

**O que armazena:** Papel, goals, responsabilidades, nível de conhecimento, preferências de comunicação.

**Quando salvar:** Ao aprender detalhes sobre quem é o usuário — cargo, expertise, áreas de foco.

**Como usar:** Adaptar respostas ao perfil. Um engenheiro senior recebe explicações diferentes de um estudante.

**Exemplo de trigger:**
```
User: "Sou data scientist investigando o logging que temos"
→ Salva: user é data scientist, foco atual em observability/logging
```

### 3.2 `feedback` — Correções e Confirmações

**O que armazena:** Orientações do usuário sobre como trabalhar — tanto correções ("não faça X") quanto confirmações de abordagem ("sim, exatamente isso").

**Quando salvar:**
- Correções explícitas: "não faça isso", "pare de fazer X"
- Confirmações de abordagens não-óbvias: "perfeito", "exatamente", aceitar sem pushback

**Estrutura obrigatória:**
```markdown
Regra: [o que fazer/não fazer]

**Why:** [razão que o usuário deu — incidente, preferência forte]

**How to apply:** [quando/onde essa orientação se aplica]
```

**Exemplo:**
```
User: "não mocke o banco nesses testes — queimamos trimestre passado quando mocks passaram mas a migration falhou em prod"
→ Salva: testes de integração devem usar banco real.
  Why: incidente onde mock/prod divergiram e esconderam migration quebrada.
  How to apply: qualquer teste que toque persistence layer.
```

### 3.3 `project` — Contexto do Projeto

**O que armazena:** Informações sobre trabalho em andamento, decisões, deadlines, iniciativas — coisas que **não estão no código nem no git**.

**Quando salvar:** Ao aprender quem faz o quê, por quê, ou até quando. Converter datas relativas para absolutas ("quinta" → "2026-04-16").

**Estrutura obrigatória:**
```markdown
Fato ou decisão.

**Why:** [motivação — constraint, deadline, pedido de stakeholder]

**How to apply:** [como isso deve influenciar sugestões]
```

**Exemplo:**
```
User: "estamos congelando merges não-críticos depois de quinta — mobile está cortando release branch"
→ Salva: merge freeze a partir de 2026-04-16 para release cut do mobile.
  Why: release branch do mobile.
  How to apply: alertar sobre PRs não-críticos após essa data.
```

### 3.4 `reference` — Ponteiros Externos

**O que armazena:** Onde encontrar informações em sistemas externos (Jira, Slack, Grafana, Linear, etc).

**Quando salvar:** Ao descobrir recursos externos e seu propósito.

**Exemplo:**
```
User: "bugs do pipeline são rastreados no Linear projeto INGEST"
→ Salva: bugs de pipeline estão no Linear project "INGEST"
```

---

## 4. Ciclo de Vida

### 4.1 Salvamento (2 passos obrigatórios)

**Passo 1:** Escrever o arquivo de memória com frontmatter:
```
memory/feedback_testing.md
```

**Passo 2:** Adicionar entrada no `MEMORY.md`:
```
- [Testing Feedback](feedback_testing.md) — integration tests must hit real DB, no mocks
```

Ambos os passos são necessários. Sem a entrada no índice, a memória existe mas não é descoberta.

### 4.2 Carregamento

Na inicialização de cada conversa:
1. O sistema carrega `MEMORY.md` inteiro no contexto (system prompt injection)
2. O assistente lê o índice e decide quais memórias individuais são relevantes
3. Memórias individuais são lidas sob demanda via `Read` tool

### 4.3 Atualização

- Antes de criar nova memória, verificar se já existe uma similar → atualizar em vez de duplicar
- Manter `name`, `description`, `type` do frontmatter sincronizados com o conteúdo
- Remover memórias obsoletas ou incorretas

### 4.4 Validação antes de uso

Memórias podem ficar stale. Antes de agir baseado em memória:

| Memória menciona... | Validar com... |
|---|---|
| Caminho de arquivo | Verificar se existe |
| Função ou flag | Grep no código |
| Estado do repo | `git log` ou ler código atual |

> "A memória diz que X existe" ≠ "X existe agora"

---

## 5. O que NÃO Salvar

Estas categorias são **explicitamente excluídas**, mesmo se o usuário pedir:

| Categoria | Razão | Alternativa |
|---|---|---|
| Padrões de código, convenções, arquitetura | Derivável do código atual | Ler o código |
| Estrutura de arquivos, paths | Derivável do filesystem | `glob`, `ls` |
| Git history, quem mudou o quê | Derivável do git | `git log`, `git blame` |
| Soluções de debugging, receitas de fix | O fix está no código, contexto no commit | Ler o diff |
| Coisas já documentadas em CLAUDE.md/AGENTS.md | Redundante | Ler o arquivo |
| Detalhes de tarefa ephemeral, estado temporário | Só útil na conversa atual | Usar tasks/planos |

Se o usuário pedir para salvar algo nessas categorias (ex: "salve o resumo de atividade do PR"), perguntar **o que foi surpreendente ou não-óbvio** — essa parte vale salvar.

---

## 6. Triggers de Salvamento

### Salvamento Explícito
- Usuário diz: "lembre disso", "salve isso", "memorize"
- → Salvar imediatamente no tipo mais adequado

### Salvamento Implícito (o assistente decide)
- Aprender detalhes sobre o usuário → `user`
- Receber correção OU confirmação de abordagem não-óbvia → `feedback`
- Aprender sobre trabalho em andamento, decisões, deadlines → `project`
- Descobrir recursos externos e seu propósito → `reference`

### Quando Esquecer
- Usuário diz: "esqueça isso", "remova essa memória"
- → Encontrar e deletar o arquivo + remover entrada do MEMORY.md

---

## 7. Regras de Uso em Contexto

1. **Carregar quando relevante** — não ler todas as memórias em toda conversa
2. **Obrigatório carregar** quando usuário explicitamente pede para "lembrar", "checar", "recall"
3. **Ignorar memória** quando usuário diz "ignore memory", "não use memória"
4. **Verificar antes de agir** — memória é snapshot, pode estar stale
5. **Preferir estado atual** — se memória conflita com código/git atual, confiar no que se observa agora e atualizar a memória

---

## 8. Diferenciação de Mecanismos de Persistência

| Mecanismo | Escopo | Sobrevive à sessão? | Quando usar |
|---|---|---|---|
| **Memory** | Entre sessões | **Sim** — arquivos em disco | Perfil do usuário, preferências, contexto de projeto não-derivável, ponteiros externos |
| **Tasks** | Conversa atual | Não | Quebrar trabalho em steps, tracking de progresso dentro da sessão |
| **Plans** | Conversa atual | Não | Alinhar abordagem de implementação com o usuário antes de executar |
| **Session history** | Conversa atual | Não (ou parcial via `.aru/sessions/`) | Contexto automático — tudo que foi dito e feito nesta conversa |

### Teste decisivo

> **"Se eu fechar o terminal agora e abrir uma nova sessão amanhã, vou precisar dessa informação?"**
>
> - **Sim** → Memory (salvar em arquivo `.md`)
> - **Não** → Tasks, plans, ou simplesmente o histórico da sessão atual
>
> Exemplos:
> - "O usuário prefere PRs únicos para refactors" → **Memory** (vale para sempre)
> - "Estou no passo 3 de 5 do plano" → **Task** (só importa agora)
> - "Vou usar a abordagem X para implementar o feature Y" → **Plan** (só importa agora)
> - "O usuário acabou de dizer que o arquivo está em src/foo.py" → **Nada** (já está no histórico)

---

## 9. Implementação para o Aru

### 9.1 Diretório de Memória

```python
# Sugestão de estrutura
MEMORY_DIR = Path.home() / ".aru" / "projects" / project_hash / "memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
```

O `project_hash` pode ser derivado do `cwd` (substituir separadores por `--`).

### 9.2 Componentes Necessários

```
aru/
├── memory/
│   ├── __init__.py       # Public API: MemoryStore
│   ├── store.py          # CRUD de memórias (read, write, update, delete)
│   ├── index.py          # Gerenciamento do MEMORY.md (parse, add, remove entries)
│   ├── loader.py         # Carregamento no contexto (system prompt injection)
│   └── types.py          # MemoryType enum, MemoryEntry dataclass
```

### 9.3 API Mínima

```python
class MemoryType(Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"

@dataclass
class MemoryEntry:
    name: str
    description: str
    type: MemoryType
    content: str
    filename: str  # slug derivado do name

class MemoryStore:
    def __init__(self, project_dir: Path): ...

    def save(self, entry: MemoryEntry) -> Path:
        """Salva memória + atualiza índice. Passo 1 e 2 juntos."""

    def load_index(self) -> list[MemoryEntry]:
        """Parse MEMORY.md, retorna lista de entries (sem conteúdo)."""

    def read(self, filename: str) -> MemoryEntry:
        """Lê conteúdo completo de uma memória específica."""

    def update(self, filename: str, entry: MemoryEntry) -> Path:
        """Atualiza memória existente + índice."""

    def delete(self, filename: str) -> None:
        """Remove arquivo + entrada do índice."""

    def find_similar(self, description: str) -> MemoryEntry | None:
        """Busca memória existente similar para evitar duplicatas."""

    def get_index_content(self) -> str:
        """Retorna conteúdo do MEMORY.md para injeção no system prompt."""
```

### 9.4 Injeção no System Prompt

No `agents/base.py`, ao montar as instruções do agente:

```python
memory_store = MemoryStore(project_dir)
index_content = memory_store.get_index_content()

if index_content:
    system_prompt += f"\n\n# Memory Index\n{index_content}"
```

### 9.5 Tools de Memória

Adicionar ao `tools/codebase.py`:

```python
def save_memory(name: str, description: str, type: str, content: str) -> str:
    """Salva uma memória persistente para uso em conversas futuras."""

def read_memory(filename: str) -> str:
    """Lê o conteúdo completo de uma memória específica."""

def delete_memory(filename: str) -> str:
    """Remove uma memória persistente."""

def list_memories() -> str:
    """Lista todas as memórias salvas (retorna o índice)."""
```

### 9.6 Instruções do Agente

Adicionar ao `BASE_INSTRUCTIONS` em `agents/base.py` um bloco explicando:
- Os 4 tipos de memória e quando usar cada um
- O que NÃO salvar
- Quando carregar memórias automaticamente
- Como validar memórias antes de agir

### 9.7 Frontmatter Parser

```python
import re
import yaml

def parse_memory_file(content: str) -> MemoryEntry:
    match = re.match(r'^---\n(.+?)\n---\n(.+)', content, re.DOTALL)
    if not match:
        raise ValueError("Invalid memory format")
    meta = yaml.safe_load(match.group(1))
    body = match.group(2).strip()
    return MemoryEntry(
        name=meta["name"],
        description=meta["description"],
        type=MemoryType(meta["type"]),
        content=body,
        filename=""  # set by caller
    )

def render_memory_file(entry: MemoryEntry) -> str:
    return f"""---
name: {entry.name}
description: {entry.description}
type: {entry.type.value}
---

{entry.content}
"""
```

---

## 10. Fluxo Completo

```
Conversa inicia
    │
    ├─ 1. Carregar MEMORY.md no system prompt
    │
    ├─ 2. Agente recebe mensagem do usuário
    │     │
    │     ├─ Usuário pede para lembrar algo?
    │     │   └─ save_memory() → arquivo .md + atualizar MEMORY.md
    │     │
    │     ├─ Usuário pede para esquecer algo?
    │     │   └─ delete_memory() → remover arquivo + entrada no MEMORY.md
    │     │
    │     ├─ Usuário dá feedback/correção?
    │     │   └─ Salvar como feedback (implícito)
    │     │
    │     ├─ Aprendi algo sobre o usuário?
    │     │   └─ Salvar como user (implícito)
    │     │
    │     ├─ Contexto de projeto não derivável do código?
    │     │   └─ Salvar como project (implícito)
    │     │
    │     ├─ Memória parece relevante para a tarefa?
    │     │   └─ read_memory() → carregar conteúdo completo
    │     │   └─ Validar que info ainda é atual antes de agir
    │     │
    │     └─ Usuário diz "ignore memory"?
    │         └─ Não usar, citar, ou mencionar memórias
    │
    └─ 3. Responder ao usuário
```

---

## 11. Edge Cases

- **MEMORY.md não existe:** Criar na primeira vez que salvar uma memória
- **Diretório memory/ não existe:** Criar automaticamente
- **Memória duplicada:** Verificar `find_similar()` antes de criar nova
- **Memória stale:** Sempre verificar estado atual antes de agir baseado em memória
- **MEMORY.md > 200 linhas:** Truncar, reorganizar, ou consolidar memórias antigas
- **Datas relativas:** Sempre converter para absolutas antes de salvar ("quinta" → "2026-04-16")
- **Conteúdo sensível:** Não salvar secrets, tokens, senhas em memórias
