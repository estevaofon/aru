# Plano: Tier 3 — Fechar lacunas e destravar paralelismo real

**Criado:** 2026-04-20
**Status:** Aprovado (v2 após revisão externa) — implementação integral em uma passada após Tier 2 mergeado
**Depende de:** Tier 2 (`feat/tier2-product`) — Stage 1 usa hook `file.changed`; Stage 3 usa memory store; Stage 4 usa LSP client + apply_patch.
**Objetivo:** Fechar features conscientemente adiadas dos Tiers 1-2 e destravar o caso de uso principal que ainda está quebrado: **sub-agentes paralelos em worktrees distintas**. Total ~28–38h numa branch única com 4 commits.

### Histórico de revisão

**v2 (2026-04-20, pós-crítica externa):**
- Stage 1: idempotência **por byte-match** (`formatted == current_content → skip write`), não TTL. Contradição entre corpo e R2 resolvida. Flag `_in_progress` por path como defesa-em-profundidade via try/finally, sem TTL frágil.
- Stage 4: **`documentChanges` suportado no day-one** (não punt). ~20 linhas extras evitam UX ruim em TS (typescript-language-server usa `documentChanges`, que estava sendo rejeitado). Stage 4 sobe de 6-8h para 7-10h.
- Stage 2: `delegate_task(worktree=...)` lifecycle preciso — **branch name obrigatória** (path não suportado), worktrees **persistem até `/worktree remove` manual**, **lock por branch** em `ctx` para serializar `create_worktree` concorrente.
- Stage 2: escopo realista — bump de 10-14h para **14-18h** (7 tools + gitignore + delegate + session restore + display + migração de testes existentes).
- Stage 2 adiciona `display.py` (status bar consome `ctx.cwd` em vez de `os.getcwd()`) + session restore path em `session.py/cli.py` (R11 nova).
- R11 adicionado (session restore).

---

## 1. Contexto

Tier 2 entregou worktree como primitiva manual, auto-memory com índex no prompt, LSP com 4 operações read-only, hooks com file.changed. Mas três lacunas continuam presentes:

1. **`os.getcwd()` process-global** impede paralelismo real entre sub-agentes — Stage 1 do Tier 2 deixou isso explicito em "non-goals".
2. **Auto-memory tem só um caminho de leitura**: índex no prompt. Se o agente quer o corpo completo de uma memory, precisa `read_file` adivinhando o slug.
3. **LSP é read-only** — read-only integration acabou sendo útil pra exploração mas deixa o valor maior (refactor seguro via rename) em cima da mesa.

Adicionalmente, **toda tool que escreve arquivo** (write/edit/apply_patch) não formata o output. Code generated pelo agent sai com inconsistência de estilo e precisa de passada manual de `black`/`prettier` depois.

### Mapa dos alvos

| # | Feature | Arquivos principais | Inspiração | Esforço |
|:-:|---|---|---|:-:|
| 1 | Format integration pós-`file.changed` | `aru/format/` (novo), config `format.*` | OC `src/format/` | 4–6h |
| 2 | Cwd-aware tools refactor | `runtime.py`, `tools/file_ops.py`, `search.py`, `shell.py`, `apply_patch.py`, `gitignore.py`, `delegate.py`, `display.py`, `session.py`, `cli.py` | CC worktree + agent scoping | 14–18h |
| 3 | Memory search tool | `aru/tools/memory_tool.py` (novo) | CC `services/SessionMemory/findRelevantMemories.ts` | 4–6h |
| 4 | LSP rename (code action) com `changes` + `documentChanges` | `aru/tools/lsp.py`, `aru/lsp/client.py` | OC `src/tool/lsp.ts` rename | 7–10h |

### Ordem de execução sugerida

1. **Stage 1 (Format)** — easy, destrava review de código automático; zero dependência estrutural
2. **Stage 2 (Cwd-aware)** — heavy e breaking; fazer cedo pra Stage 4 (rename) já nascer correto
3. **Stage 3 (Memory search)** — light; independente
4. **Stage 4 (LSP rename)** — depende de #2 (rename aplica edits multi-file em worktree-aware) e do apply_patch

### Não-objetivos

