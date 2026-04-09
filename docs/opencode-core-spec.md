# opencode — Core Spec

Referência técnica do que forma o **core** do opencode (agente de coding open-source). O objetivo não é documentar a API, e sim capturar decisões de arquitetura, contratos e padrões distintivos — de forma que outros agentes open-source possam comparar item a item ("o opencode faz X assim — e nós?").

Todas as citações são paths relativos ao repositório (`packages/opencode/src/...`), com linhas aproximadas. O código-fonte é a fonte da verdade; este doc é o mapa.

---

## 1. Arquitetura geral

### 1.1 Monorepo e papéis

O repositório é um monorepo turbo/bun. Papéis dos packages:

- **Core/server:** `packages/opencode` — servidor Hono, sessões, mensagens, projetos, worktrees, bus, CLI, providers, tools. É o único processo "autoridade".
- **SDK:** `packages/sdk/js` — cliente TypeScript **gerado a partir do OpenAPI** (`packages/sdk/openapi.json`, ~13k linhas). Todos os clients consomem o mesmo schema.
- **Plugin SDK:** `packages/plugin` (`@opencode-ai/plugin`) — tipagens/contratos para plugins externos.
- **Clients:** `packages/app`, `packages/web`, `packages/desktop`, `packages/desktop-electron`, `sdks/vscode/`, `packages/slack` — todos consomem o SDK JS, nenhum toca storage diretamente.
- **TUI:** não fica no core; é cliente como qualquer outro (conecta ao servidor via HTTP/SSE).
- **Infra:** `packages/util`, `packages/ui`, `packages/storybook`, `packages/docs`, `packages/function`, `packages/enterprise`, `packages/console`, `packages/extensions`.

**Decisão distintiva:** *TUI é só mais um client*. Core nunca renderiza. O TUI roda como processo separado e fala HTTP + SSE com o servidor — o que também permite o mesmo servidor servir desktop/web/VSCode simultaneamente, rodar remoto, etc.

### 1.2 Modelo cliente-servidor

- **HTTP framework:** Hono + `@hono/node-server` + `hono-openapi`.
  - `src/server/server.ts:47-236` — `ControlPlaneRoutes()` monta middleware (auth, CORS, compressão, logging, OpenAPI).
  - `src/server/server.ts:238-244` — `create()` constrói app Hono com `createNodeWebSocket()` para upgrade.
  - `src/server/server.ts:251-269` — `openapi()` usa `generateSpecs()` do `hono-openapi` para emitir o spec.
  - `src/server/server.ts:273-352` — `Server.listen()` sobe o HTTP com opção de publicar mDNS para discovery local.
- **Rotas:** `src/server/instance.ts:45-59` monta sub-rotas lazy: `/project`, `/pty`, `/config`, `/session`, `/permission`, `/question`, `/provider`, `/file`, `/event`, `/mcp`, `/tui`. Implementações em `src/server/routes/*.ts`.
- **SDK codegen:** `packages/sdk/js/src/gen/` é gerado a partir de `openapi.json`. Cliente: `createOpencodeClient({ directory?: string })` injeta `x-opencode-directory` header (`packages/sdk/js/src/client.ts:32-55`).
- **Streaming:** SSE via `streamSSE()` da Hono — rota `GET /event` assina `Bus.subscribeAll()` e emite frames JSON + heartbeat a cada 10s (`src/server/routes/event.ts:11-83`). Sem WebSocket para eventos — apenas upgrade para PTY/TUI.

**Decisão distintiva:** **OpenAPI-first**. O schema é fonte única; o JS SDK é gerado, tipado, e compartilhado por todos os clients. Qualquer mudança de rota quebra tipo em build-time nos clients.

### 1.3 Multi-projeto / multi-sessão / multi-worktree

Três níveis de aninhamento: **Project → Worktree/Workspace → Session**.

- **Project:** `src/project/project.ts:21-49,90-101` — `Project.Info = { id, worktree, name, icon, vcs?, commands, ... }`. `fromDirectory(dir)` resolve o root via git e caches o `Instance`.
- **Workspace/Worktree:** `src/worktree/index.ts:43-53` — `Worktree.Info = { name, branch, directory }`. Criado via `git worktree add`. `src/control-plane/workspace.ts:16-95` generaliza em `Workspace.Info { id, type: "worktree", ... }`.
- **Adaptor pattern:** `src/control-plane/adaptors/index.ts:4-20` — workspace tem adaptors (hoje só `worktree`; extensível para cloud/ssh). Cada adaptor implementa `configure / create / remove / target(config) → { type: "local"|"remote", directory|url }`.
- **Session:** `src/session/index.ts:125-167` — liga-se a `projectID` + opcional `workspaceID` + opcional `parentID` (fork).
- **Routing por directory (distintivo):** `src/server/router.ts:29-105` — `WorkspaceRouterMiddleware()` intercepta todo request. Lê `?directory=` ou `x-opencode-directory` header → resolve instance local ou proxy para workspace remoto via `ServerProxy`. **Não existe path-based tenancy** (sem `/workspaces/:id/...`); o tenant é header-driven e implícito.
- **Instance caching:** `src/project/instance.ts:65-83` — instances cached por diretório resolvido. Boot lazy via `Project.fromDirectory()`. Dispose dispara `server.instance.disposed` para `GlobalBus`.

### 1.4 Event bus

- `src/bus/index.ts:10-185` — `Bus` construído em cima de `Effect PubSub` (unbounded). Typed pub/sub por evento + um wildcard "all".
- `src/bus/bus-event.ts:1-40` — `BusEvent.define(type, zodSchema)` — schema usado tanto runtime quanto na geração do OpenAPI.
- `src/bus/global.ts` — `GlobalBus` (EventEmitter) cross-instance para coordenação de cluster e eventos "meta" (dispose etc).
- **Dual-layer eventing:** `Bus` (tempo real, instance-local) + `SyncEvent` (agregado por `sessionID`, event-sourcing — `src/session/index.ts:188-200`). O primeiro alimenta SSE; o segundo alimenta replay/share.

### 1.5 CLI & boot

- `src/index.ts:64-184` — yargs + middlewares: log init, version check, DB migration (JSON legacy → SQLite), injeção de env vars (`AGENT=1`, `OPENCODE=1`, `OPENCODE_PID`).
- Comandos: `serve`, `run`, `attach`, `agent`, `acp`, `mcp`, `generate`, `web`, etc. (`src/cli/cmd/*`).
- `ServeCommand` (`src/cli/cmd/serve.ts:9-24`) — bare server headless.
- `RunCommand` — sobe server **in-process** e usa o próprio SDK JS para conversar com ele. Mesma abstração que um client externo.
- **Bootstrap pós-boot:** `src/project/bootstrap.ts:15-31` — init paralelo de `Plugin`, `Format`, `LSP`, `File`, `FileWatcher`, `Vcs`, `Snapshot`. Depois assina `Command.Event.Executed` para marcar o projeto como inicializado.

### 1.6 Runtime (Effect)

