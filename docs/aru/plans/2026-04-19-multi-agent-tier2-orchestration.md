# Plano: Multi-agent Tier 2 — orquestração e persistência relacional

**Criado:** 2026-04-19
**Status:** Proposta
**Objetivo:** Subir o critério 7 (Multi-agente/Paralelismo) de **6.5 → 8.5+**, fechando os gaps apontados pela análise honesta da outra sessão. Completa o plano anterior (`2026-04-19-multi-agent-parity.md`) que entregou 5.5 → 6.5.

---

## 1. Contexto

Após implementar #A-#H do plano anterior (prompt, abort, built-ins, trace, permission, resume, persist-traces, background), Aru ficou em 6.5 no critério 7 — avaliação externa do outro modelo. Os 4 gaps ainda presentes, priorizados:

| Gap | Impacto na nota | Referência |
|---|:-:|---|
| Delegação single-hop (hardcoded no código) | alto | `delegate.py:32-33` + `registry.py` exclui `delegate_task` do `_SUBAGENT_TOOLS` |
| Sem hierarquia persistida relacional | alto | Traces em JSON sem queries; OpenCode tem `parentID` column em SQLite |
| Catálogo de agents estático | médio | `AGENTS` dict vs `Agent.list()` dinâmico do OpenCode |
| Sem worktree-per-agent | alto se edição paralela | CC tem `createAgentWorktree` automático |
| Sem swarm / teammate primitives | baixo em uso diário | CC tem `spawnMultiAgent`, `SendMessageTool`, `RemoteAgentTask` |

## 2. Tiers de entrega

| Tier | Delta nota 7 | Esforço | Itens |
|:-:|:-:|:-:|---|
| 1 | 6.5 → 7.5 (paridade OC) | ~12h | #I #J #K |
| 2 | 7.5 → 8.5 | +10h | #L |
| 3 | 8.5 → 9.0+ (paridade CC) | +25h | #M #N |

---

## 3. Items detalhados

### #I — Recursão configurável de subagents
**Esforço:** 2-3h · **Prioridade:** ALTA · **Depende de:** nada

**Goal:** permitir que subagents invoquem `delegate_task` quando o AgentSpec autoriza, ao invés de bloquear por código.

**Estado atual** (`aru/tools/delegate.py:32-33`):
```python
# Excludes delegate_task itself to prevent infinite recursion.
_SUBAGENT_TOOLS: list = []
```
`registry.py` popula `_SUBAGENT_TOOLS` com tudo menos `delegate_task`. Resultado: `explorer`/`verification`/`reviewer`/`guide` não podem delegar.

**Referência OpenCode** (`tool/task.ts:56-94`):
```typescript
const hasTaskPermission = agent.permission.some((rule) => rule.permission === "task")

const session = await Session.create({
  parentID: ctx.sessionID,
  permission: [
    ...(hasTaskPermission ? [] : [
      { permission: "task", pattern: "*", action: "deny" }
    ]),
    // ...
  ],
})

const result = await SessionPrompt.prompt({
  tools: {
    ...(hasTaskPermission ? {} : { task: false }),
  },
})
```
Agents sem permissão `task` recebem regra deny + toolset sem `task`. Agents com permissão `task` podem recursar.

**Implementação:**

1. **Adicionar `can_delegate` em `AgentSpec`** (`aru/agents/catalog.py`):
```python
@dataclass(frozen=True)
class AgentSpec:
    # ... existing fields
    can_delegate: bool = False  # If True, subagent gets delegate_task tool
```

2. **Mudar `_SUBAGENT_TOOLS` para ser construído por-chamada** (`aru/tools/registry.py`):
```python
def resolve_subagent_tools(spec: AgentSpec | None) -> list:
    """Build a subagent's toolset, including delegate_task when authorised.

    Replaces the module-level `_SUBAGENT_TOOLS` constant. Called per
    invocation so the decision is based on the spec, not a global flag.
    Mirrors opencode/tool/task.ts:56 — permission check gates recursion
    via YAML config, not code.
    """
    base = [t for t in ALL_TOOLS if t is not invoke_skill]  # existing exclusion
    if spec and spec.can_delegate:
        return base  # includes delegate_task
    return [t for t in base if t is not delegate_task]
```

3. **Em `delegate.py`** trocar `_SUBAGENT_TOOLS` por chamada ao novo resolver:
```python
# Built-in path
tools = resolve_subagent_tools(builtin_spec)
# Custom agent path
tools = resolve_subagent_tools(None) if not agent_def.tools else resolve_tools(agent_def.tools)
```

4. **Proteção contra recursão infinita via depth counter** (safety net):
```python
# ctx.subagent_depth (fork_ctx increments)
MAX_SUBAGENT_DEPTH = 5

if get_ctx().subagent_depth >= MAX_SUBAGENT_DEPTH:
    return f"[DELEGATE] Max subagent recursion depth ({MAX_SUBAGENT_DEPTH}) reached."
```

5. **Custom agent YAML** (parser já existe em `config.py`; mapear `can_delegate`):
```yaml
---
name: orchestrator
mode: subagent
can_delegate: true          # new field
description: ...
tools: [glob_search, grep_search, read_file, delegate_task]
---
```