1. **Formatter interativo / configurável por regra** — usa os binários padrão (`black`, `prettier`, `rustfmt`) via stdin. Sem regex customizada.
2. **LSP code actions além de rename** — `quickFix`, `extractFunction`, organize imports ficam para Tier 4 se houver sinal de uso.
3. **Memory embedding-based semantic search** — Stage 3 é keyword substring puro. Semantic search exige embeddings + storage + custo recorrente.
4. **Cwd refactor como breaking sem migration path** — plugins antigos que usam `os.getcwd()` continuam funcionando; novo código DEVE usar `get_ctx().cwd`. Documentado.
5. **`os.chdir` automático no enter_worktree** — Stage 2 elimina a necessidade de chdir. Compatibilidade com código legado via fallback ao process cwd.

---

## 2. Stage 1 — Format integration (~4–6h)

**Arquivos:**
- `aru/format/__init__.py` (novo)
- `aru/format/manager.py` (novo) — `FormatManager` + detecção por extensão + `_in_progress` set + byte-match idempotência
- `aru/format/runner.py` (novo) — subprocess com stdin/stdout, timeout via Tier 1 pattern
- `aru/config.py` — campo `format: dict`
- `aru/cli.py` — instala FormatManager e subscribe no hook `file.changed`
- `tests/test_format.py` (novo)

### Problema

Arquivo escrito/editado pelo agent sai sem formatação. Linhas longas, imports desordenados, inconsistência com o resto do projeto. Agent não sabe o que o projeto prefere sem ler explicitamente `.prettierrc` / `pyproject.toml`. A cada turno, re-aprende ou erra.

### Shape da solução

1. Config em `aru.json`:
   ```json
   {
     "format": {
       "enabled": true,
       "python":     { "command": "black",   "args": ["-q", "-"],                    "stdin": true },
       "typescript": { "command": "prettier", "args": ["--stdin-filepath", "{path}"], "stdin": true },
       "rust":       { "command": "rustfmt",  "args": ["--emit=stdout"],              "stdin": true }
     }
   }
   ```
   - Padrão `enabled: false`. Opt-in explícito.
   - `{path}` é placeholder substituído pelo caminho real (prettier usa para escolher parser).

2. `FormatManager` mantém dicionário de extensão -> entrada de config. Detecta por extensão (`.py`, `.ts`, `.tsx`, `.rs`, `.go`).

3. Subscribe ao `file.changed` hook:
   ```python
   async def _format_if_applicable(event):
       path = event.get("path")
       mtype = event.get("mutation_type")
       if path is None or mtype in (None, "unknown", "delete"):
           return
       await _run_formatter(path)
   ```

4. `_run_formatter` pipe o conteúdo atual via stdin, lê stdout, escreve de volta. Se formatter sai non-zero, **NÃO mexe no arquivo** (preserva trabalho do agent). Log via `aru.format` logger.

5. **Idempotência por byte-match** (quebra o loop na raiz, sem TTL frágil):
   - Após rodar o formatter, comparar `formatted == current_content`. Se forem byte-idênticos, **skip the write** — não toca no disco, não dispara `file.changed`. Ciclo morre no primeiro turno onde o arquivo já está no formato.
   - **Defesa-em-profundidade**: `_in_progress: set[str]` de paths atualmente sendo formatados, gerenciado via `try/finally`. Se formatter crashar, `finally` remove o path do set (não depende de timer).
   - Sem TTL: black em arquivo grande pode passar de qualquer threshold arbitrário. Lifecycle cola direto no escopo do handler.

### Edge cases

- Formatter não instalado no PATH → warning na primeira invocação, marca `failed`, skip nas próximas até restart (mesmo padrão do MCP health).
- Arquivo Python com syntax error inválido → `black` sai non-zero, conteúdo do agent preservado.
- Arquivo novo (AddFile no apply_patch) → `file.changed` emitido, formatter roda.
- Delete → skip (path deletado, nada para formatar).
- Recursão: write pelo formatter → `_notify_file_mutation` → `file.changed` → formatter. Quebrar com flag `_formatting_in_progress` por path.

### Testes