- opencode usa **Effect** (`effect` library) como runtime para DI, scoped resources, streams cancelláveis. Não é detalhe cosmético: quase todo service expõe `Interface` + `Layer`.
- `src/effect/instance-state.ts:1-82` — `InstanceState<A>` wrap sobre `ScopedCache` keyed por diretório; auto-dispose on scope close.
- Bridge sync↔async: `Instance.provide()` usa AsyncLocalStorage (`ALS`) para disponibilizar contexto a código síncrono não-Effect.
- Trade-off: curva alta; mas ganhos em cancelamento determinístico, composição de streams, e isolamento por instance.

### Aplicável a outros projetos
- **TUI-como-client:** se seu agente acopla TUI ao core, considere split. O ganho é poder rodar remoto, servir desktop/web sem retrabalho, e debuggar separadamente.
- **OpenAPI-first + SDK gerado:** trate a API do core como contrato público; gere o client; force type-safety cross-process.
- **Directory-based multi-tenancy por header:** evita `/workspaces/:id/...` em toda rota e permite que um mesmo process atenda múltiplos projetos.
- **Dual eventing (pub/sub + event-sourced):** SSE para live UX, sync events para replay/share/dashboard.

---

## 2. Modelo de sessão e mensagens

### 2.1 Session

`src/session/index.ts:125-167` — `Session.Info`:

```ts
{
  id: SessionID,                // branded, descending ULID
  slug: string,
  projectID: ProjectID,
  workspaceID?: WorkspaceID,
  directory: string,            // cwd efetivo da session
  parentID?: SessionID,         // fork / subagent child
  title: string,                // auto via 'title' agent
  version: string,              // Installation.VERSION
  summary?: { additions, deletions, files, diffs? },
  share?: { url },
  time: { created, updated, compacting?, archived? },
  permission?: Permission.Ruleset,
  revert?: { messageID, partID?, snapshot?, diff? }
}
```

Tabela: `src/session/session.sql.ts:14-44` — drizzle sqliteTable em snake_case (ver §10). Cascade delete para mensagens/parts; JSON blobs para `revert`/`permission`/`summary_diffs`.

- **Fork:** `Session.fork(messageID)` — cria child que reusa o directory do parent; título `(fork #N)`.
- **Children:** `src/session/index.ts:448-458` — cascade recursiva por `parent_id`.
- **Auto-title:** `src/session/prompt.ts:187-248` — dispara no primeiro user message não-sintético, via `title` agent (modelo small, temperature 0.5, `<think>` stripado, limite 100 chars).

### 2.2 Mensagens v2 — parts como cidadão de primeira classe

`src/session/message-v2.ts` + `packages/opencode/specs/v2/message-shape.md`.

Mensagens separam **info** (User | Assistant) de **Parts** (lista heterogênea). Por que v2: o legacy `message.ts` tinha tool calls como `{ state: "call" | "partial-call" | "result" }` inline. V2 promove tudo (reasoning, snapshots, patches, sub-tasks) a Part, com discriminated unions zod.

**User** (`message-v2.ts:357-380`):
```ts
{ role: "user", time, format?, summary?, agent, model: { providerID, modelID, variant? },
  system?, tools?: Record<string, boolean> }
```

**Assistant** (`message-v2.ts:403-450`):
```ts
{ role: "assistant", time: { created, completed? },
  error?: discriminatedUnion(AuthError | APIError | OutputLengthError | AbortedError |
                             StructuredOutputError | ContextOverflowError | Unknown),
  parentID: MessageID, modelID, providerID, agent, path: { cwd, root },
  summary?: boolean,           // marca mensagem de compactação
  cost: number,
  tokens: { total?, input, output, reasoning, cache: { read, write } },
  structured?, variant?, finish? }
```

**Parts** (`message-v2.ts:87-401`) — tipos:

| type | conteúdo | uso |
|---|---|---|
| `text` | `{ text, synthetic?, ignored?, metadata? }` | texto de user/assistant; `synthetic` = injetado pelo sistema (compaction prompt etc) |
| `file` | `{ mime, filename?, url, source? }` | attachments; source rastreia origem (path/symbol/resource URI) |
| `tool` | `{ callID, tool, state: ToolState, metadata? }` | ver §2.3 |
| `reasoning` | `{ text, time: { start, end? }, metadata? }` | thinking |
| `snapshot` | `{ snapshot: string }` | hash git content-addressed |
| `patch` | `{ hash, files: string[] }` | descritor de patch git |
| `step-start` | `{ snapshot? }` | início de loop de tool execution |
| `step-finish` | `{ reason, snapshot?, cost, tokens }` | fim do loop; accounting |
| `compaction` | `{ auto: boolean, overflow? }` | marker synthetic de compactação |
| `retry` | `{ attempt, error, time }` | retry docs |
| `subtask` | `{ prompt, description, agent, model?, command? }` | delegação a sub-agent |
| `agent` | `{ name, source? }` | referência a agent instruction |

**ToolState** (discriminated por `status`, `message-v2.ts:273-350`):
```ts
pending   { input }
running   { input, time.start, title?, metadata? }
completed { input, output, title, metadata, time: { start, end, compacted? }, attachments?: FilePart[] }
error     { input, error, time: { start, end } }
```

`compacted: number` é o timestamp em que o output foi pruned (§2.5).

**Paginação** (`message-v2.ts:822-857`): cursor base64url de `{ id, time }`, ordem descending, tie-break por id. Fetch `limit+1` para detectar `hasMore`.

### 2.3 Pipeline tool → ToolPart

Tool retorna `{ title, metadata, output, attachments? }` (ver §3.1). O runner da session (`session/llm.ts`) serializa cada chamada em um `ToolPart` com `callID` único, state transitions em tempo real (running → completed/error), e publica `Bus.Event.Updated` por part.

### 2.4 Snapshot / revert / unrevert

- **Snapshot service** (`src/snapshot/index.ts:49-58`) — git-backed, gitdir isolado:
  ```ts
  interface Interface {
    track():     Effect<string | undefined>   // hash atual
    patch(hash): Effect<Patch>                 // descritor de patch
    restore(snapshot): Effect<void>            // restore a um hash
    revert(patches): Effect<void>              // undo patches
    diff(hash): Effect<string>                 // unified diff
    diffFull(from, to): Effect<FileDiff[]>
  }
  ```
  Implementação: `git --git-dir=.../snapshot/{projectID}/{worktreeHash}/ --work-tree=...` em dir paralelo (`src/snapshot/index.ts:88-92`). Não polui o `.git` do projeto do usuário.
- **Revert flow** (`src/session/revert.ts:42-91`):
  1. Itera mensagens para achar `(messageID, partID?)`.
  2. Guarda snapshot baseline em `session.revert.snapshot`.
  3. Se já havia revert anterior, restaura ao snapshot dele primeiro; depois reverte patches forward até o ponto desejado.
  4. Calcula diff + summary, publica `session.diff`.
- **Unrevert** (`revert.ts:94-102`) — restaura baseline e limpa flag.
- **Cleanup** (`revert.ts:104-145`) — deleta mensagens ≥ `revert.messageID` publicando `MessageV2.Event.Removed`.

**Decisão distintiva:** gitdir paralelo isolado → snapshots content-addressed sem tocar o git do user; revert reversível (stack de snapshots).

### 2.5 Compaction / overflow