**Testes:**
```python
def test_subagent_without_can_delegate_lacks_tool():
    spec = AGENTS["explorer"]  # can_delegate=False default
    tools = resolve_subagent_tools(spec)
    assert delegate_task not in tools

def test_subagent_with_can_delegate_gets_tool():
    spec = AgentSpec(..., can_delegate=True)
    tools = resolve_subagent_tools(spec)
    assert delegate_task in tools

def test_depth_limit_prevents_infinite_recursion():
    ctx = init_ctx()
    ctx.subagent_depth = 5
    result = await delegate_task("x", agent_name="explorer")
    assert "Max subagent recursion depth" in result
```

**Arquivos:** `aru/agents/catalog.py`, `aru/tools/registry.py`, `aru/tools/delegate.py`, `aru/runtime.py` (subagent_depth), `aru/config.py` (parse can_delegate from frontmatter), `tests/test_delegate_recursion.py`.

---

### #J — SQLite schema com parentID + queries hierárquicas
**Esforço:** 6-8h · **Prioridade:** ALTA · **Depende de:** nada (#G fica compatível)

**Goal:** substituir os JSONs em `.aru/sessions/<id>/subagents/<task_id>/` por tabelas SQLite com `parentID` como foreign key. Habilita `/subagents --tree` com navegação relacional, `/subagents --orphans`, e integridade referencial.

**Referência OpenCode** (`session/index.ts:125-167, 339, 448-458`):
```typescript
export const Info = z.object({
  id: SessionID.zod,
  parentID: SessionID.zod.optional(),  // <-- FK para sessão pai
  title: z.string(),
  time: z.object({ created, updated, archived }),
  permission: Permission.Ruleset.optional(),
})

const children = Effect.fn("Session.children")(function* (parentID: SessionID) {
  const rows = yield* db((d) =>
    d.select().from(SessionTable).where(and(
      eq(SessionTable.project_id, ctx.project.id),
      eq(SessionTable.parent_id, parentID)
    )).all(),
  )
  return rows.map(fromRow)
})
```

**Implementação:**

1. **Novo módulo `aru/subagent_store.py`** — wrapper em sqlite3 (stdlib, sem nova dependência):
```python
"""SQLite-backed sub-agent session hierarchy (OpenCode parity).

Replaces the JSON-per-task_id layout in .aru/sessions/<id>/subagents/
with a relational store. Enables tree queries, orphan detection,
cross-session references, and cleaner `/subagents` rendering.

Schema:
    subagent_sessions
        task_id TEXT PRIMARY KEY
        parent_task_id TEXT    NULL-allowed; FK self-reference to task_id
        primary_session_id TEXT NOT NULL  root session (for scoping)
        agent_name TEXT NOT NULL
        task TEXT              truncated 200 chars
        started_at REAL NOT NULL
        ended_at REAL NULL
        tokens_in INTEGER DEFAULT 0
        tokens_out INTEGER DEFAULT 0
        status TEXT NOT NULL   CHECK(status IN ('running','completed','cancelled','error'))
        result TEXT            truncated 500 chars
        tool_calls_json TEXT   JSON array of {tool, args_preview, duration}

Indexes:
    idx_parent   ON (parent_task_id)
    idx_session  ON (primary_session_id)
    idx_started  ON (started_at DESC)
"""

import sqlite3
from contextlib import contextmanager

_DB_PATH = os.path.join(os.path.expanduser("~"), ".aru", "subagents.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS subagent_sessions (
    task_id TEXT PRIMARY KEY,
    parent_task_id TEXT REFERENCES subagent_sessions(task_id) ON DELETE SET NULL,
    primary_session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    task TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('running','completed','cancelled','error')),
    result TEXT,
    tool_calls_json TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_parent ON subagent_sessions(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_session ON subagent_sessions(primary_session_id);
CREATE INDEX IF NOT EXISTS idx_started ON subagent_sessions(started_at DESC);
"""

@contextmanager
def _conn():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def upsert(trace: SubagentTrace, primary_session_id: str) -> None:
    """Idempotent write — repeated calls for the same task_id update in place.

    Called from delegate.py at two moments: when the subagent starts (status
    running, ended_at NULL) and when it completes. This gives /subagents a
    live view of running subagents, not just terminated ones.
    """
    with _conn() as conn:
        conn.execute("""
            INSERT INTO subagent_sessions
                (task_id, parent_task_id, primary_session_id, agent_name, task,
                 started_at, ended_at, tokens_in, tokens_out, status, result, tool_calls_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                ended_at=excluded.ended_at,
                tokens_in=excluded.tokens_in,
                tokens_out=excluded.tokens_out,
                status=excluded.status,
                result=excluded.result,
                tool_calls_json=excluded.tool_calls_json
        """, (trace.task_id, trace.parent_id, primary_session_id, trace.agent_name,
              trace.task, trace.started_at, trace.ended_at, trace.tokens_in,
              trace.tokens_out, trace.status, trace.result,
              json.dumps(trace.tool_calls)))

def children(parent_task_id: str) -> list[SubagentTrace]:
    """Direct descendants of a given task_id. Mirrors OC Session.children()."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subagent_sessions WHERE parent_task_id = ? ORDER BY started_at",
            (parent_task_id,)
        ).fetchall()
        return [_row_to_trace(r) for r in rows]

def descendants(root_task_id: str) -> list[SubagentTrace]:
    """Recursive CTE — all transitive children. For /subagents tree rendering."""
    with _conn() as conn:
        rows = conn.execute("""
            WITH RECURSIVE tree(task_id) AS (
                SELECT task_id FROM subagent_sessions WHERE task_id = ?
                UNION ALL
                SELECT s.task_id FROM subagent_sessions s
                  JOIN tree t ON s.parent_task_id = t.task_id
            )
            SELECT s.* FROM subagent_sessions s JOIN tree t ON s.task_id = t.task_id
            ORDER BY s.started_at
        """, (root_task_id,)).fetchall()
        return [_row_to_trace(r) for r in rows]

def for_session(primary_session_id: str) -> list[SubagentTrace]:
    """All sub-agents spawned (directly or transitively) from one session."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subagent_sessions WHERE primary_session_id = ? ORDER BY started_at",
            (primary_session_id,)
        ).fetchall()
        return [_row_to_trace(r) for r in rows]

def orphans() -> list[SubagentTrace]:
    """Running/idle subagents whose primary session is no longer active —
    useful for GC: /subagents --orphans followed by cleanup."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subagent_sessions WHERE status = 'running' AND ended_at IS NULL"
        ).fetchall()
        return [_row_to_trace(r) for r in rows]
```

2. **Substituir `_persist_trace_async`** em `delegate.py` — call `subagent_store.upsert(trace, session.session_id)` no início (status=running) e ao completar.

3. **Atualizar comandos `/subagents` e `/subagent`** em `commands.py` — ler do store via `for_session(session.session_id)` ao invés de `session.subagent_traces` e filesystem.

4. **Novos comandos:**
   - `/subagents --tree <task_id>` → `descendants(task_id)`
   - `/subagents --orphans` → `orphans()`
   - `/subagents --gc` → remove orphans + registros >30 dias

5. **Migração** — ler JSONs existentes em `.aru/sessions/*/subagents/*/trace.json` uma vez e inserir no SQLite. Script em `aru/migrations/0001_subagent_store.py`.

**Testes:**
```python
def test_upsert_roundtrip():
    trace = SubagentTrace(task_id="t1", ...)
    subagent_store.upsert(trace, "sess1")
    loaded = subagent_store.for_session("sess1")
    assert loaded[0].task_id == "t1"

def test_children_query():
    subagent_store.upsert(trace_parent, "sess1")  # task_id="p"
    subagent_store.upsert(trace_child, "sess1")   # parent_task_id="p"
    kids = subagent_store.children("p")
    assert len(kids) == 1
    assert kids[0].task_id == "c"

def test_descendants_recursive():
    # p → c1 → gc1, p → c2
    traces = [...]
    for t in traces: subagent_store.upsert(t, "sess1")
    assert len(subagent_store.descendants("p")) == 4  # p + c1 + gc1 + c2

def test_fk_self_reference_integrity():
    # Inserting child before parent should fail FK with PRAGMA foreign_keys=ON
    with pytest.raises(sqlite3.IntegrityError):
        subagent_store.upsert(trace_child_nonexistent_parent, "sess1")

def test_idempotent_upsert_on_status_change():
    subagent_store.upsert(running_trace, "sess1")
    completed = dataclasses.replace(running_trace, status="completed", ended_at=2.0)
    subagent_store.upsert(completed, "sess1")
    loaded = subagent_store.for_session("sess1")[0]
    assert loaded.status == "completed"
    assert loaded.ended_at == 2.0
```

**Arquivos:** `aru/subagent_store.py` (novo), `aru/tools/delegate.py`, `aru/commands.py`, `aru/migrations/0001_subagent_store.py` (novo), `tests/test_subagent_store.py` (novo).

**Back-compat:** a função `load_persisted_traces(session_id)` mantida como wrapper em cima de `subagent_store.for_session` pra não quebrar #G.

---

### #K — `Agent.list()` dinâmico (custom agents como cidadãos de primeira classe)
**Esforço:** 2-3h · **Prioridade:** média · **Depende de:** nada

**Goal:** substituir `AGENTS: dict[str, AgentSpec]` estático por função que merge defaults + `config.agent.*` + descobertas em runtime.

**Estado atual** (`aru/agents/catalog.py:60`):
```python
AGENTS: dict[str, AgentSpec] = {
    "build": ..., "plan": ..., "executor": ...,
    "explorer": ..., "verification": ..., "reviewer": ..., "guide": ...,
}
```
Catálogo hardcoded no módulo. Custom agents existem via `custom_agent_defs` em paralelo — dois caminhos separados em `delegate.py`.

**Referência OpenCode** (`agent/agent.ts:281-295`):
```typescript
const list = Effect.fnUntraced(function* () {
  const cfg = yield* config.get()
  return pipe(
    agents,  // merged: defaults + config.agent.*
    values(),
    sortBy(
      [(x) => (cfg.default_agent ? x.name === cfg.default_agent : x.name === "build"), "desc"],
      [(x) => x.name, "asc"],
    ),
  )
})
```

**Implementação:**

1. **Renomear `AGENTS` dict → `_BUILTIN_AGENTS`** (não-exportado).

2. **Nova função `list_agents(mode: str | None = None) -> list[AgentSpec]`**:
```python
def list_agents(mode: str | None = None) -> list[AgentSpec]:
    """Return the current agent catalogue — built-ins merged with custom defs.

    Parity with opencode/agent/agent.ts:281 (Agent.list). Sort order:
    default agent first (if configured), then alphabetical.

    Args:
        mode: filter to "primary" or "subagent"; None returns all.
    """
    merged: dict[str, AgentSpec] = dict(_BUILTIN_AGENTS)
    try:
        custom = get_ctx().custom_agent_defs or {}
    except LookupError:
        custom = {}
    for name, agent_def in custom.items():
        if name in merged:
            continue  # user file shadows built-in — skip (mirror OC behaviour)
        merged[name] = _agent_def_to_spec(agent_def)

    specs = list(merged.values())
    if mode:
        specs = [s for s in specs if s.mode == mode]

    default_name = _get_default_agent_name()  # from aru.json
    specs.sort(key=lambda s: (s.role != default_name, s.role))
    return specs


def get_agent(name: str) -> AgentSpec | None:
    """Lookup by name across built-in + custom. Returns None on miss."""
    for spec in list_agents():
        if spec.name.lower() == name.lower() or spec.role == name:
            return spec
    return None
```

3. **`_agent_def_to_spec`** converte `CustomAgentDef` (from .agents/agents/*.md) em `AgentSpec`:
```python
def _agent_def_to_spec(agent_def) -> AgentSpec:
    """Adapt a custom .md agent to the AgentSpec shape so list_agents()
    returns a homogeneous collection. tools_factory closes over the
    resolved tool list so late registration still works."""
    tools_list = agent_def.tools or None
    return AgentSpec(
        name=agent_def.name,
        role=agent_def.name,  # custom role = extra_instructions via agent_def.system_prompt
        mode=agent_def.mode,
        tools_factory=lambda td=tools_list: resolve_tools(td) if td else ALL_TOOLS,
        max_tokens=4096,
        small_model=False,
        use_reasoning=False,
        description=agent_def.description or "",
        extra_instructions=agent_def.system_prompt or "",
        can_delegate=bool(getattr(agent_def, "can_delegate", False)),
    )
```

4. **Refatorar `delegate.py`** — remover o branch `elif _agent_name in custom_agent_defs`, usar `get_agent(_agent_name)` unificado.

5. **Atualizar `_render_agent_list()`** em `delegate.py` — iterar `list_agents(mode="subagent")`.

**Testes:**
```python
def test_list_agents_includes_builtin():
    specs = list_agents()
    names = {s.role for s in specs}
    assert "explorer" in names
    assert "verification" in names

def test_list_agents_filter_by_mode():
    subs = list_agents(mode="subagent")
    assert all(s.mode == "subagent" for s in subs)
    assert "build" not in {s.role for s in subs}  # build is primary

def test_custom_agent_appears_in_list(monkeypatch):
    get_ctx().custom_agent_defs = {
        "my-custom": FakeAgentDef(description="custom", mode="subagent", ...)
    }
    specs = list_agents(mode="subagent")
    names = {s.role for s in specs}
    assert "my-custom" in names

def test_builtin_shadows_custom():
    get_ctx().custom_agent_defs = {"explorer": FakeAgentDef(...)}
    explorer = get_agent("explorer")
    # Built-in wins
    assert "read-only codebase" in explorer.description.lower()

def test_default_agent_sorts_first():
    # aru.json has "default_agent": "verification"
    set_config_with_default("verification")
    specs = list_agents()
    assert specs[0].role == "verification"
```

**Arquivos:** `aru/agents/catalog.py`, `aru/tools/delegate.py`, `tests/test_agent_catalog.py`.

**Nota:** testes existentes em `test_catalog.py` referenciam `AGENTS` diretamente — atualizar para `_BUILTIN_AGENTS` ou chamar `list_agents()`.

---

### #L — Git worktree per agent
**Esforço:** 8-12h · **Prioridade:** alta se edição paralela comum · **Depende de:** nada

**Goal:** habilitar edições paralelas de arquivo sem conflitos. Cada subagent pode rodar em seu próprio git worktree, isolado do pai até o merge.

**Referência Claude Code** (`utils/worktree.ts:902-952`):
```typescript
export async function createAgentWorktree(slug: string): Promise<{
  worktreePath: string
  worktreeBranch?: string
  headCommit?: string
  gitRoot?: string
  hookBased?: boolean
}> {
  validateWorktreeSlug(slug)
  if (hasWorktreeCreateHook()) {
    const hookResult = await executeWorktreeCreateHook(slug)
    return { worktreePath: hookResult.worktreePath, hookBased: true }
  }
  const gitRoot = findCanonicalGitRoot(getCwd())
  const { worktreePath, worktreeBranch, headCommit, existed } =
    await getOrCreateWorktree(gitRoot, slug)
  if (!existed) await performPostCreationSetup(gitRoot, worktreePath)
  return { worktreePath, worktreeBranch, headCommit, gitRoot }
}
```

Chave: NÃO toca no `currentWorktreeSession` global — é strictly para isolamento de subagent.

**Implementação:**

1. **Novo módulo `aru/worktree.py`** — wrappers sobre `git worktree`:
```python
"""Git worktree utilities for subagent isolation.

Each subagent can optionally run in its own worktree, branching from the
primary session's current HEAD. Edits are contained until the user
decides to merge. Mirrors claude-code's createAgentWorktree pattern
(utils/worktree.ts:902).

Storage layout:
    <git_root>/.aru/worktrees/<slug>/    ← worktree dir
    branch: aru-subagent/<slug>          ← isolated branch

Cleanup: worktrees older than 30 days are pruned by `/worktree gc`.
Explicit cleanup: `remove_agent_worktree(slug)` removes dir + branch.
"""

import subprocess
from pathlib import Path

def has_git_root(cwd: str) -> str | None:
    """Return git root if cwd is inside a git repo, else None."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=cwd, timeout=2,
        )
        return out.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def create_agent_worktree(slug: str, base_cwd: str | None = None) -> dict | None:
    """Create an isolated worktree for a subagent. Returns info dict or None
    when the cwd isn't a git repo (fail-soft — subagent runs in-place).

    slug: short identifier, validated to [a-z0-9-_] only. The subagent's
          task_id makes a good slug.
    """
    slug = _validate_slug(slug)
    cwd = base_cwd or os.getcwd()
    gitroot = has_git_root(cwd)
    if not gitroot:
        return None  # not a git repo — skip isolation, don't fail
    worktree_path = str(Path(gitroot) / ".aru" / "worktrees" / slug)
    branch = f"aru-subagent/{slug}"
    try:
        # git worktree add -b <branch> <path>
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, worktree_path],
            check=True, cwd=gitroot, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        # Branch might exist from prior failed run — try reattach
        try:
            subprocess.run(
                ["git", "worktree", "add", worktree_path, branch],
                check=True, cwd=gitroot, timeout=10,
            )
        except subprocess.CalledProcessError:
            return None
    return {"path": worktree_path, "branch": branch, "gitroot": gitroot}


def remove_agent_worktree(slug: str, delete_branch: bool = True) -> None:
    """Tear down worktree — dir + branch. Safe to call on already-removed."""
    slug = _validate_slug(slug)
    cwd = os.getcwd()
    gitroot = has_git_root(cwd)
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
    """Prune worktrees whose directory mtime is older than `days`.

    Returns count removed. Mirrors claude-code's stale-cleanup logic.
    """
    # ... iterate .aru/worktrees/, check mtime, remove old ones
```

2. **Opt-in via AgentSpec** — novo campo `use_worktree: bool = False`. Explorer/verification/reviewer/guide = False (são read-only, não precisam). Para custom agents mutating em paralelo, setar True.

3. **Em `delegate.py._run`**, criar worktree quando spec pede:
```python
worktree_info = None
if builtin_spec and builtin_spec.use_worktree:
    from aru.worktree import create_agent_worktree
    worktree_info = create_agent_worktree(f"sa-{agent_id}")

# If worktree created, chdir subagent's runtime to it
if worktree_info:
    # fork_ctx already happened; mutate fork's cwd context
    get_ctx().cwd = worktree_info["path"]
    # also chdir so tools that don't respect ctx.cwd land in the right place
    os.chdir(worktree_info["path"])

try:
    # ... existing execution
finally:
    if worktree_info:
        # Tool output header carries the worktree branch for the user to inspect
        trace.result = f"[worktree: {worktree_info['branch']}] {trace.result}"
```

**Importante:** `os.chdir` afeta o processo inteiro. Em asyncio com múltiplos subagents concorrentes, isso quebra. Soluções:
- Option A: serializar (sequential dispatch when use_worktree=True) — simples mas perde paralelismo
- Option B: pass `cwd` to every subprocess call instead of relying on process CWD — mais correto mas requer refactor de tools
- Option C: use `os.chdir` inside a lock + revert on exit (still serialises)

Escolha recomendada: **B** — tools (bash, read_file, write_file) já aceitam caminhos absolutos. Adicionar `cwd` contextvar e fazer tools prepend quando relativo. Trabalho adicional: ~3h sobre os 8-12h.

4. **Novos slash commands** `/worktree list`, `/worktree gc`, `/worktree merge <slug>` (apresenta diff + opta pelo merge).

**Testes:**
```python
def test_create_worktree_succeeds(tmp_git_repo):
    info = create_agent_worktree("test-slug", base_cwd=tmp_git_repo)
    assert os.path.isdir(info["path"])
    assert info["branch"] == "aru-subagent/test-slug"

def test_create_worktree_fails_soft_outside_git(tmp_non_git_dir):
    assert create_agent_worktree("x", base_cwd=tmp_non_git_dir) is None

def test_remove_cleans_dir_and_branch(tmp_git_repo):
    create_agent_worktree("rm-test", tmp_git_repo)
    remove_agent_worktree("rm-test")
    assert not os.path.isdir(Path(tmp_git_repo) / ".aru" / "worktrees" / "rm-test")

def test_slug_validation_rejects_path_traversal():
    with pytest.raises(ValueError):
        _validate_slug("../etc")

@pytest.mark.asyncio
async def test_subagent_with_worktree_writes_are_isolated(monkeypatch, tmp_git_repo):
    # Spec with use_worktree=True; subagent edits foo.py; parent foo.py unchanged
    ...
```

**Arquivos:** `aru/worktree.py` (novo), `aru/agents/catalog.py` (use_worktree field), `aru/tools/delegate.py`, `aru/tools/file_ops.py` (cwd-aware paths), `aru/commands.py` (/worktree), `tests/test_worktree.py` (novo).

---

### #M — Swarm orchestration (coordenação multi-agent)
**Esforço:** 10-15h · **Prioridade:** baixa · **Depende de:** #L

**Goal:** coordenar múltiplos subagents paralelos como grupo — permission polling, status aggregation, cascade cancel, broadcast.

**Referência Claude Code** (`hooks/useSwarmInitialization.ts:30`, `tools/shared/spawnMultiAgent.ts:305-539`, `hooks/useSwarmPermissionPoller.ts`):
- Swarm view = grupo de teammates registrados em `AppState.tasks`
- Coordination: permission poller, inbox poller, cascade cancel
- Spawn: split-pane (tmux/iterm) ou in-process

**Implementação para Aru (escopo reduzido — sem tmux):**

1. **Novo módulo `aru/swarm.py`**:
```python
"""In-process swarm coordination — group of subagents running in parallel.

Each swarm is identified by a swarm_id. Members are regular SubagentTraces
linked by swarm_id (new column). Coordination primitives:
- fan_out(tasks): launch N subagents, return when all complete
- broadcast(swarm_id, message): append user message to every live member
- cancel_swarm(swarm_id): abort_event.set() on the shared event
- swarm_status(swarm_id): aggregate running/completed/failed counts
"""

@dataclass
class Swarm:
    swarm_id: str
    created_at: float
    member_task_ids: list[str]
    shared_abort_event: threading.Event
    pending_messages: dict[str, list[str]]  # per-member inbox
```

2. **Nova tool `spawn_swarm(tasks: list[dict], agent_name: str)`** — convenience para fan-out:
```python
async def spawn_swarm(tasks: list[dict], agent_name: str = "explorer") -> str:
    """Launch a coordinated group of subagents running in parallel.

    Each task dict has keys: task (str), context (str, optional).
    Returns a swarm_id + aggregate summary when all complete.
    """
    swarm_id = f"swarm-{uuid.uuid4().hex[:8]}"
    swarm = Swarm(swarm_id=swarm_id, ...)
    _SWARMS[swarm_id] = swarm
    coros = [
        delegate_task(t["task"], context=t.get("context", ""),
                      agent_name=agent_name, _swarm_id=swarm_id)
        for t in tasks
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    return _format_swarm_report(swarm_id, results)
```

3. **Novos slash commands** `/swarm status <id>`, `/swarm cancel <id>`, `/swarm broadcast <id> <msg>`.

**Nota de escopo:** Claude Code suporta tmux panes e bridges remotos. Aru fica com in-process only — suficiente pra parity OpenCode (que também não tem) e 80% do valor prático de CC.

**Testes:**
```python
@pytest.mark.asyncio
async def test_fan_out_runs_in_parallel():
    t0 = time.monotonic()
    result = await spawn_swarm([
        {"task": "slow 1"},  # fake agent sleeps 0.1s
        {"task": "slow 2"},
        {"task": "slow 3"},
    ], agent_name="explorer")
    elapsed = time.monotonic() - t0
    # Parallel: ~0.1s, not 0.3s
    assert elapsed < 0.2

@pytest.mark.asyncio
async def test_cancel_swarm_stops_all_members():
    swarm_id = await spawn_swarm_nonblocking([...])
    await asyncio.sleep(0.05)
    cancel_swarm(swarm_id)
    # All members transitioned to status=cancelled
    ...
```

**Arquivos:** `aru/swarm.py` (novo), `aru/tools/delegate.py` (aceita `_swarm_id`), `aru/tools/registry.py` (expose spawn_swarm), `aru/commands.py` (/swarm), `tests/test_swarm.py` (novo).

---

### #N — Remote/teammate primitives (SendMessage entre agents)
**Esforço:** 15+h · **Prioridade:** baixa (edge cases) · **Depende de:** #J, #M

**Goal:** permitir comunicação assíncrona entre agents — subagent A pergunta pro subagent B, broadcast de mensagens, agendamento.

**Referência Claude Code** (`tools/SendMessageTool/SendMessageTool.ts:46-126`):
```typescript
const inputSchema = z.object({
  to: z.string()  // "teammate-name" | "*" | "uds:<path>" | "bridge:<session-id>"
    .describe('Recipient: teammate name, "*" for broadcast, ...'),
  summary: z.string().optional(),
  message: z.union([z.string(), StructuredMessage()]),
})
```

**Implementação (escopo reduzido — sem uds/bridge):**

1. **Nova tool `send_message(to: str, message: str)`** — disponível apenas em subagents com `can_delegate=True`:
```python
async def send_message(to: str, message: str) -> str:
    """Send a message to a sibling subagent or broadcast.

    `to`: task_id of target subagent, or "*" for broadcast to the same swarm.
    Delivery is async — target receives the message as a user-role prompt on
    its next turn. No synchronous reply channel.
    """
    swarm_id = _current_swarm_id()
    if not swarm_id:
        return "[SendMessage] Not in a swarm — no recipients."
    swarm = _SWARMS.get(swarm_id)
    if to == "*":
        for member_id in swarm.member_task_ids:
            if member_id != get_ctx().agent_id:
                swarm.pending_messages[member_id].append(message)
        return f"[SendMessage] Broadcast to {len(swarm.member_task_ids)-1} peers."
    # Direct
    if to in swarm.member_task_ids:
        swarm.pending_messages[to].append(message)
        return f"[SendMessage] Delivered to {to}."
    return f"[SendMessage] Unknown recipient: {to}"
```

2. **Subagent loop drena `pending_messages[agent_id]`** antes de cada turno — similar ao que #H faz com notificações de background.

3. **`ScheduleCronTool`** equivalente — provavelmente overkill para Aru. Pular.

**Testes:**
```python
@pytest.mark.asyncio
async def test_send_message_direct():
    # Spawn 2 subagents, one sends to the other, other's next turn receives it
    ...

@pytest.mark.asyncio
async def test_broadcast_excludes_sender():
    ...
```

**Arquivos:** `aru/tools/send_message.py` (novo), `aru/swarm.py` (expandir), `aru/tools/delegate.py` (drain inbox), `aru/tools/registry.py`, `tests/test_send_message.py` (novo).

---

## 4. Ordem de execução recomendada

Ordem por ROI prático:

| # | Item | ROI | Horas | Nota após |
|:-:|---|:-:|:-:|:-:|
| 1 | #I Recursão configurável | ⭐⭐⭐⭐⭐ | 2-3 | 6.5 → 6.9 |
| 2 | #K Agent.list() dinâmico | ⭐⭐⭐⭐ | 2-3 | 6.9 → 7.2 |
| 3 | #J SQLite + parentID | ⭐⭐⭐⭐ | 6-8 | 7.2 → 7.6 (paridade OC) |
| 4 | #L Worktree per agent | ⭐⭐⭐ | 8-12 | 7.6 → 8.5 |
| 5 | #M Swarm orchestration | ⭐⭐ | 10-15 | 8.5 → 8.9 |
| 6 | #N Remote/teammate | ⭐ | 15+ | 8.9 → 9.1 |

**Parada natural em #3** (paridade OpenCode, ~12h).
**Parada natural em #4** (8.5 — competitivo com OC em todas as dimensões, nicho em paralelismo real de edição).
**Completar #5-#6 só se houver demanda concreta** — orquestração de swarm e messaging entre agents são casos de uso avançados.

---

## 5. Estratégia de testes

Mesmo princípio do plano anterior:

1. Nenhuma regressão nos 1277 testes existentes — rodar `pytest -q` após cada item
2. Testes unit + integration por item; mocks para LLM (usar o `_FakeAgent` de `tests/test_delegate.py`)
3. **Para #J**, cobertura focada em SQL — integridade referencial, CTE recursivo, idempotência
4. **Para #L**, testes requerem um `tmp_git_repo` fixture (init + commit inicial) — tests/conftest.py
5. **Para #M/#N**, testes de timing precisam tolerância Windows (10% de slack)

Suite total esperada: 1277 → ~1350 com os novos.

---

## 6. Riscos e mitigações

| Risco | Impacto | Mitigação |
|---|:-:|---|
| Recursão profunda explode contexto | alto | `MAX_SUBAGENT_DEPTH=5` + trace mostra depth em `/subagents --tree` |
| SQLite locked em acesso concorrente | médio | `PRAGMA journal_mode=WAL`; connections curtas (context manager) |
| Migração JSON → SQLite perde traces antigos | baixo | Script migration idempotente; JSONs ficam no disco como backup até GC manual |
| Worktree `os.chdir` quebra paralelismo | alto | Migrar tools para cwd-aware (Option B); fallback: serializar com lock |
| Custom agent shadow built-in acidental | baixo | `list_agents()` prioriza built-in; warn no load se há conflito |
| Swarm `cancel_swarm` deixa orphans em DB | médio | Job GC automático que marca status='cancelled' em registros órfãos >1h |
| Send_message em ctx inexistente | baixo | Fail-soft retornando "not in swarm" — nunca raise |
| git worktree em repo sem commits | médio | `create_agent_worktree` detecta HEAD vazio → return None (fail-soft) |

---

## 7. Métricas de sucesso

1. **Recursão funcional**: um subagent `orchestrator` pode rodar `delegate_task("find X", agent_name="explorer")` e receber resultado — end-to-end test passando
2. **Queries hierárquicas**: `/subagents --tree <id>` mostra árvore com N níveis > 1 em uma sessão real
3. **Worktree isolamento**: subagent com `use_worktree=True` edita foo.py, primary session mantém foo.py original
4. **Paralelismo de edição sem conflito**: 3 subagents editando arquivos diferentes em 3 worktrees — zero conflitos
5. **Custom agent aparecendo via `Agent.list`**: agent definido em `.agents/agents/reviewer.md` é listado em `/agents list` e utilizável via `delegate_task(..., agent_name="reviewer")`
6. **Nota crit 7**: auto-avaliação + avaliação externa chegam a 8.0+ após Tier 1

---

## 8. Referências

### OpenCode (SQL parity)

- `packages/opencode/src/session/index.ts:125-167` — `Info` schema com `parentID`
- `packages/opencode/src/session/index.ts:448-458` — `Session.children` com SQL query
- `packages/opencode/src/agent/agent.ts:281-295` — `Agent.list()` merge + sort
- `packages/opencode/src/agent/agent.ts:107-263` — agent build from hardcoded + config.agent.*
- `packages/opencode/src/tool/task.ts:37-94` — recursion gate via permission

### Claude Code (orchestration)

- `src/utils/worktree.ts:702-778` — `createWorktreeForSession` (opt-in per-session)
- `src/utils/worktree.ts:902-952` — `createAgentWorktree` (automatic per-subagent)
- `src/tools/EnterWorktreeTool/EnterWorktreeTool.ts:77-119` — tool exposure
- `src/hooks/useSwarmInitialization.ts:30-81` — swarm bootstrap on resume
- `src/tools/shared/spawnMultiAgent.ts:305-539` — split-pane (tmux/iterm) spawn
- `src/tools/shared/spawnMultiAgent.ts:840-1032` — in-process teammate spawn
- `src/tasks/RemoteAgentTask/RemoteAgentTask.tsx:22-59` — remote poller pattern
- `src/tasks/InProcessTeammateTask/InProcessTeammateTask.tsx:1-30` — in-process mailbox
- `src/tools/SendMessageTool/SendMessageTool.ts:46-126` — routing schema

### Aru (estado atual)

- `aru/tools/delegate.py` — onde a maior parte das mudanças cai
- `aru/tools/registry.py` — `_SUBAGENT_TOOLS` constante a remover
- `aru/agents/catalog.py` — `AGENTS` dict a refatorar
- `aru/session.py` — `SubagentTrace` ainda relevante (schema vira row do SQL)
- `aru/runtime.py` — `RuntimeContext` ganha `subagent_depth` e `cwd`
- `aru/commands.py` — `/subagents`, `/subagent`, `/bg` existentes; novos `/worktree`, `/swarm`
- `docs/aru/plans/2026-04-19-multi-agent-parity.md` — plano anterior (itens #A-#H já entregues)

### Decisões registradas

- **Escopo reduzido** em #M/#N: in-process only, sem tmux/uds/bridge — suficiente para paridade OpenCode + 80% do valor CC
- **Fail-soft** em worktree: repos sem git → subagent roda in-place, não falha
- **Migração gradual** para SQLite: JSONs em disco continuam sendo lidos como fallback se DB falha abrir
- **`can_delegate` via frontmatter**: custom agents controlam recursão via YAML, não código (OpenCode parity)
- **Built-ins sempre shadowam custom**: `list_agents()` prioriza catálogo interno (evita surpresas com nome conflitante)
  *(Corrigido na seção 9: esta decisão inverte OpenCode — custom deve vencer com warn.)*

---

## 9. Critique e emendas (2026-04-19, pós-review)

Este plano foi revisado e os pontos abaixo corrigem leituras incorretas ou estimativas otimistas. Quem reler este documento no futuro deve tomar decisão com base nesta seção, não nas tabelas da seção 4 originalmente.

- **Deltas de nota por item eram falsa precisão.** O critério 7 pesa 5% na média ponderada — ganho máximo realista do plano completo é ~0.13 no score geral. As entradas "6.9 → 7.2 → 7.6" na seção 4 são invenção: a nota não se move em incrementos de 0.3 por feature. **Motivação deste plano não é a nota** — é utilidade concreta para workflows reais. Se a resposta para "seu workflow real precisa de recursão + hierarquia persistida + worktrees?" for "nunca", pare em #I. Se for "às vezes", faça Tier 1 e reavalie.

- **#L subestimou o custo de `os.chdir` em 5-10×.** A seção 3 diz "~3h sobre os 8-12h" para migrar tools para cwd-aware. Irreal: Aru tem ~19 tools + helpers, cada `open()`/`subprocess.run()`/`Path()` relativo é um bug silencioso esperando acontecer. Orçar **16-20h** para Option B, ou adotar **Option A (serialização via lock) como MVP** — paralelismo só quando `use_worktree=False`. O modo de falha (sub-agent grava no repo errado sem erro) é pior que perder paralelismo.

- **#J: DB global vaza traces entre projetos.** A seção 3 propõe `~/.aru/subagents.db`. Errado — sessões do Aru já vivem em `<project>/.aru/sessions/` por design; o DB deve seguir o mesmo e morar em `<project>/.aru/subagents.db`. Além disso: `primary_session_id TEXT` sem FK real é incoerente — se vamos buscar integridade relacional, o elo mais importante (session → subagent) precisa ter REFERENCES também, não só `parent_task_id`.

- **#K inverte a semântica do OpenCode.** A seção 3 (linha 409) faz built-in shadow custom com comentário "mirror OC behaviour". Leitura errada de `agent/agent.ts:107-263`: lá config.agent.* **sobrescreve** defaults. Correção: custom agent com mesmo nome deve vencer built-in com warn no load. Caso contrário o sistema de customização fica decorativo — usuário não consegue redefinir o `explorer`.

- **#N assume hook de drain que o Agno não expõe.** `#H` (já entregue) drena notificações antes do turno via prepend no user_input do REPL. `#N` (send_message entre peers) precisa drenar durante o turno de outro subagent — Agno não tem middleware de pre-turn nem API de injeção mid-loop. **Investigar viabilidade antes de estimar horas** — pode ser bloqueador requerendo fork do framework.

- **Faltando em #M: `MAX_CONCURRENT_SUBAGENTS` + cap de custo.** Recursão com `MAX_SUBAGENT_DEPTH=5` e fan-out 3 por nível = 243 sub-agents em cascata. Uma conta Anthropic Pro satura em 3-4 paralelos (rate limit RPM/TPM). Antes de #M existir, o plano precisa: (a) limite configurável de sub-agents concorrentes, (b) cap de custo acumulado por sessão que para delegações quando atinge threshold.

### Decisão

**Implementar #I apenas.** Esforço real 2-3h, baixo risco, fecha a crítica mais concreta (bloqueio por código contradiz `AgentSpec` extensível). Usar em tarefa real por 1-2 semanas. Só então decidir sobre #J+#K (paridade OC) ou parar por aí.

#L em diante ficam congelados até haver uso documentado que justifique o custo real (16-20h para #L, potencial bloqueio para #N, rate-limit guardrails para #M).