- `tests/test_format.py`:
  - Python file escrito sem formatação → após hook, arquivo é black-formatted
  - Formatter crasha (binário inexistente) → arquivo original preservado
  - `format.enabled: false` → formatter não roda
  - Extensão não configurada (`.md`) → skip
  - Delete não dispara formatter

---

## 3. Stage 2 — Cwd-aware tools refactor (~14–18h)

**Arquivos:**
- `aru/runtime.py` — campo `cwd` em RuntimeContext, helpers, `worktree_create_locks`
- `aru/tools/file_ops.py` — todos os leitores/escritores resolvem via `ctx.cwd`
- `aru/tools/search.py` — glob/grep walk a partir de `ctx.cwd`
- `aru/tools/shell.py` — `subprocess.Popen(cwd=ctx.cwd)`
- `aru/tools/apply_patch.py` — `root` default para `ctx.cwd`
- `aru/tools/gitignore.py` — walk cache keyed em cwd
- `aru/tools/worktree.py` — `enter_worktree` set `ctx.cwd` sem `os.chdir`
- `aru/tools/delegate.py` — `delegate_task(worktree=...)` + lock-by-branch
- `aru/session.py` — `Session.project_root` usa explicit init, `worktree_path` serializado
- `aru/cli.py` — **session restore path** (R11): re-enter worktree se salvo
- `aru/display.py` — status bar consome `ctx.cwd`, não `os.getcwd()`
- `AGENTS.md` — seção migration para plugins
- `tests/test_cwd_awareness.py` (novo)
- `tests/test_worktree_session_restore.py` (novo — R11)
- Testes existentes — auditoria para uso implícito de `os.getcwd()`

### Problema

Process cwd é global no Python. `os.chdir` afeta TODAS as threads/tasks. Dois sub-agentes paralelos em worktrees distintas vão colidir: o que chamar `os.chdir(worktree_A)` move o processo inteiro, e sub-agente B vê worktree A.

Consequência concreta hoje: `asyncio.gather(delegate_task(worktree="a"), delegate_task(worktree="b"))` é **impossível** de ser correto.

### Shape da solução

**Princípio:** substituir `os.getcwd()` por `get_ctx().cwd` em tudo que é tool, e nunca chamar `os.chdir` em fluxos normais. Process cwd fica congelado no project_root da sessão.

1. `RuntimeContext` ganha:
   ```python
   cwd: str = field(default_factory=os.getcwd)
   ```
   - `fork_ctx()` copia `cwd` da parent por valor (string imutável — isolated automaticamente via shallow copy).

2. Helper em `runtime.py`:
   ```python
   def resolve_path(path: str) -> str:
       """Interpret a relative path against the ctx's cwd."""
       if os.path.isabs(path):
           return path
       return os.path.join(get_ctx().cwd, path)
   ```

3. Substituições por arquivo:
   - `file_ops.py`: `read_file` / `write_file` / `edit_file` / `list_directory` → `resolve_path(path)` antes de I/O.
   - `search.py`: `glob_search` / `grep_search` → `directory or ctx.cwd`; walk começa em resolved dir.
   - `shell.py`: `subprocess.Popen(..., cwd=get_ctx().cwd)` — tool bash respeita worktree automaticamente.
   - `apply_patch.py`: default `root=get_ctx().cwd` em `apply_patch_text` e `validate`.
   - `gitignore.py`: `walk_cache` key = `(ctx.cwd, ...)` para evitar cross-worktree pollution.

4. `worktree.enter_worktree(path, branch)`:
   - **NÃO chama `os.chdir`**. Apenas set `ctx.worktree_path`, `ctx.worktree_branch`, `ctx.cwd`.
   - `session.cwd` também atualizado para consistência com UX (`/cost` etc.).