`src/session/compaction.ts` + `overflow.ts`.

- **Overflow check** (`overflow.ts:8-22`): `count = tokens.total || sum(input+output+cache.read+cache.write)`; `usable = limit.input - reserved` (reserved default = `COMPACTION_BUFFER` ou `ProviderTransform.maxOutputTokens(model)`).
- **Thresholds** (`compaction.ts:35-37`):
  - `PRUNE_MINIMUM = 20_000` — abaixo disso, não pruna.
  - `PRUNE_PROTECT = 40_000` — mantém ≥40k tokens de tool output; pruna mais antigos.
  - `PRUNE_PROTECTED_TOOLS = ["skill"]` — nunca compacta output de skill.
- **Pruning** (`compaction.ts:93-120`): itera backward acumulando tokens; ao cruzar `PRUNE_PROTECT`, limpa `output` dos tool completions mais antigos e seta `time.compacted`. Input/metadata preservados.
- **Compaction part:** dispara agent `compaction` (hidden) que resume; o resultado vira `User` com `CompactionPart` + `text` de sumário, marcado `auto=true`.

**Decisão distintiva:** não resume o history inteiro cegamente — **pruna tool outputs** (lossy) e mantém metadata/intent. Skills preservados por design (são instruções caras de re-obter).

### 2.6 Share

`src/share/share-next.ts` + `src/share/share.sql.ts`.

Tabela `session_share` com `{ session_id, id, secret, url }`. Service:

```ts
interface Interface {
  url():                    Effect<string>
  create(sessionID):        Effect<Share>     // POST remoto, retorna URL pública
  remove(sessionID):        Effect<void>
}
```

Data types publicados (`share-next.ts:47-68`):
```ts
Data = { type: "session"|"message"|"part"|"session_diff"|"model", data: ... }
```

Publish via `ShareNext.sync` — eventos da session são espelhados em um backend remoto; leitura via `/share/{shareID}`.

### 2.7 System prompt assembly

`src/session/system.ts` + `prompt.ts` + `instruction.ts`.

- **Por-provider** (`system.ts:20-34`): switch no `model.api.id` → `PROMPT_BEAST` (o1/o3/gpt-4), `PROMPT_GPT`, `PROMPT_CODEX`, `PROMPT_GEMINI`, `PROMPT_ANTHROPIC`, `PROMPT_TRINITY`, `PROMPT_KIMI`, `PROMPT_DEFAULT`. Prompts vivem em `src/session/prompt/*.txt`.
- **Environment block** (`system.ts:36-61`): injeta cwd, worktree, git status, platform, date, opcionalmente tree da pasta.
- **Skills block** (`system.ts:63-76`): lista skills disponíveis para aquele agent (filtrado por permissão `skill`).
- **Instruction resolution** (`instruction.ts:54-231`): busca `AGENTS.md` / `CLAUDE.md` / `CONTEXT.md` walking upward; fallback a `~/.opencode/AGENTS.md`, `~/.claude/CLAUDE.md`; remote URLs via `config.instructions[]`.
- **Prompt mutators** (`prompt.ts:153-185`): detecta `@file.md` references no template, stat-checka, injeta como `FilePart` ou como `agent` reference.

### Aplicável a outros projetos
- **Parts heterogêneos com discriminated union:** migre de "mensagem com state inline" para lista de parts tipadas; desacopla storage, enables reasoning/snapshot/subtask nativos.
- **Snapshot via gitdir paralelo:** não suje o `.git` do user. Container seu próprio git store, versionado pelo próprio git.
- **Compactação lossy por output de tool (não resumo do history):** preserva intent; é mais barato e determinístico.
- **Prompt injection por modelo:** o que funciona em Claude não funciona em GPT-4o; mantenha famílias de prompt separadas.
- **Instrução hierárquica + per-message claim tracking:** evita reinjetar o mesmo AGENTS.md a cada turn.

---

## 3. Sistema de tools

### 3.1 Contrato de Tool

`src/tool/tool.ts:9-127`:

```ts
interface Def<Parameters extends z.ZodType, M extends Metadata> {
  id: string
  description: string              // carregado de .txt sidecar (ver §3.3)
  parameters: Parameters            // zod schema
  execute(args, ctx: Context): Promise<{
    title: string                  // UI-facing
    metadata: M
    output: string                 // texto principal (auto-truncado)
    attachments?: Omit<FilePart, "id"|"sessionID"|"messageID">[]
  }>
  formatValidationError?(err: ZodError): string
}

type Context<M> = {
  sessionID, messageID, agent, callID?, abort: AbortSignal,
  extra?, messages: MessageV2.WithParts[],
  metadata(input: { title?, metadata? }): void   // streaming UI updates
  ask(input: Omit<Permission.Request, "id"|"sessionID"|"tool">): Promise<void>
}
```

Regras chave aplicadas no `Tool.wrap()`:
1. **Validação zod first**; error formatter customizável.
2. **Auto-truncação** (`tool.ts:83-95`) via `Truncate.MAX_BYTES` / `MAX_LINES`. Seta `metadata.truncated=true` e grava output completo em `metadata.outputPath`.
3. **Streaming de progresso:** `ctx.metadata({ title, metadata })` permite update in-flight (ex: "[2/10] Reading foo.ts"), que o runner propaga como `Bus` event por part.

### 3.2 Built-ins

`src/tool/*.ts` — tools registradas:

| Tool | Arquivo | Nota |
|---|---|---|
| `bash` | `bash.ts` | tree-sitter (bash + powershell) para extração de paths; permission `bash` + `external_directory`; timeout configurável |
| `read` | `read.ts` | text/image/pdf dispatch por MIME; LSP warming; offset/limit; 2000 lines default |
| `edit` | `edit.ts` | **9 replacers em fallback chain**: simple, line-trimmed, block-anchor, whitespace-normalized, indentation-flexible, escape-normalized, trimmed-boundary, context-aware, multi-occurrence |
| `write` | `write.ts` | create/overwrite + LSP diagnostics post-write |
| `multiedit` | `multiedit.ts` | batch de edits sequenciais no mesmo file |
| `apply_patch` | `apply_patch.ts` | unified diff; usado em GPT (ver §3.4 model filtering) |
| `glob` | `glob.ts` | ripgrep glob, sort por mtime, limit 100 |
| `grep` | `grep.ts` | ripgrep full-text + multiline + context |
| `ls` | `ls.ts` | listing (mostly deprecated em favor de read em dir) |
| `codesearch` | `codesearch.ts` | Exa API (provider-gated: `opencode` ou `OPENCODE_ENABLE_EXA`) |
| `websearch` | `websearch.ts` | mesmo gating do codesearch |
| `webfetch` | `webfetch.ts` | HTTP fetch + streaming |
| `task` | `task.ts` | **sub-agent delegation** (ver §4.2) |
| `skill` | `skill.ts` | carrega skill markdown por nome |
| `todo` / `todowrite` | `todo.ts` | session-scoped TODOs |
| `question` | `question.ts` | interactive Q&A (flag-gated: client cli/app/desktop ou `OPENCODE_ENABLE_QUESTION_TOOL`) |
| `lsp` | `lsp.ts` | experimental (`OPENCODE_EXPERIMENTAL_LSP_TOOL`) |
| `plan_exit` | `plan.ts` | sai de plan mode (`OPENCODE_EXPERIMENTAL_PLAN_MODE`) |
| `invalid` | `invalid.ts` | handler para malformed tool calls |

### 3.3 `.txt` sidecars

Cada tool tem um `.txt` ao lado com sua description renderizada com placeholders (`${directory}`, `${os}`, `${shell}`, `${maxLines}`, etc). Carregados no init e injetados na description enviada ao modelo (`src/tool/registry.ts:209-216,465-470`).

**Decisão distintiva:** descriptions **não vivem em código**. Permite iteração sem rebuild, e comparar descriptions entre versões via git diff dos `.txt`.

### 3.4 Registry, filtering e plugin override

`src/tool/registry.ts:42-258`:

```ts
interface Interface {
  ids():     Effect<string[]>
  all():     Effect<Def[]>
  tools(model: { providerID, modelID, agent: Agent.Info }): Effect<Def[]>
  fromID(id): Effect<Def>
}
```

Fluxo de registro:
1. Built-ins hardcoded (`registry.ts:140-162`), init paralelo.
2. Custom tools: scan `{tool,tools}/*.{js,ts}` em config dirs; dynamic import wrapped (`registry.ts:112-125`).
3. Plugin tools: `plugin.list()` wrapped via `fromPlugin()` (`registry.ts:127-132`).
4. **Feature flags** gate `question`/`lsp`/`plan_exit`.

Filtragem por `(providerID, modelID, agent)`:
- `codesearch`/`websearch` → só `opencode` provider ou `OPENCODE_ENABLE_EXA`.
- **`apply_patch` vs `edit`/`write`**: modelos GPT (não OSS) recebem `apply_patch`; outros recebem `edit`+`write`. Evita mismatch de format preference.
- Plugin hook `tool.definition` permite mutar descriptions em runtime.
- `agent.permission` filtra tool availability via `tools: { name: false }` passados ao LLM.

### Aplicável a outros projetos
- **Descriptions em arquivos:** facilita A/B de prompts de tool sem rebuild; versiona naturalmente.
- **Tool filtering por modelo:** especialmente `apply_patch` vs `edit` — modelos têm preferências comprováveis; respeite-as.
- **`ctx.metadata()` streaming:** para tools longas (grep em repo grande), empurre progresso incremental pro client; melhora percepção de latência.
- **Auto-truncação com outputPath:** nunca estoure context window por acidente; mas preserve o full output em disco para inspeção.

---

## 4. Sistema de agentes / sub-agentes

### 4.1 Agent.Info

`src/agent/agent.ts:27-52`:

```ts
{
  name: string,
  description?: string,
  mode: "subagent" | "primary" | "all",       // visibilidade
  native?: boolean,                            // built-in
  hidden?: boolean,                            // ui visibility
  topP?, temperature?, color?,
  permission: Permission.Ruleset,              // core → define tool access
  model?: { modelID, providerID },
  variant?, prompt?,                           // system prompt override
  options: Record<string, any>,
  steps?: number                               // max loop iterations
}
```

Service (`agent.ts:54-66`):
```ts
{ get(name), list(), defaultAgent(), generate({ description, model? }) → { identifier, whenToUse, systemPrompt } }
```

### 4.2 Built-ins

`src/agent/agent.ts:107-233`:

- **`build`** (`mode:"primary"`) — default. Permissions full + `question:allow`, `plan_enter:allow`.
- **`plan`** (`mode:"primary"`) — edit negado globalmente exceto `.opencode/plans/*.md` e `data/plans/*.md`. Read-only exploration com exit ritualizado para build.
- **`general`** (`mode:"subagent"`) — só callable via `task`. `todowrite:deny`. Multi-step research.
- **`explore`** (`mode:"subagent"`) — whitelist restrita: glob, grep, list, bash, webfetch, websearch, codesearch, read. Custom `PROMPT_EXPLORE`.
- **`compaction`, `title`, `summary`** (`hidden:true, native:true`) — agentes internos para jobs específicos; permissions deny-all exceto o que precisam; `title` com temperature 0.5.

User-defined (`agent.ts:236-263`): definidos em `opencode.json` → `agent: { myname: { mode, permission, model, prompt, ... } }`. `Permission.merge()` compõe defaults + user config + agent-specific. `mode` default = `"all"`.

### 4.3 Task tool — sub-agent handoff

`src/tool/task.ts:15-154`:

```ts
parameters: {
  description: string              // 3-5 words
  prompt: string
  subagent_type: string
  task_id?: string                 // resume
  command?: string
}
```

Fluxo de `execute`:
1. `ctx.ask("task", ...)` — permission check, a menos que `ctx.extra.bypassAgentCheck`.
2. Cria nova session child com `parentID: ctx.sessionID` (ou resume via `task_id`).
3. **Override de permission** do child: se subagent não tem `task`/`todowrite` allow, injeta deny rules — impede sub-subagent recursion sem policy explícita.
4. **Tool filtering:** calcula `tools: { [id]: boolean }` a partir do ruleset do subagent → desliga tools não permitidas no LLM request.
5. Model = subagent.model ?? parent.message.model.
6. Chama `SessionPrompt.prompt()` síncrono; espera resultado.
7. Retorna output em `<task_result>` XML wrapper + `task_id` para resume.

Parent tracking: parent session ganha `SubtaskPart` (`message-v2.ts:216-231`) com `{ prompt, description, agent, model?, command? }`. UI pode inline a sub-conversation.

**Decisão distintiva:** sub-agents são **sessions isoladas**, não contextos efêmeros. Isso dá:
- Permission enforcement natural (cada session tem seu ruleset).
- Resume de task antiga via `task_id` (continuar um general pelo ID anterior).
- Snapshot/revert por task.
- Cost accounting separado.

### Aplicável a outros projetos
- **Agent.permission como fonte única de tool access:** em vez de configurar tool list + policy em dois lugares, gere a tool list A PARTIR do permission ruleset.
- **Sub-agents como child sessions isoladas:** ganha dedup de estado, resume, e enforcement de policy out-of-the-box.
- **Hidden "native" agents (title, compaction, summary):** jobs internos não precisam poluir o UI picker; use o mesmo sistema de agent definition pro pipeline interno.

---

## 5. Providers de LLM

### 5.1 Abstração

- **Base:** Vercel AI SDK (`@ai-sdk/*`) — opencode usa `streamText()` / `generateText()` como runtime, mas **não** expõe o AI SDK diretamente; embrulha em `Provider.Model` + `ProviderTransform`.
- **Enum** (`src/provider/schema.ts:6-27`): `ProviderID` branded — `opencode | anthropic | openai | google | google-vertex | github-copilot | amazon-bedrock | azure | openrouter | mistral | gitlab`.
- **Bundled providers** (`src/provider/provider.ts:127-150`): factories diretamente importadas, inclui Anthropic, OpenAI, Google (GenAI + Vertex), Azure, Bedrock, OpenRouter, X.AI, Mistral, Groq, DeepInfra, Cerebras, Cohere, TogetherAI, Perplexity + custom `createGitHubCopilotOpenAICompatible` e `gitlab-ai-provider`.