5. `delegate_task` ganha parâmetro `worktree: str | None = None`. **Contrato preciso:**
   - **Branch name obrigatória** — não aceita path absoluto. Input é interpretado como branch name sempre. Simplifica semantics e evita confusão "isso é path ou branch?".
   - **Idempotência + race-safety**: `ctx.worktree_create_locks: dict[str, asyncio.Lock]` (shared via fork, como `subagent_instances_lock`). `delegate_task` acquires lock-for-branch antes de chamar `create_worktree(branch)`. Dois delegates paralelos pra mesma branch serializam no create; o segundo vê a worktree já existente e reutiliza (create_worktree já é idempotente).
   - **Lifecycle de cleanup**: worktrees criadas por `delegate_task` **persistem** até `/worktree remove <branch>` manual ou comando explícito `/worktree clean --stale` (futuro). Sem auto-cleanup — riscoso deletar trabalho in-flight.
   - `forked.cwd = worktree_path` após `fork_ctx()`. Sub-agent herda `ctx.cwd` isolado; seus tools operam naquela worktree sem afetar siblings. Process cwd continua em project root.

6. Migration section em AGENTS.md:
   ```
   ## Plugin migration (Tier 3 Stage 2)
   
   `os.getcwd()` continues to return the process cwd (always project root).
   New code should use `get_ctx().cwd` to respect per-agent worktree scope.
   Tools still using `os.getcwd()` will see project root regardless of
   the agent's active worktree — a silent correctness bug in multi-agent
   parallel use.
   ```

### Edge cases

- **Custom plugin tool com `os.getcwd()`**: continua funcionando (vê project root), mas não respeita worktree scope. Warning em AGENTS.md.
- **MCP server subprocesses**: já são spawned com `os.environ.copy()` — se dependerem de cwd, ficam no project root. Anotar.
- **Windows UNC paths** em subprocess — testar.
- **Process cwd inconsistente com ctx.cwd**: só deve acontecer se algum código legado chamar `os.chdir`. Teste assert `os.getcwd() == session.project_root` ao final de cada turn.
- **Async task spawned sem fork_ctx**: herda ctx via contextvars, mas se usar `os.getcwd()` diretamente, vê process cwd. OK porque process cwd é project root.

### Testes

- `tests/test_cwd_awareness.py`:
  - `os.getcwd()` inalterado antes e depois de `enter_worktree`
  - Dois `asyncio.gather(delegate_task(worktree="a"), delegate_task(worktree="b"))` — cada sub-agent's `ctx.cwd` distinto durante execução
  - `read_file("rel/path.py")` em sub-agent resolve via `ctx.cwd` (worktree), não process cwd
  - `bash("pwd")` em worktree retorna worktree path
  - `grep_search` em worktree A não varre worktree B
  - `apply_patch` com paths relativos aplica na worktree correta
  - Parent ctx.cwd inalterado após sub-agent mutation
- Existentes:
  - `test_worktree.py` atualizado: remove expectativa de `os.chdir`
  - `test_apply_patch.py`: continua passando com `monkeypatch.chdir` porque `ctx.cwd` cai no `os.getcwd()` default

---

## 4. Stage 3 — Memory search tool (~4–6h)

**Arquivos:**
- `aru/tools/memory_tool.py` (novo)
- `aru/tools/registry.py` — inclui em `_READ_ONLY_TOOLS`
- `aru/memory/store.py` — pode precisar de `search_memories(query)` helper
- `tests/test_memory_tool.py` (novo)

### Problema