### 5.2 Model catalog (models.dev)

`src/provider/models.ts:16-150` — `ModelsDev` fetch remoto de `models.dev` API (5min TTL cache, fallback bundled snapshot).

```ts
Model {
  id, name, family, release_date,
  capabilities: { attachment, reasoning, temperature, tool_call, interleaved, reasoningPlacement? },
  cost: { input, output, cacheRead?, cacheWrite?, overage200k? },
  limit: { context, input?, output? },
  modalities: { input: [...], output: [...] },
  provider: { npm, endpoint },
  variants?: Record<string, options>
}
```

**Decisão distintiva:** catálogo remoto centralizado (`models.dev`). Novos modelos não exigem release do opencode.

### 5.3 Auth

`src/auth/index.ts:14-110` — Effect Schema classes:

```ts
Oauth  { type:"oauth", refresh, access, expires, accountId?, enterpriseUrl? }
Api    { type:"api", key, metadata? }
```

Persistência: `~/.opencode/auth.json` mode `0o600`.

`ProviderAuth` service (`provider/auth.ts:11-150`) expõe auth via plugin hooks:
```ts
{ methods(providerID): AuthMethod[]
  authorize(providerID, method): { url }
  callback(providerID, query): Auth }
```

Plugins como `CodexAuthPlugin`, `CopilotAuthPlugin`, `GitlabAuthPlugin`, `CloudflareWorkersAuthPlugin`, `PoeAuthPlugin` (internal — `plugin/index.ts:50-57`) contribuem auth methods próprios. **Auth é extensível por plugin** — o core não hardcoda fluxos OAuth provider-specific.

### 5.4 Transform

`src/provider/transform.ts`:

- **Normalização** (49-190): scrub tool IDs (Claude: alnum+`_-`; Mistral: 9-digit alnum); filter empty content para Anthropic/Bedrock; reordena tool messages (Mistral não aceita tool antes de user); extrai reasoning interleaved (`reasoning_content`/`reasoning_details`).
- **Prompt caching** (192+): aplica headers provider-specific — `anthropic.cacheControl = { type: "ephemeral" }`, OpenRouter equivalents, etc.
- **`sdkKey()`**: mapeia npm package → AI SDK provider-options key.

### 5.5 Stream orchestration

`src/session/llm.ts:80-350+` — `LLM.stream()`:

```ts
input: {
  user: MessageV2.User
  sessionID, model: Provider.Model, agent: Agent.Info,
  system: string[], messages: ModelMessage[], tools: Record<string, Tool>,
  retries?, toolChoice?: "auto"|"required"|"none"
}
```

Passos:
1. `Provider.getLanguage()` + `Auth.get()`.
2. Build system: agent prompt + user system + provider default. Dispara `experimental.chat.system.transform` plugin hook.
3. Merge options (provider / agent / variant). Dispara `chat.params` e `chat.headers` hooks.
4. Resolve tools; injeta `_noop` dummy se o proxy (LiteLLM etc) exige tools.
5. `streamText()` do AI SDK com wrapped model.
6. GitLab workflow models têm `toolExecutor` callback especial.

### 5.6 Retry

`src/session/retry.ts:1-120`:
- Exponential backoff: 2s inicial, factor 2x, max 30s (sem header).
- Respect: `Retry-After-Ms`, `Retry-After` (sec ou HTTP date), `retry-after-ms`.
- Retryable: rate-limit, overloaded. **Não** retry em context overflow (vai pra compaction).

### Aplicável a outros projetos
- **Catálogo de modelo externo:** evita shipar updates do agente por cada modelo novo. Se você hardcoda a lista de modelos, considere um serviço remoto cacheado.
- **Scrub de tool IDs provider-specific:** bugs sutis acontecem quando tool IDs não batem o regex do provider. Centralize no transform, não no tool runner.
- **Prompt caching headers por provider no transform:** não misture isso com a lógica de orchestration.
- **Plugin-extensible auth:** se você quer suportar login flows customizados (enterprise SSO, tokens locais), plugue a partir do core em vez de branching.

---

## 6. Permissões e segurança

### 6.1 Modelo

`src/permission/schema.ts` + `index.ts`:

```ts
Action = "allow" | "deny" | "ask"

Rule = { permission: string, pattern: string, action: Action }
Ruleset = Rule[]

Request {
  id: PermissionID, sessionID,
  permission: string,             // tool id (pode ser "*", "bash", "external_directory", ...)
  patterns: string[],              // paths/comandos sendo acessados
  metadata: Record<string, any>,   // diff, diagnostics, cwd, ...
  always: string[],                // patterns pra auto-allow se reply="always"
  tool?: { messageID, callID }
}

Reply = "once" | "always" | "reject"
```

**Três tiers:** `allow` (passa), `ask` (prompt UI → Reply), `deny` (throw `DeniedError`). Rule matching = wildcard glob em ambos campos, **last match wins** (`src/permission/evaluate.ts:9-15`). Default quando nada casa: `ask`.

### 6.2 Fluxo

`src/permission/index.ts:140-260`:

`Permission.ask({ ruleset, permission, patterns, always, metadata })`:
1. Para cada pattern, `evaluate(permission, pattern, ruleset, approved)` → action.
2. Qualquer `deny` → throw `DeniedError` (falha a tool imediatamente).
3. Se todos `allow`, retorna.
4. Senão, cria `Request` com id, publica `Permission.Event.Asked` no bus, bloqueia em `Deferred.await()`.
5. `Permission.reply({ requestID, reply, message? })`:
   - `reject` → falha TODAS as pending requests da mesma session (`Deferred.fail` → `RejectedError`).
   - `once` → succeed esta.
   - `always` → succeed + push `{permission, pattern, action:"allow"}` em `approved[]`. **E re-avalia outras pending requests** da mesma session: as que ficam totalmente `allow` são auto-aprovadas.

`approved[]` é session-scoped (in-memory). Rules persistentes vivem em config ou em `PermissionTable`.

### 6.3 Default rulesets e enforcement

`src/agent/agent.ts:86-103` — defaults aplicados a todos agents:
```ts
"*": "allow",
doom_loop: "ask",
external_directory: { "*": "ask", ".opencode/*": "allow", ... },
question: "deny", plan_enter: "deny", plan_exit: "deny",
read: { "*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow" }
```

**plan** (`agent.ts:127-143`): `edit: { "*": "deny", ".opencode/plans/*.md": "allow", ... }`.

**explore** (`agent.ts:162-180`): deny-all + whitelist `grep/glob/list/bash/webfetch/websearch/codesearch/read`.

### 6.4 Bash arity — granularidade de comando

`src/permission/arity.ts:1-162`:

```ts
ARITY = {
  "git": 2,            // ["git","checkout","main"] → ["git","checkout"]
  "npm run": 3,
  "docker compose": 3,
  "terraform": 2,
  ...
}
```

`prefix(tokens)` acha o maior prefix registrado e slice por arity. Usado em `bash.ts:265` para gerar `always` pattern tipo `git checkout *` — permite user dizer "always allow `git checkout *`" sem liberar o `git` inteiro.

**Decisão distintiva:** permission granularity para bash não é por comando literal (frágil) nem por binário (burro); é por **canonical prefix** semântico.

### 6.5 Extração de paths em bash

`tool/bash.ts:241-270` — tree-sitter AST walk:
- Parse command tree → extrai command nodes.
- Path args (respeitando flags `-path`, `-destination`, etc.).
- Expansão de `$VAR`, `~`, `${env:VAR}`.
- `cygpath` para POSIX shells em Windows.
- Marca como "external" se fora de `Instance.directory`.

Isso alimenta dois permission asks: `external_directory` (se paths fora do project) e `bash` (comando canônico).

### Aplicável a outros projetos
- **Permission as declarative data, não código:** rulesets são comparáveis, serializáveis, configuráveis, e habilitam diff/review de policy.
- **Três tiers allow/ask/deny:** `ask` + `Reply=always` é o que faz o sistema ser usável — user aprova uma vez, sistema lembra via auto-extend do approved[].
- **Re-avaliação de pending após `always`:** se o user aprova "sempre `edit src/**`" no meio de um batch, as outras pending da mesma session resolvem sozinhas. UX crítico.
- **Arity semântico para bash:** compare seu modelo — prefix matching com tabela de arity é prático e auditável.
- **Tree-sitter para extração de paths de comandos:** mais robusto que regex; vale o custo.

---

## 7. Integração com o ambiente

### 7.1 LSP

`src/lsp/` (`index.ts`, `server.ts`, `client.ts`, `launch.ts`):

- Servidores configurados via `lsp` config; built-ins para Pyright, typescript-eslint; experimental Ty (`OPENCODE_EXPERIMENTAL_LSP_TY`).
- `LSP.spawn()` sobe server stdio-transport.
- **LSP warming** em `read` tool — ao ler um file, `touchFile()` dispara o LSP daquela linguagem.
- Diagnostics surfacing:
  - Em `write`/`edit` — após a mutação, coleta diagnostics e inclui no output.
  - Via tool `lsp` dedicada (experimental).

### 7.2 MCP

`src/mcp/` + `src/server/routes/mcp.ts`:

**Config** (`config/config.ts:373-434`):
```ts
McpLocal  { type:"local", command: string[], environment?, enabled?, timeout? }
McpRemote { type:"remote", url, headers?, oauth?: McpOAuth | false, timeout? }
```

**Tool conversion** (`mcp/index.ts:133-161`):
```ts
convertMcpTool(mcpTool) → dynamicTool({
  description: mcpTool.description,
  inputSchema: jsonSchema(schema),   // MCP schema → AI SDK jsonSchema
  execute: (args) => client.callTool({ name, arguments })
})
```

MCP tools são **merged no mesmo tool registry** dos built-ins — opacas para o LLM. Service expõe `tools()`, `prompts()`, `resources()`.

**OAuth:** `McpAuth` + `McpOAuthProvider`. Routes `/mcp/:name/auth/start`, `/callback`, `/authenticate` (`server/routes/mcp.ts:64-177`). Commands MCP prompts viram `Command.Info` (§8.3).

### 7.3 Shell / PTY

- **PTY abstraction** (`src/pty/pty.ts:1-26`): `Proc { pid, onData, onExit, write, resize, kill }`.
- **Implementações:** `pty.node.ts` + `pty.bun.ts` via native bindings.
- **Bash tool:** tree-sitter parsing + permission scan + timeout (default 2min, override `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS`) + path expansion + Windows/POSIX shell detection + metadata cap 30KB.
- Rotas `/pty/*` para clients abrirem terminais interativos no processo servidor.

### 7.4 Git e Worktree

`src/git/index.ts:6-200+` — Effect-based, roda git com flags: `--no-optional-locks`, `core.autocrlf=false`, `core.symlinks=true`, `core.longpaths=true`. API: `run`, `branch`, `defaultBranch`, `status`, `diff`, `mergeBase`, `show`.

`src/worktree/index.ts:24-200+` — `Worktree` service:
- `create()` — gera nome, `git worktree add`, opcional start command.
- `remove()` — clean + parse warnings do git.
- `reset()` — hard reset a branch head.
- Events `worktree.ready` / `worktree.failed` via bus.

**Uso:** worktrees são o vetor de isolamento para multi-session (cada session pode viver em um worktree dedicado para evitar lock contention / branch switching).

### 7.5 IDE / Desktop

- `src/ide/` detecta IDE pelo env (`TERM_PROGRAM=vscode`, `GIT_ASKPASS`). Suporta Windsurf, VS Code / Code Insiders, Cursor, VSCodium.
- Extension install: `code --install-extension sst-dev.opencode`.
- `sdks/vscode/` — extension que usa o SDK JS contra o server local.
- `packages/desktop/` (Tauri) e `packages/desktop-electron/` — wrappers que spawn o server e rendem o app.

### 7.6 File watching / sync

`src/file/watcher.ts:23-171` — `@parcel/watcher` (native binaries por platform: win native, macOS fs-events, linux inotify).
- Respeita `.gitignore`, config ignores, protected paths.
- Publica `FileWatcher.Event.Updated` no bus (add/change/unlink).
- Git directory monitorado separadamente (para detectar commit/branch switch).

### Aplicável a outros projetos
- **MCP tools como first-class tools:** não crie um path "MCP" separado — wrap e merge no registry. O modelo não deve saber que é MCP.
- **LSP warming implícito em read:** antes que o modelo peça diagnostics, a stack já aqueceu — reduz latência de `lsp` tool.
- **Worktree por session:** se você tem múltiplas sessions concorrentes mexendo em branches diferentes, worktree é a primitive certa (em vez de pular branches).
- **PTY abstraction separada de tool bash:** facilita cliente querer PTY direto (attach, replay) sem passar pelo LLM.

---

## 8. Extensibilidade

### 8.1 Plugins

`src/plugin/` (`index.ts`, `loader.ts`, `install.ts`, `meta.ts`):

**Contrato** (`plugin/index.ts:21-260+`):
```ts
type PluginModule = (input: PluginInput, options?: any) => Promise<Hooks>

interface Hooks {
  "experimental.chat.system.transform"?: (input, output: {system: string[]}) => Promise<void>
  "chat.params"?: (input, output: { temperature?, topP?, ... }) => Promise<void>
  "chat.headers"?: (input, output: { headers }) => Promise<void>
  "tool.definition"?: (input, output) => Promise<void>
  "event"?: (input: { event }) => void
  "auth"?: { provider?, methods, ... }
  "config"?: (cfg: Config.Info) => void | Promise<void>
}
```

Hooks são "trigger style": chamados em sequência, cada um muta o `output` (conceito de reducer-like shared mutable output).

**Internal plugins** (`plugin/index.ts:50-57`): CodexAuthPlugin, CopilotAuthPlugin, GitlabAuthPlugin, PoeAuthPlugin, CloudflareWorkersAuthPlugin, CloudflareAIGatewayAuthPlugin.