Agent vê index MEMORY.md no system prompt (Tier 2 #4), mas:
- Index tem só name + description (one-liner por memory).
- Se agent quer o body completo de uma memory para tomar decisão, precisa `read_file` adivinhando o caminho exato (`~/.aru/projects/<hash>/memory/<slug>.md`) — fora do project dir, exige paths absolutos.
- Index está capped em 200 linhas. Em projetos maduros, memórias antigas ficam invisíveis.

### Shape da solução

Tool `memory_search(query: str = "", slug: str | None = None) -> str`:

```python
def memory_search(query: str = "", slug: str | None = None) -> str:
    """Search project memory by substring or load a specific memory body.
    
    Args:
        query: Case-insensitive substring to match against name/description/body.
               Empty string returns summary stats instead.
        slug: If provided, return the full body of that specific memory.
              Takes precedence over query.
    """
```

Comportamento:

| Input | Output |
|---|---|
| `memory_search(slug="user_pytest")` | Full frontmatter + body of `user_pytest.md`, or `"No memory with slug..."` |
| `memory_search(query="pytest")` | List of matching entries with 200-char body preview each |
| `memory_search()` | Summary: `"N memories: X user, Y feedback, Z project, W reference"` |

Implementação:
- `aru/memory/store.py` ganha `search_memories(project_root, query)` que itera `list_memories` e filtra por substring em name/description/body.
- Match é case-insensitive, word-boundary-agnóstico (substring puro).
- Ordenação: matches no `name` primeiro, depois `description`, depois `body`.

Adicionado a `_READ_ONLY_TOOLS` (todos os agents vêem).

### Edge cases

- Query vazia + slug vazio → summary, não lista completa (evita dump de 50 memories).
- Query sem matches → `"No memories matching query"`.
- Memory corrompida (frontmatter inválido) → skip no search, como em `list_memories`.
- Project sem diretório de memory → `"No memories stored for this project"`.

### Testes

- `tests/test_memory_tool.py`:
  - `memory_search(slug="x")` retorna body se existe
  - `memory_search(slug="missing")` retorna mensagem friendly
  - `memory_search(query="pytest")` retorna apenas matches relevantes
  - `memory_search()` retorna counts por type
  - Case-insensitive: query "PyTest" encontra memory com "pytest"
  - Preview truncado em 200 chars
  - Projeto sem memory dir → não crasha

---

## 5. Stage 4 — LSP rename code action (~7–10h)

**Arquivos:**
- `aru/lsp/client.py` — handler para WorkspaceEdit
- `aru/tools/lsp.py` — novo `lsp_rename` tool
- `aru/tools/apply_patch.py` — pode ser chamado internamente para aplicar WorkspaceEdit
- `tests/test_lsp_rename.py` (novo)

### Problema

Rename de símbolo via grep+edit é a operação mais frequente onde grep alucina. Scenarios:
- "Renomeie `user_name` para `username`" — grep pega occurrences em strings, comments, nomes de variáveis em escopos diferentes que não deveriam mudar.
- LSP's `textDocument/rename` é semantic: só renomeia o símbolo real, respeitando scope e imports.

Tier 2 Stage 5 parou nos 4 read-only. Rename é o primeiro write. Uma vez que rename funciona, extract_method / organize_imports são incrementos pequenos.

### Shape da solução

Novo tool:
```python
async def lsp_rename(file_path: str, line: int, column: int, new_name: str) -> str:
    """Rename the symbol at file:line:column (0-indexed) to new_name.
    
    Runs textDocument/rename via the language server. The result is a
    WorkspaceEdit covering every usage across the workspace; the tool
    applies it atomically via apply_patch's rollback mechanism.
    """
```

Fluxo:

1. Resolve LSP client via `get_lsp_manager().get_client_for(file_path)`.
2. `ensure_open` no arquivo (Tier 2 já implementado).
3. `request("textDocument/rename", {textDocument, position, newName})`.
4. Response é um `WorkspaceEdit` — **dois formatos possíveis, ambos suportados** no day-one:

   **`changes`** (formato simples — pylsp):
   ```json
   {
     "changes": {
       "file:///abs/a.py": [{"range": {...}, "newText": "username"}, ...],
       "file:///abs/b.py": [...]
     }
   }
   ```

   **`documentChanges`** (formato rico — typescript-language-server):
   ```json
   {
     "documentChanges": [
       {
         "textDocument": {"uri": "file:///abs/a.ts", "version": 1},
         "edits": [{"range": {...}, "newText": "username"}, ...]
       },
       ...
     ]
   }
   ```

5. Converter `WorkspaceEdit` para lista de operações atômicas e aplicar:
   - **Normalizar** os dois formatos num dict unificado `{uri: list[TextEdit]}`. `documentChanges` pode também ter `CreateFile` / `DeleteFile` / `RenameFile` — nesta stage só suportamos entradas com `edits` (TextDocumentEdit). Outras emitem warning e são puladas.
   - Para cada URI: ler arquivo, aplicar TextEdits **em ordem reversa de offset** (para offsets de edits subsequentes não drift), escrever.
   - Usar `checkpoint_file(path)` antes de cada write (Tier 2 Stage 2 pattern).
   - Se qualquer edit falhar, rollback via restaurar conteúdo original armazenado em memória.
6. Emit `file.changed` por arquivo (com `mutation_type="rename"`) → Stage 1 Format roda, LSP didChange sincroniza.

Estrutura do apply:

```python
def _apply_workspace_edit(edits: dict[str, list[TextEdit]]) -> None:
    backups: dict[str, str] = {}
    try:
        for uri, text_edits in edits.items():
            path = uri_to_path(uri)
            original = _read_text(path)
            backups[path] = original
            _checkpoint_file(path)
            # Apply edits in reverse order of start offset
            sorted_edits = sorted(text_edits, key=lambda e: _range_start_offset(original, e["range"]), reverse=True)
            new_text = _apply_text_edits(original, sorted_edits)
            _write_text(path, new_text)
            _notify_file_mutation(path=path, mutation_type="rename")
    except Exception as exc:
        # Rollback
        for path, original in backups.items():
            _write_text(path, original)
        raise
```

### Edge cases

- LSP server retorna `null` → "No symbol to rename at that position."
- LSP retorna `WorkspaceEdit` com `documentChanges` contendo `CreateFile` / `DeleteFile` / `RenameFile` (não apenas TextDocumentEdit): log warning e pula essas entradas, aplica apenas os `edits`. Rename puro raramente precisa de file creates.
- Multiple edits in same file: ordem reversa por offset para aplicar corretamente.
- Edit ranges inválidos (linha fora do arquivo): falha, rollback.
- Rename name é inválido pra linguagem (ex. palavra reservada) → LSP retorna erro, tool surfa.

### Testes

- `tests/test_lsp_rename.py` (usa FakeClient como em Stage 5 Tier 2):
  - WorkspaceEdit com 1 arquivo, 3 edits → todos aplicados em ordem reversa
  - WorkspaceEdit com 3 arquivos → todos aplicados, todos checkpointed
  - LSP retorna null → mensagem "no symbol"
  - LSP retorna `documentChanges` (unsupported format) → mensagem clara
  - File write falha no 2º arquivo → backups dos primeiros restaurados
  - Após success, `file.changed` emitido com `mutation_type="rename"` por file
  - Offset drift não ocorre quando múltiplas edits no mesmo file com `newText` de tamanho diferente

---

## 6. Testes consolidados

Novos test files (4):

| Stage | Test file | Casos |
|:-:|---|:-:|
| 1 | `tests/test_format.py` | 5 |
| 2 | `tests/test_cwd_awareness.py` | 7 |
| 3 | `tests/test_memory_tool.py` | 7 |
| 4 | `tests/test_lsp_rename.py` | 7 |

Total: ~26 casos novos.

Smoke tests manuais:

1. **Format**: configurar `format.python: black`, escrever arquivo com indent errado via agent, conferir que após salvar o arquivo está formatted.
2. **Cwd parallel**: prompt que delega 2 sub-agentes simultâneos em worktrees distintas pra escrever arquivos com mesmo nome relativo → arquivos vão nas worktrees certas, sem colisão, `os.getcwd()` inalterado.
3. **Memory search**: sessão com 5+ memories, agent usa `memory_search("pytest")` e recebe subset relevante.
4. **LSP rename**: configurar pylsp, agent renomeia função em arquivo pequeno, múltiplos arquivos atualizados, `/undo` desfaz rename atômico.

---

## 7. Riscos

| # | Risco | Mitigação |
|:-:|---|---|
| R1 | Formatter destrói código se stdout incompleto num edge case (ex. black com syntax error) | Non-zero exit → preservar original. stdout vazio → preservar. |
| R2 | Recursão format → file.changed → format | Byte-match: formatted == content → skip write (ciclo morre no 1º turno já formatted). `_in_progress` set como defense-in-depth via try/finally, sem TTL. |
| R3 | Cwd refactor quebra custom plugin que usa `os.getcwd()` implicitamente | Process cwd continua = project root. Plugin continua lendo dali; só perde o scope de worktree. Documentado em migration section. |
| R4 | Test suite existente assume `os.getcwd()` mudar em enter_worktree | Auditoria: fixtures `monkeypatch.chdir` continuam válidas porque setam process cwd (o que ctx.cwd copia). `test_worktree.py` atualizado para nova semântica. |
| R5 | MCP server subprocesses inherit wrong cwd | Já era inherit de `os.getcwd()` que agora é process cwd = project root. Se user tinha MCP esperando estar em worktree, anotar. |
| R6 | `memory_search` retorna muita informação de uma vez (todas as memories ~50) | Preview truncado 200 chars; query obriga filtragem. |
| R7 | ~~LSP rename com `documentChanges` em vez de `changes`~~ (mitigado em v2) | Day-one suporta ambos `changes` e `documentChanges.edits`. Entries do tipo CreateFile/DeleteFile/RenameFile emitem warning e são puladas — TextDocumentEdit cobre rename puro. |
| R8 | Rename race com outros edits concorrentes no mesmo arquivo | Rename já lock-free num sub-agent; entre sub-agentes diferentes, cada um tem ctx.cwd isolado (Stage 2). Mas duas renames concorrentes no mesmo arquivo: possível race. Aceitar — uso real é serial. |
| R9 | Stage 4 apply do WorkspaceEdit pode aplicar edits parciais se I/O falha entre arquivos | Rollback via in-memory backup (mesmo pattern de apply_patch). |
| R10 | Format em arquivo grande (100k+ LOC) trava REPL | Formatter roda em `asyncio.to_thread` com timeout (Tier 1 #1 pattern). Default 30s. |
| R11 | Session restore (`aru --resume <id>`) não restaura `ctx.cwd` se a sessão original estava em worktree | `Session.to_dict` já serializa `worktree_path` e `worktree_branch` (Tier 2); `cli.py` on resume path precisa: se session tem `worktree_path` válida e existente, `enter_worktree(path, branch)` antes do REPL loop iniciar. Path inválida → fallback para project_root com warning. |

---

## 8. Estrutura de entrega (branch + commits)

Branch única `feat/tier3-product`, 4 commits cirúrgicos:

1. `feat(format): formatter integration via file.changed hook` (Stage 1)
2. `refactor(runtime): cwd-aware tools — eliminate process-global os.chdir` (Stage 2) [**breaking** — documented migration]
3. `feat(tools): memory_search tool for agent-side memory queries` (Stage 3)
4. `feat(lsp): rename code action with atomic WorkspaceEdit application` (Stage 4)

Rollback: cada commit é `git revert`-friendly. Stage 2 é o único breaking para plugins externos — documentado no commit message + AGENTS.md.

## 9. Checklist de merge

- [ ] Stage 1 — `aru/format/` + FormatManager + hook file.changed subscribe + config.format + testes verdes
- [ ] Stage 2 — `ctx.cwd` em RuntimeContext + todas as tools migradas + `delegate_task(worktree=...)` + AGENTS.md migration section + testes verdes
- [ ] Stage 2 — audit: `rg "os\.getcwd\(\)" aru/tools/` — zero matches (exceto em helpers de fallback documentados)
- [ ] Stage 3 — `memory_search` em `_READ_ONLY_TOOLS` + store helper `search_memories` + testes verdes
- [ ] Stage 4 — `lsp_rename` tool + WorkspaceEdit applier + rollback + `file.changed` emit + testes verdes
- [ ] Smoke manual dos 4 cenários passa
- [ ] AGENTS.md atualizado (format config, cwd-aware migration, memory_search tool doc, lsp_rename)
- [ ] Versão bump `0.35.0 -> 0.36.0`

---

## 10. Pós-Tier-3 (futuro, NÃO neste plano)

- **LSP code actions adicionais**: `quickFix`, `extractFunction`, `organizeImports` — cada um é add-on de ~2-3h sobre a infraestrutura do Stage 4.
- **LSP `documentChanges` format** para suportar `typescript-language-server` nativamente sem fallback.
- **Semantic memory search** via embeddings (requer storage + custo recorrente — não trivial).
- **Cross-project memory** — opt-in global para preferências do user que não são project-specific.
- **Worktree auto-cleanup**: tool que remove worktrees antigas automaticamente após N dias de inatividade.
- **Format rules customizadas** via plugin hook, além dos binários padrão.