**External loader** (`plugin/loader.ts`): stages install → compat check → entry resolve → load. Metadata em `plugin-meta.json` com spec, source (npm/file), version, fingerprint, mtime, load count. Spec formats: npm package string, `file://path`, tuple `[spec, options]`. `installDependencies()` garante `@opencode-ai/plugin` no `package.json` e roda `npm install`.

### 8.2 Skills

`src/skill/` + `src/tool/skill.ts`:

**Formato:** markdown com YAML frontmatter:
```md
---
name: my-skill
description: O que esta skill faz
---
# Conteúdo da skill...
```

**Discovery paths** (`skill/discovery.ts`):
- Global: `~/.claude/skills/`, `~/.agents/`
- Project: `.claude/skills/`, `.agents/`, `agents/`, `skills/`
- Config: `skills.paths[]`, `skills.urls[]`

**Service:** scans paralelo, parse frontmatter, dedup por nome.

**Tool integration** (`tool/skill.ts`): o tool `skill` carrega skill por nome, embebe conteúdo, sample related files (cap 10), inclui base dir URL para relative imports. O system prompt do agent lista skills disponíveis (ver §2.7).

**Decisão distintiva:** skills são **arquivos markdown puros**, descobríveis (não enumerados em config), e carregados **sob demanda** (não pre-injected no prompt). Reduz bloat de context e permite skills muito grandes.

### 8.3 Comandos customizados

`src/command/index.ts:14-195`:

```ts
Command.Info {
  name, description?, agent?, model?,
  source: "command" | "mcp" | "skill",
  template: string | Promise<string>,   // com placeholders $1 $2 $ARGUMENTS
  hints: string[]                        // placeholders detectados
}
```

Sources:
1. **Built-in:** `init` (gera AGENTS.md), `review`.
2. **Config `command`/`commands` folders:** markdown com frontmatter `template:`.
3. **MCP prompts:** cada MCP prompt vira um command com placeholders dos args.
4. **Skills:** reusadas por nome se não shadowed.

Hints extraction (`command.ts:53-61`): regex nos placeholders para alimentar UI de completion.

### 8.4 Config + hooks

`src/config/config.ts:45-600+` — merge precedente:
1. Managed macOS (`/Library/Managed Preferences/{domain}.plist`) — highest.
2. System: ProgramData (Win) / `/etc/opencode` (Linux).
3. User global: `~/.opencode/opencode.json` + `AGENTS.md` + `agent/` + `command/`.
4. Project local: `opencode.json`, `AGENTS.md`, `agent/`, `command/`, `plugin/`.
5. Env: `OPENCODE_*` vars override all.

**Agent config shape:**
```ts
Agent {
  model?, variant?, temperature?, top_p?, prompt?,
  permission?: Permission,   // read, edit, bash, lsp, skill, etc.
  options?, color?, steps?, mode?: "subagent"|"primary"|"all"
}
```

**Plugin spec** (`config.ts:319-356`): string (npm) | `file://` | `[spec, options]`. `resolvePluginSpec()` normaliza; `deduplicatePluginOrigins()` remove duplicatas por package name.

Hooks formais de lifecycle (no sentido de "run this command antes/depois de X") **não** têm um sistema dedicado em config — a extensão é via plugin hooks (§8.1). Eventos bus + plugin `event` hook é o substituto.

### Aplicável a outros projetos
- **Skills como MD discoverable:** baixa fricção (user só cria um MD e aparece); sob-demanda (não inflaciona prompt).
- **Plugin hooks como reducer sobre output:** contrato simples, encadeável, sem subscribers competindo.
- **Commands unificam config + MCP prompts + skills:** mesma UX de slash command independente da fonte.
- **Managed config via MDM/ProgramData:** se você tem usuários enterprise, honrar managed config (macOS preferences, win ProgramData) é free win.

---

## 9. Persistência e estado

### 9.1 Storage

- **Engine:** SQLite via Drizzle ORM (bun-sqlite).
- **Path** (`src/storage/db.ts:38-44`): `OPENCODE_DB` flag (`:memory:` ou path) → senão `opencode-${CHANNEL}.db` em `Global.Path.data` (`~/.local/share/opencode` no Linux).
- **Migrations:** `packages/opencode/migration/`, `drizzle.config.ts` aponta para `src/**/*.sql.ts`.

### 9.2 Tabelas principais

`session/session.sql.ts`:
- `session` (cascades → `message`, `part`, `permission`)
- `message` — index `(session_id, time_created, id)`
- `part` — index `(message_id, id)` + `session_id`; `data` é JSON blob (todos os tipos de Part caem num campo só)
- `permission` — cascades de `project`

Project e workspace têm suas próprias `*.sql.ts`.

**Distintivo:** Part storage é single-table JSON blob (não uma tabela por tipo). Paga custo de query-ability em troca de schema evolution trivial.

### 9.3 Hydration

`message-v2.ts:529-574` — query `messages WHERE session_id=?` → bulk `parts WHERE message_id IN (?)` agrupado por message. Evita N+1.

### 9.4 Snapshots

Ver §2.4: git paralelo em `~/.local/share/opencode/snapshot/{projectID}/{worktreeHash}/`.

### 9.5 Share persistence

`session/share.sql.ts` — tabela `session_share { session_id, id, secret, url, timestamps }`. Secret é HMAC key pra sync autenticada.

### 9.6 In-memory state

- Bus PubSubs (per-instance).
- Permission pending requests (Map por sessionID, Deferreds).
- Permission approved ruleset (session-scoped).
- Instance cache (por diretório, via ScopedCache).
- LSP clients, file watchers, PTY procs.

Quando `Instance` é disposed: Scope close → caches limpos, pending permissions auto-reject, LSP/PTY mortos. Tudo deterministicamente.

### Aplicável a outros projetos
- **SQLite + drizzle para agent state:** ideal pro perfil (single-user local, queries simples, portável). Ordens de magnitude mais rápido que file-per-session.
- **Parts como JSON blob em single table:** schema migration fica barato quando os tipos de part evoluem toda semana.
- **Scope-based cleanup:** amarrar recursos (LSP, PTY, watchers) a um scope Effect (ou equivalent) garante dispose correto sem manual bookkeeping.

---

## 10. Princípios de código e estilo

Extraídos de `AGENTS.md` (raiz) e `packages/opencode/AGENTS.md`:

### 10.1 Regras gerais
- **Bun first.** Usar `Bun.file()` etc. em vez de node.
- **Evitar `try`/`catch`** (deixa errors propagarem; use Effect ou Result types).
- **Proibido `any`**.
- **Type inference.** Não anotar explicitamente salvo necessário para export ou clareza.
- **Functional array methods** (flatMap/filter/map) > for loops; type guards em filters preservam inference.

### 10.2 Naming — MANDATÓRIA
- Single-word names por default para locals, params, helpers.
- Multi-word só quando single word é ambíguo.
- **Não** introduzir novos camelCase compostos quando um single-word é claro.
- Preferidos: `pid, cfg, err, opts, dir, root, child, state, timeout`.
- Evitar: `inputPID, existingClient, connectTimeout, workerPath`.
- Revisar linhas tocadas antes de finalizar edit e encurtar identifiers.

### 10.3 Outras convenções
- **Inline quando só usado 1x.** Reduz variable count.
  ```ts
  // Good
  const journal = await Bun.file(path.join(dir, "journal.json")).json()
  // Bad
  const journalPath = path.join(dir, "journal.json")
  const journal = await Bun.file(journalPath).json()
  ```
- **Nada de destructuring desnecessário.** `obj.a` > `const { a } = obj`.
- **`const` > `let`.** Ternário ou early return, sem reassignment.
- **Sem `else`.** Early return always.
- **Drizzle snake_case** sem string redundante:
  ```ts
  // Good
  { id: text().primaryKey(), project_id: text().notNull(), created_at: integer().notNull() }
  // Bad
  { id: text("id").primaryKey(), projectID: text("project_id").notNull() }
  ```
- **Testing:** sem mocks; teste implementação real; não duplicar lógica em tests. Tests não rodam de repo root (guard `do-not-run-tests-from-root`); rodar de package dir.
- **Typecheck:** sempre `bun typecheck` de package dir, nunca `tsc` direto.

### 10.4 Meta
- Default branch é `dev`.
- SDK JS: `./packages/sdk/js/script/build.ts` regenera a partir do OpenAPI.
- Paralelismo: "ALWAYS USE PARALLEL TOOLS WHEN APPLICABLE." — a própria instrução do codebase é reforço de design para bash/edit batch em paralelo.

### Aplicável a outros projetos
- **Naming enforcement no AGENTS.md:** se seu agente escreve código no seu repo, você controla o estilo dele **via seu próprio AGENTS.md**. Regras curtas, exemplos good/bad, checkpoint "review touched lines" são efetivos.
- **Snake_case + drizzle trick:** evita drift entre column name string e field name — pega de graça a economia de um bug frequente.
- **Sem mocks em tests:** vale re-examinar a cultura de testing do seu projeto à luz disso; mocks mascaram drift de schema, como o opencode afirma evitar.
- **Estilo single-word + inline:** não é fetiche — é redução de ruído em diffs, e em código revisado por agente, menos identifiers = menos superfície pro modelo se confundir.

---

## Apêndice A — Mapa rápido de arquivos de alto-leverage

| Tema | Arquivo(s) |
|---|---|
| Server boot | `src/server/server.ts`, `src/server/instance.ts`, `src/server/router.ts` |
| Routes | `src/server/routes/*.ts` |
| SSE eventing | `src/server/routes/event.ts`, `src/bus/index.ts`, `src/bus/bus-event.ts`, `src/bus/global.ts` |
| CLI | `src/index.ts`, `src/cli/cmd/*` |
| Project/workspace | `src/project/project.ts`, `src/project/instance.ts`, `src/project/bootstrap.ts`, `src/worktree/index.ts`, `src/control-plane/workspace.ts`, `src/control-plane/adaptors/index.ts` |
| Session | `src/session/index.ts`, `src/session/session.sql.ts`, `src/session/message-v2.ts`, `src/session/llm.ts`, `src/session/prompt.ts`, `src/session/system.ts`, `src/session/compaction.ts`, `src/session/overflow.ts`, `src/session/revert.ts`, `src/session/retry.ts`, `src/session/instruction.ts` |
| Snapshot | `src/snapshot/index.ts`, `src/patch/` |
| Tool contract | `src/tool/tool.ts`, `src/tool/registry.ts`, `src/tool/schema.ts` |
| Tools | `src/tool/{bash,edit,write,read,glob,grep,multiedit,apply_patch,task,skill,todo,webfetch,websearch,codesearch,lsp,plan,question,invalid,ls}.ts` + `.txt` sidecars |
| Agent | `src/agent/agent.ts`, `src/agent/prompt/` |
| Permission | `src/permission/{index,schema,evaluate,arity}.ts`, `src/server/routes/permission.ts` |
| Provider | `src/provider/{provider,auth,models,schema,transform}.ts`, `src/provider/sdk/` |
| Auth | `src/auth/index.ts` |
| MCP | `src/mcp/index.ts`, `src/mcp/auth.ts`, `src/server/routes/mcp.ts` |
| LSP | `src/lsp/{index,server,client,launch}.ts` |
| Plugin | `src/plugin/{index,loader,install,meta}.ts`, `packages/plugin/` |
| Skill | `src/skill/{index,discovery}.ts`, `src/tool/skill.ts` |
| Command | `src/command/index.ts` |
| Config | `src/config/config.ts` |
| Storage | `src/storage/db.ts`, `src/session/session.sql.ts`, `src/session/share.sql.ts`, `src/project/project.sql.ts` |
| File watching | `src/file/watcher.ts`, `src/filesystem/` |
| PTY | `src/pty/{pty,pty.node,pty.bun}.ts`, `src/server/routes/pty.ts` |
| Effect runtime | `src/effect/instance-state.ts` |
| SDK | `packages/sdk/openapi.json`, `packages/sdk/js/src/client.ts`, `packages/sdk/js/src/gen/` |

## Apêndice B — Decisões distintivas consolidadas

1. **TUI como client de um servidor HTTP** — core não renderiza; mesmo processo serve web/desktop/VSCode.
2. **OpenAPI-first com SDK gerado** — contrato single-source, tipos cross-process garantidos.
3. **Directory-based multi-tenancy por header** (`x-opencode-directory`) — evita path-based routing, permite um server hostear N projetos.
4. **Workspace adaptor pattern** — abstração genérica sobre worktree local / cloud / ssh sem tocar session logic.
5. **Parts v2 como discriminated union rica** (reasoning, snapshot, patch, subtask, compaction, retry, step-start/finish, agent) — escala de estado do agent sem hacks.
6. **Snapshot em gitdir paralelo** — reversível, content-addressed, não suja `.git` do user.
7. **Compactação lossy por tool output** (não resumo inteiro do history) — preserva intent, determinístico, barato.
8. **Tool descriptions em `.txt` sidecars** — iteração sem rebuild, diff natural de prompt changes.
9. **Agent.permission como fonte única de tool access** — tool availability derivada da policy, não declarada em dois lugares.
10. **Sub-agents como sessions isoladas com `task_id` resumível** — permission, cost, snapshot por sub-task grátis.
11. **Catálogo de modelos remoto via models.dev** — novos modelos sem release.
12. **Bash permission por arity semântica** — matching canonical (`git checkout *`) em vez de literal/por-binário.
13. **Tree-sitter para extração de paths em bash** — robusto o suficiente pra detectar `external_directory`.
14. **MCP tools merged no registry principal** — opacas para o LLM; mesma pipeline dos built-ins.
15. **Skills como MD discoverable carregado sob demanda** — zero bloat de context quando não usado.
16. **Plugin hooks "reducer sobre output"** — contrato simples, encadeável, sem competition entre subscribers.
17. **Managed config (MDM / ProgramData) no topo do merge order** — enterprise-grade config baseline.
18. **Effect + AsyncLocalStorage bridge** — runtime com scoped resources + cancelamento determinístico, bridgeável para código sync.
19. **Dual eventing (Bus + SyncEvent aggregate-sourced)** — live stream + replay no mesmo modelo.
20. **Naming mandate single-word no AGENTS.md** — estilo policiado pelo próprio agente, reduz ruído em diffs geradas por LLM.
