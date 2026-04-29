# Comparativo Técnico: Claude Code × OpenCode × Aru

> Avaliação aplicando os 10 critérios da skill `agentic-cli-comparator`,
> com peso de 15% para os cinco critérios core (1 a 5) e 5% para os cinco
> avançados (6 a 10). Análise qualitativa, sem notas numéricas — para evitar
> que pontuações antigas enviesem revisões futuras conforme as três
> ferramentas evoluem.
>
> **Fontes de evidência:**
> - Claude Code: `D:\OneDrive\Documentos\ts_projects\claude-code\` (versão pública distribuída como CLI da Anthropic; ~753 arquivos TS/TSX só sob `src/`).
> - OpenCode: `D:\OneDrive\Documentos\ts_projects\opencode\` (monorepo com `packages/opencode` como núcleo TS/Effect; ~301 arquivos TS apenas no core).
> - Aru: `D:\OneDrive\Documentos\python_projects\aru\` (Python 3.13+, Textual TUI, framework Agno; 85 arquivos Python sob `aru/` e 89 testes em `tests/`).

---

## 1. Critérios avaliados

| #  | Critério                                            | Peso |
|----|-----------------------------------------------------|------|
| 1  | Qualidade do agent loop e raciocínio                | 15%  |
| 2  | Integração com o ambiente de desenvolvimento        | 15%  |
| 3  | Controle, segurança e permissionamento              | 15%  |
| 4  | Gerenciamento de contexto e custo                   | 15%  |
| 5  | DX e ergonomia do workflow                          | 15%  |
| 6  | Suporte a modelos e flexibilidade de backend        | 5%   |
| 7  | Capacidades multi-agente e paralelismo              | 5%   |
| 8  | Qualidade do code editing                           | 5%   |
| 9  | Observabilidade e debugging do próprio agente       | 5%   |
| 10 | Extensibilidade e ecossistema                       | 5%   |

---

## 2. Análise por critério

### Critério 1 — Agent loop e raciocínio (peso 15%)

Claude Code domina aqui de forma clara. A pasta `src/services/compact/` traz
seis arquivos especializados (`microCompact.ts`, `autoCompact.ts`,
`postCompactCleanup.ts`, `compactWarningHook.ts`, `apiMicrocompact.ts`,
`sessionMemoryCompact.ts`) e o loop principal vive em `QueryEngine.ts`,
`Task.ts`, `coordinator/`, `query/`. Há recuperação de `max_tokens` em
múltiplas camadas, micro-compactação intra-turno seletiva por ferramenta, e o
sistema de _tasks_ (`src/tasks/`) suporta agentes locais e remotos com
notificação assíncrona. Tudo isso é forjado para sessões de horas em
codebases gigantes.

OpenCode estrutura o loop com `effect-ts` em `session/processor.ts` e
`session/llm.ts` — há `SessionRetry`, `SessionStatus`, `SessionSummary` como
serviços separados, anti-loop _doom-loop_ explícito (constante
`DOOM_LOOP_THRESHOLD = 3` em `processor.ts`) e overflow gating
(`session/overflow.ts`). É uma arquitetura sofisticada, mas com menos
heurísticas batalhadas em produção que a CC.

Aru implementa o essencial em `aru/runner.py`: recovery de `max_tokens` por
três tentativas com `_MAX_TOKENS_RECOVERY_PROMPT`, replay seguro de
`tool_use` órfãos, e o loop em si delega ao Agno. O `executor` itera plan
steps. **Sistema de tasks/planner completo:** `aru/tools/tasklist.py` traz
`create_task_list` (1-10 subtasks por fase, idempotente — chamadas
subsequentes substituem a lista) e `update_task` (status pending/in_progress/
completed/failed) com painel Rich live renderizado direto na ChatPane da
TUI. Plano macro tem `update_plan_step` (`tools/plan_mode.py`) integrado
com `enter_plan_mode`/`/plan` e flush coalescido pelo runner para evitar
re-render de plano stale após substituição mid-batch. Paridade real com
o sistema de tasks da CC mencionado acima. Falta ainda doom-loop detection
explícito (OpenCode tem `DOOM_LOOP_THRESHOLD = 3`) e _stop reason_
adaptativo por tipo de erro.

### Critério 2 — Integração com o ambiente (peso 15%)

Claude Code expõe ~40 ferramentas nativas (`src/tools/` lista
AgentTool/BashTool/FileEditTool/GlobTool/GrepTool/LSPTool/MCPTool/
NotebookEditTool/PowerShellTool/REPLTool/RemoteTriggerTool/ScheduleCronTool/
SkillTool/SyntheticOutputTool/TaskCreateTool/...). Suporte MCP completo
(`MCPTool/`, `McpAuthTool/`, `ListMcpResourcesTool/`, `ReadMcpResourceTool/`),
worktrees como ferramenta de primeira classe (`EnterWorktreeTool/`,
`ExitWorktreeTool/`), Jupyter (`NotebookEditTool/`), agendamento
(`ScheduleCronTool/`). Integração com IDE em `src/services/ide/` e
`hooks/useIDE*`.

OpenCode tem catálogo enxuto mas potente em `tool/` (apply_patch, bash,
codesearch, edit, glob, grep, lsp, multiedit, plan, question, read, skill,
task, todo, webfetch, websearch). MCP em `mcp/` com OAuth nativo
(`oauth-provider.ts`, `oauth-callback.ts`). LSP em `lsp/` próprio. Servidor
HTTP/WS embutido em `server/` com mDNS, expõe API REST via OpenAPI; SDKs JS
gerados em `packages/sdk`. Tem suporte ACP (Agent Communication Protocol) e
worktree em `worktree/`.

Aru cobre o essencial bem em `aru/tools/`: read/write/edit (single + batch),
glob/grep com fast-path ripgrep e fallback Python puro, bash com
background-process tracking e _taskkill_ no Windows, web_search/web_fetch,
delegate_task, MCP via gateway lazy (`tools/mcp_client.py` com circuit
breaker, _half-open retry_, dois modos eager/lazy), LSP próprio
(`aru/lsp/client.py` com 5 tools: definition/references/hover/diagnostics/
rename), apply_patch atômico com rollback, worktree git, AST tree-sitter para
Python. Falta paridade em Jupyter, scheduled cron e PowerShell dedicado.

### Critério 3 — Controle, segurança e permissionamento (peso 15%)

Claude Code tem `src/utils/permissions/` + `hooks/toolPermission/` +
`hooks/useCanUseTool.tsx`, modos `bypassPermissions`, `acceptEdits`, `default`,
`plan`, modo `swarm` separado (`useSwarmPermissionPoller.ts`), regras por
ferramenta com escopo, _allow rules_ por sessão/projeto. Plan mode é uma
ferramenta dedicada (`EnterPlanModeTool`/`ExitPlanModeTool`).

OpenCode usa `permission/{evaluate,arity,schema,index}.ts` com tipos
`Action = "allow" | "deny" | "ask"`, regras com pattern via `Wildcard`,
três classes de erro distintas (`DeniedError`, `RejectedError`,
`CorrectedError` com feedback), `Permission.fromConfig` permite expressão
declarativa, e há regras default no `agent.ts` com whitelist de
`external_directory` por skill. Modo de aprovação `once`/`always`/`reject`.

Aru tem `aru/permissions.py` (986 linhas) com paridade explícita: classes
`PermissionDenied`, `PermissionRejected`, `PermissionCorrected` (mirror direto
de OpenCode), tabela de regras por categoria (read/edit/write/bash/web_fetch/
delegate), patterns `fnmatch`, allowlist de ~40 comandos read-only, `*.env`
deny-by-default. `aru/tool_policy.py` (222 linhas) é o gate único que combina
plan-mode + skill `disallowed_tools` + `ALWAYS_ALLOWED_TOOLS` numa decisão
sem regras paralelas contraditórias. Modo YOLO, hook `permission.ask`
permite override por plugin. Ainda falta auditoria estruturada / replay
completo de decisões, e o modo _swarm_ (regras separadas por subagente)
está presente mas é menos rico que o da CC.

### Critério 4 — Gerenciamento de contexto e custo (peso 15%)

Claude Code é o estado da arte: micro-compactação seletiva por ferramenta
(constante `COMPACTABLE_TOOLS` no `microCompact.ts` lista exatamente as
mesmas tools que Aru espelha — read/grep/glob/bash/edit/write/webfetch/
websearch), compaction _time-based_ (`timeBasedMCConfig.ts`), proteção de
prompt cache (`promptCacheBreakDetection.ts`), pós-compact cleanup, AgentSummary
service para resumir trabalho longo.

OpenCode tem `session/compaction.ts` com `PRUNE_MINIMUM = 20_000` /
`PRUNE_PROTECT = 40_000` tokens, `PRUNE_PROTECTED_TOOLS = ["skill"]`,
`session/overflow.ts` calcula reserva dinâmica baseada em
`maxOutputTokens(model)`. Compactação automática gated por `cfg.compaction.auto`.
Há `summary.ts` para resumos hierárquicos.

Aru tem **três camadas** em `aru/context.py` e `aru/cache_patch.py`:

1. **Prune budget-based (paridade OpenCode):** `PRUNE_MINIMUM_CHARS = 80_000`
   (~20K tokens), `PRUNE_PROTECT_CHARS = 160_000` (~40K tokens),
   `PRUNE_PROTECTED_TOOLS = {"delegate_task"}`, prune-aware cache patching para
   preservar breakpoints Anthropic.
2. **Micro-compactação intra-turno (paridade CC):** `_prune_tool_messages` é
   chamada via patch em `Model.format_function_call_results` antes de cada
   round-trip de API; `COMPACTABLE_TOOLS` em `context.py` espelha exatamente o
   `microCompact.ts` da CC (read/write/edit/glob/grep/bash/web*). Métricas
   públicas (`get_microcompact_stats()` em `cache_patch.py:108`) expõem
   `invocations`, `clear_passes`, `results_cleared` para `/cost` calibrar.
   Comentário em `cache_patch.py:612`: "Intra-turn live session update".
3. **Recuperação reativa de overflow (vantagem sobre OpenCode):**
   `_aggressive_prune` é disparado por `_is_context_overflow_error` quando o
   provider rejeita a chamada por contexto cheio (`_patch_overflow_recovery`
   envolve `Model._ainvoke_with_retry` e `_ainvoke_stream_with_retry`). Mantém
   apenas os últimos 3 tool_results compactáveis e re-tenta uma vez. OpenCode
   **explicitamente não faz isso** — em `session/retry.ts:53` há o comentário
   "context overflow errors should not be retried". Suite em
   `tests/test_microcompact.py` cobre allowlist, métricas, prune agressivo e
   detecção de overflow (assinaturas de Anthropic/OpenAI/Groq/DashScope/DeepSeek).

Paridade real com OpenCode no budget-based prune, paridade real com CC na
micro-compactação intra-turno, e ainda uma camada reativa de overflow recovery
que nenhum dos dois rivais tem implementada. Ainda atrás da CC porque falta
time-based compaction (`timeBasedMCConfig.ts`), AgentSummary service para
resumos hierárquicos, e tracking de custo por ferramenta — esses seriam os
próximos passos para empatar.

### Critério 5 — DX e ergonomia (peso 15%)

Claude Code usa Ink (React no terminal) — visto em `src/ink.ts`, `main.tsx`,
~80 hooks em `src/hooks/` para virtualização, autocompletion, vim mode, voice,
notificações, copy-on-select, double-press detection, IDE integration. O
ecossistema de hooks é extraordinário (`useTextInput`, `useSearchInput`,
`useVirtualScroll`, `useTypeahead.tsx`, `useVimInput.ts`, `useVoice.ts`).
Configuração via `CLAUDE.md`/`settings.json`/hooks. Slash commands ricos
(120+ comandos em `src/commands/`). Streaming, retomada de sessão, fila de
prompts (`useCommandQueue.ts`).

OpenCode tem TUI no `cli/cmd/tui/app.tsx` (939 linhas — agora migrada para
Ink/TS, antes era Go/Bubbletea), com `dialog-*.tsx` para cada modal (agente,
comando, MCP, modelo, provider, sessão, skill, stash, status, tag, theme,
variant, workspace), interface gráfica Tauri em `packages/desktop/`,
plugins de TUI extensíveis em `feature-plugins/` (slots de UI). Configuração
via `opencode.json`/`AGENTS.md`/skills. Streaming via SSE/WebSocket no
servidor.

Aru tem TUI Textual (`aru/tui/app.py` — 2083 linhas), 9 widgets
(chat/completer/context_pane/header/inline_choice/loaded_pane/status/thinking/
tools), 4 screens modais (choice/confirm/search/text_input), keybindings
(Ctrl+Q/L/A/P/F + Up/Down history), slash commands locais sem round-trip,
`@file` mention com `SlashCompleter`, recovery de terminal pós-sleep
(Ctrl+R), shell escape (commit recente). Configuração via `aru.json` +
`AGENTS.md` + `.agents/commands/`. REPL clássico ainda disponível via
`--repl`. Funcional e bem implementado, mas ainda atrás dos dois rivais em
variedade de modos (sem Vim mode, voice, copy-on-select, virtualização
avançada, fila de comandos visível). A TUI é boa; o ambiente em volta dela
tem menos refinamento.

### Critério 6 — Modelos e flexibilidade (peso 5%)

Claude Code é fortemente acoplado ao Claude (lógico, é da Anthropic). Suporte
a outros providers existe via configuração custom (`provider/`), mas o
caminho dourado e os recursos de cache são otimizados para o stack
Anthropic.

OpenCode foi desenhado provider-agnostic desde o dia 1: `provider/transform.ts`,
`provider/v2/`, `models.dev` como registro de referência. Suporta dezenas de
providers e modelos via `ai-sdk`.

Aru tem `aru/providers.py` cobrindo Anthropic, OpenAI, Ollama, Groq,
OpenRouter, DeepSeek nativamente, com providers customizáveis via `aru.json`,
e `ReasoningConfig` provider-neutral resolvido para params específicos. A
documentação do `AGENTS.md` mostra suporte a Claude até 4-7, Sonnet 4-6 com
1M context window. Perto da paridade com OpenCode aqui, mas o catálogo é
menor e a abstração via Agno restringe extensão para providers exóticos.

### Critério 7 — Multi-agente e paralelismo (peso 5%)

Claude Code tem `AgentTool/runAgent.ts`, `forkSubagent.ts`,
`built-in/generalPurposeAgent.ts`, agentes em background com auto-background
após 120s, _teammates_ (`isTeammate`, `useTeammateViewAutoExit.ts`),
_swarms_ (`useSwarmInitialization.ts`, `useSwarmPermissionPoller.ts`,
`SendMessageTool` com mailbox por team — gated atrás de `isAgentSwarmsEnabled()`,
modo experimental, **não disponível no fluxo subagent default**), agentes
remotos (`RemoteAgentTask`, `teleportToRemote`), tasks paralelas
(`TaskCreateTool/`, `TaskListTool/`, `TaskOutputTool/`, `TaskStopTool/`),
modo coordinator (`coordinator/coordinatorMode.ts`), cap explícito de
concorrência (`services/tools/toolOrchestration.ts:8-12` define
`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` com default 10 + partição
read-only vs mutating em `partitionToolCalls`), e visualização live rica
(`components/tasks/BackgroundTaskStatus.tsx` com pills coloridas por agente,
`agentColorManager.ts`, e três dialogs especializados —
`AsyncAgentDetailDialog`, `InProcessTeammateDetailDialog`,
`RemoteSessionDetailDialog`).

OpenCode tem `session/processor.ts` orquestrando, `task.ts` como tool,
`Task.ts` como serviço, suporte a sub-sessões via `parentID` no schema,
modo `subagent`/`primary`/`all` no `agent.ts`. **Não tem** swarms,
auto-background, cap de concorrência (delega ao Vercel AI SDK), worktree
isolation por subagent, traces persistidos por subagent, ou comunicação
inter-worker. A visualização live é mínima: `cli/cmd/tui/component/subagent-footer.tsx`
só mostra um contador "X of Y siblings" quando o usuário navegou para um
subagent específico — não há painel global de workers ativos.

Aru tem `delegate_task` (`aru/tools/delegate.py`) com `MAX_SUBAGENT_DEPTH = 5`,
catálogo de subagents especializados em `aru/agents/catalog.py`
(explorer/verification/reviewer/guide/plan/executor/build, cada um com seu
próprio toolset e permissions via `mode="subagent"`), `fork_ctx()` para
isolamento por contextvars, traces persistidos em
`.aru/sessions/<id>/subagents/<task_id>/{trace.json,metadata.json}`,
inspeção via `/subagents` (árvore com tokens in/out, duration, status) e
`/subagent <id>` (trace detalhado).

**Fan-out paralelo via multi-call:** `aru/runner.py:340` documenta
"parallel tool execution" — múltiplas chamadas de `delegate_task` numa
mesma assistant message disparam concorrentemente via Agno tool batch.
Comportamento documentado para o modelo em `delegate_prompt.txt:16-19`
("Multiple `delegate_task` calls in a single message run concurrently").

**Isolamento por worker:** `worktree=branch_name` opcional dá um git
worktree dedicado a cada subagente (`delegate.py:230-246`); concorrentes
pedindo a mesma branch serializam num lock para a criação ser idempotente.
Cada worker recebe `fork_ctx()` (permissions, task_store, depth próprios).

**Background execution:** `run_in_background=True` em `delegate.py:162`
dispara `asyncio.create_task(_bg_wrapper())` na linha 561 e retorna
imediatamente um `bg-id`. A fila `pending_notifications` na `Session`
recebe o resultado; `drain_pending_notifications()` (linhas 102-124)
injeta um bloco `<task-notification task_id="...">...</task-notification>`
no próximo turno do orquestrador — paridade explícita com `shouldRunAsync`
da `AgentTool.tsx` da CC, citado no docstring. Comando `/bg` lista tasks
ativas. OpenCode não tem equivalente — seu `task.ts` é sempre bloqueante.

**Visualização live (gap fechado em `feat/swarm-agents`):** `aru/tui/widgets/subagent_panel.py`
mostra uma row por subagent ativo, com cor estável determinística por
`agent_name` (sha256 → palette de 8), ícone de status (`↻`/`✓`/`⊘`/`✗`),
tool em curso atualizada via dois novos eventos do bus (`subagent.tool.started`
/ `subagent.tool.completed`, emitidos do loop streaming em `delegate.py`),
e fade após 3s ao concluir. Hidden quando idle (`display: none` flip via
`-busy` class). Mais simples que `BackgroundTaskStatus.tsx` da CC
(verticais vs horizontal pills) mas materialmente equivalente em
informação. Acompanhado de fix dos paths de cancel/error que antes não
emitiam `subagent.complete` — bug latente que deixaria a row girando
para sempre.

**Não-gaps confirmados contra CC e OpenCode (correções de avaliações
anteriores):**

- _Reducer/aggregator estruturado:_ nenhum dos dois rivais tem isso. CC
  faz a "agregação" da `/batch` skill (`src/skills/bundled/batch.ts:78-87`)
  via regex sobre prosa do orquestrador; OpenCode (`tool/task.ts:138-144`)
  só empacota a saída em `<task_result>...</task_result>` por chamada.
  A síntese contextual feita pelo orquestrador (que tem a conversa toda)
  é estritamente superior a um reducer-agent built-in com prompt fixo.
- _Padrões hierárquicos formais:_ `coordinatorMode.ts:200-218` e a
  `/batch` skill da CC são **prompts**, não código. OpenCode só tem
  `mode: "subagent"|"primary"|"all"` em `Agent.Info`. Aru já replica esse
  padrão pelo catálogo + prompt do orquestrador. Não há `executeWorkflow({phases})`
  em nenhuma das três.
- _Comunicação inter-worker:_ existe na CC só no modo experimental "Agent
  Teams" (`SendMessageTool.ts` + mailbox em `teammateMailbox.ts`), gated
  atrás de `isAgentSwarmsEnabled()` em `AgentTool.tsx:262-264`. No fluxo
  subagent default, CC faz spawn → run → resultado → fim, exatamente como
  Aru e OpenCode. Migrar para peers persistentes não é "preencher gap" —
  é pivot arquitetural (daemon, mailbox, identity registry) sem demanda
  observável em CLI single-user.

**Gaps reais que ainda existem:**

- _Cap de concorrência:_ CC tem `runToolsConcurrently` com pool de
  geradores (`utils/generators.ts:32-46`) e default 10. Aru não. OpenCode
  também não. Hoje o risco no Aru é baixo (modelo raramente emite >5
  delegates/turn), mas vira problema com skills `/batch`-style. Custo de
  fechar é trivial (~20 LOC, opt-in via `ARU_MAX_PARALLEL_DELEGATES`).
- _Agentes remotos com teleport_ (`teleportToRemote`, `RemoteAgentTask`),
  _scheduled crons_ (`ScheduleCronTool`), _Agent Teams experimental_ —
  são features primeiras da CC sem demanda equivalente no Aru.

### Critério 8 — Qualidade do code editing (peso 5%)

Claude Code tem `FileEditTool` + `MultiEditTool` (não listada mas referenciada
nas constants), `apply_patch.ts` no OpenCode-like, validação de assinatura/
fingerprint via `FileTime` antes de editar, integração LSP para validar
sintaxe em tempo real.

OpenCode tem `tool/edit.ts`, `tool/multiedit.ts`, `tool/apply_patch.ts`,
validação de timestamp via `FileTime`, e integração com Truncate
(`tool/truncate.ts`) para outputs grandes.

Aru tem `edit_file`/`edit_files` (batch), `write_file`/`write_files`,
`apply_patch.py` atômico com rollback (Add/Update/Delete/Move), e
checkpoints em `aru/checkpoints.py` (snapshot pré-edição para `/undo`).
Falta validação de timestamp pré-edição (potencial race se outro processo
modifica o arquivo entre read e edit).

### Critério 9 — Observabilidade (peso 5%)

Claude Code tem `services/analytics/`, `internalLogging.ts`,
`diagnosticTracking.ts`, `useLogMessages.ts`, ant-trace
(`commands/ant-trace/`), heap dump (`commands/heapdump/`), debug-tool-call,
`debug` subcommand árvore (`commands/debug-tool-call/`), insights
(`insights.ts`), feedback (`commands/feedback/`). Display de tokens, custo,
agent state.

OpenCode tem `bus/` com event types Zod-tipados, `share/` para compartilhar
sessões com URL pública, `Bus.subscribe` para observar tudo, `cli/cmd/debug/`
com subcomandos para agent/config/file/lsp/ripgrep/skill/snapshot, `Log.create`
service-scoped, `cost-tracker.ts` no CC já existe; em OpenCode o tracking de
tokens vem do `MessageV2.Assistant.tokens` com input/output/cache.read/
cache.write separados. SDK gerado para integração externa.

Aru tem `aru/events.py` com schemas Pydantic tipados,
`plugin_manager.publish/subscribe`, hook `metrics.updated` para refresh
mid-turn de tokens/cost na TUI, `aru/sinks.py` separa REPL de TUI,
`StatusPane` mostra session/model/tokens/cost/mode, traces de subagente em
disco, `audit.log` em `.aru/`. Falta replay estruturado de sessões (CC tem
`useTeleportResume.tsx`, OpenCode tem share-link), métricas agregadas,
heap profiling, plugin error inspection limitada (só ring buffer de 50).

### Critério 10 — Extensibilidade e ecossistema (peso 5%)

Claude Code tem skills bundled em `src/skills/bundled/` + custom em
`loadSkillsDir.ts`, comandos custom em `~/.claude/commands/`, agentes custom
em `~/.claude/agents/`, hooks via `settings.json`, MCP servers, plugins
oficiais em `services/plugins/`, marketplace plugin recommendation
(`useOfficialMarketplaceNotification.tsx`), Slack/GitHub/Chrome integrations
(`commands/install-slack-app/`, `commands/install-github-app/`,
`commands/chrome/`). Documentação oficial bem mantida.

OpenCode tem package `plugin/` com tipo `Plugin` exportado, hooks ricos,
plugin system com TUI plugins (UI slots em `cli/cmd/tui/feature-plugins/`),
SDKs em múltiplas linguagens (`packages/sdk/js`, vscode em `sdks/`), Slack
app (`packages/slack/`), enterprise package, desktop electron, container
support, server modo daemon, infra IaC (`sst.config.ts`, `flake.nix`).

Aru tem skills em `.agents/skills/` ou `.claude/skills/`, custom commands
em `.agents/commands/`, custom tools em `.aru/tools/` ou `.agents/tools/`,
custom plugins em `.aru/plugins/`, sistema de instalação de plugins
inspirado no OpenCode (`plugin_cache.py` — install via github:user/repo,
git URL, file path, com manifest `aru-plugin.json` + semver via `engines.aru`,
file locks para concorrência), 28 hook events. Falta marketplace, SDK
externo, integrações third-party prontas (Slack/GitHub/IDE), documentação
ainda em fase de _internal-only_, comunidade pequena.

---

## 3. Análise em prosa

**Resumo qualitativo.** Claude Code lidera pela maturidade do agent loop, da
micro-compactação sofisticada e do volume bruto de capacidades — é uma
ferramenta que acumulou décadas-equivalentes de engenharia de produção em
pouco tempo. OpenCode encurta a distância no ecossistema (provider-agnóstico,
SDK, plugins de TUI, server HTTP) e em arquitetura limpa (Effect-TS, schemas
Zod em todo lugar). Aru fica atrás de forma honesta — é uma ferramenta jovem
construída com pequena equipe que escolheu, com sabedoria, replicar fielmente
as decisões boas dos dois rivais em vez de inventar; em gerenciamento de
contexto e em background execution já alcança ou supera OpenCode.

**Claude Code.** Ponto forte óbvio: profundidade. Cada feature tem
sub-features (compact tem 11 arquivos especializados, AgentTool tem
forkSubagent/agentMemory/runAgent separados, hooks são ~80). É a única
das três que tem _scheduled crons_, _remote agents_, _swarms_, _teammates_,
_voice_, _Chrome integration_ como recursos primeiros. Ponto fraco: é
fortemente acoplada a Claude/Anthropic e ao stack proprietário (Ink, build
opaco via `bun:bundle`, `feature()` flags), o que dificulta fork ou
extensão profunda. O custo por turno tende a ser maior porque o sistema
empurra mais contexto.

**OpenCode.** Ponto forte: arquitetura. Tudo é serviço Effect com layer
explícita, schemas Zod compartilhados entre server/SDK/cliente, MCP com
OAuth nativo, suporte ACP, server HTTP/WS embutido permite UIs externas
(Tauri desktop, VS Code extension). Provider-agnóstico de fato. Ponto fraco:
o agent loop é menos batalhado em produção que o da CC (sem swarms, sem
auto-background, sem retry reativo de overflow — `retry.ts:53` explicitamente
descarta `ContextOverflowError` da policy de retry); a documentação é boa
mas o ecossistema de plugins está nascendo.

**Aru.** Ponto forte: paridade explícita e econômica. O código é honesto
sobre de onde veio cada decisão (`# parity with OpenCode`, `# mirrors
opencode/permission/index.ts:83-103`, `# port of Claude Code's two-tier
strategy`). Permission system com 3 classes de erro tipadas, plan-mode
unificado num gate só, **três camadas de gerenciamento de contexto**
(prune budget-based no estilo OpenCode + micro-compactação intra-turno no
estilo CC + recuperação reativa de overflow que **nenhum dos dois rivais
implementa**), planner/tasklist completo com painel Rich live na TUI
(paridade com `src/tasks/` da CC), cache breakpoint patching, MCP gateway
com circuit breaker, AST tree-sitter, LSP próprio com 5 tools, plugin
install via git URL com manifest+semver. **Multi-agente sólido:**
`delegate_task` com fan-out paralelo via multi-call, `run_in_background=True`
com fila `<task-notification>` (paridade direta com `shouldRunAsync` da CC),
worktree isolation opcional por worker, catálogo de papéis especializados,
visualização live de paralelismo na TUI via `SubagentPanel` (gap fechado
em `feat/swarm-agents`). Tudo isso em ~85 arquivos Python. Ponto fraco: a
TUI é boa mas o ambiente em volta tem menos refinamento que CC/OpenCode
(sem virtualização avançada, sem voice, sem fila visível); o multi-agente
carece de cap de concorrência, agentes remotos e scheduled crons da CC;
ecossistema/integrações third-party praticamente inexistentes; documentação
é interna. É claramente a ferramenta mais nova das três, e isso aparece
nos critérios 5, 9 e 10.

**Diferenças críticas.** Onde os três mais se distanciam é no **critério 10
(extensibilidade)** — OpenCode tem SDK em JS, server público, marketplace
nascendo; Aru tem instalação de plugins via git mas sem ainda uma comunidade
ou marketplace. A segunda maior é no **critério 7 (multi-agente)** — Aru já
tem fan-out paralelo via multi-call de `delegate_task`, background execution
com notificação assíncrona, worktree isolation por worker, catálogo de papéis
especializados e visualização live (`SubagentPanel`); o gap remanescente é
cap de concorrência (real, ~20 LOC para fechar), agentes remotos com
teleport, scheduled crons e o modo Agent Teams experimental (gated por
`isAgentSwarmsEnabled()` na própria CC). Confirmamos por inspeção do fonte
que **reducer estruturado, comunicação inter-worker no fluxo default e
padrões hierárquicos formais NÃO são gaps** — nenhum dos dois rivais os
implementa de fato. A terceira maior diferença é no **critério 6
(flexibilidade de backend)**, onde OpenCode lidera por design e Aru se
aproxima por ser provider-agnóstico desde o início. No **critério 4
(gerenciamento de contexto)** Aru já tem paridade material com OpenCode e
ainda traz uma camada (overflow recovery reativa) que nenhum rival
implementou. E no **critério 1 (agent loop)** o sistema de planner/tasklist
(`create_task_list`/`update_task` + `update_plan_step` com painel Rich live)
traz paridade real com `src/tasks/` da CC, com o gap remanescente
concentrado em doom-loop detection e stop_reason adaptativo.

**Recomendação contextual.** Se você está num time pequeno trabalhando em
um projeto Anthropic-friendly e quer a ferramenta mais polida e produtiva
hoje, **Claude Code** vence sem discussão. Se você precisa de provider
flexibility (rodar local com Ollama, alternar entre Claude/GPT/Gemini por
custo), de uma arquitetura limpa para integrar num produto seu, ou se quer
hospedar internamente com server, **OpenCode** é a escolha. Se você quer
**hackear, contribuir e moldar** a sua própria ferramenta agentic em Python
— com o melhor das outras duas como referência viva no código — ou se você
precisa rodar dentro de um stack Python existente (Agno, integrações
internas), **Aru** entrega a maior parte do valor da CC com fração da
complexidade, incluindo features que a OpenCode escolheu não ter (overflow
recovery reativa, background execution). Para quem prioriza maturidade da
CC + custo mais baixo, OpenCode é o ponto-doce; para quem prioriza
customização total + resiliência de contexto sob pressão, Aru é a melhor
base de partida.

---

## 4. Notas de rodapé

- **Versões observadas:** Aru `0.48.0` (branch `feat/swarm-agents`), Claude
  Code (versão pública distribuída como CLI; sem tag aparente no tree),
  OpenCode (sem tag inspecionada; o package `desktop` está em `1.3.17`).
- **Nenhuma busca web foi necessária** — as três ferramentas estão presentes
  como código-fonte completo no disco. As avaliações se baseiam em leitura
  direta de implementação, não em material promocional.
- **Onde Aru já tem paridade real:** permissões (3 classes tipadas como
  OpenCode), prune/compact budget-based (mesmos thresholds do OpenCode),
  micro-compactação intra-turno com allowlist `COMPACTABLE_TOOLS` e métricas
  `get_microcompact_stats()` (paridade com `microCompact.ts` da CC),
  planner/tasklist completo (`create_task_list`/`update_task` +
  `update_plan_step` com painel Rich live na TUI, paridade com `src/tasks/`
  da CC), MCP gateway com circuit breaker, plugin install com manifest
  semver, hooks lifecycle (28 eventos — `subagent.tool.started`/`.completed`
  adicionados em `feat/swarm-agents`),
  `delegate_task(run_in_background=True)` com fila `pending_notifications` e
  injeção `<task-notification>` (paridade direta com `shouldRunAsync` da CC),
  fan-out paralelo via multi-call, worktree isolation opcional por subagente,
  e visualização live de paralelismo via `SubagentPanel` na TUI (paridade
  funcional com `BackgroundTaskStatus.tsx` da CC, ainda mais que OpenCode
  cujo `subagent-footer.tsx` só mostra contador de siblings).
- **Onde Aru está atrás:** cap de concorrência opt-in (CC tem `getMaxToolUseConcurrency`
  default 10 + partição read-only/mutating), agentes remotos com teleport
  (`RemoteAgentTask`/`teleportToRemote`) e scheduled crons (`ScheduleCronTool`)
  da CC, modo Agent Teams experimental da CC (`SendMessageTool` + mailbox,
  gated por `isAgentSwarmsEnabled()`), time-based compaction e AgentSummary
  hierárquico (CC), doom-loop detection explícito (OpenCode tem
  `DOOM_LOOP_THRESHOLD = 3`), ecossistema externo (SDK, marketplace,
  integrações), modos de input avançados (vim, voice, virtualização avançada),
  tracking de custo por ferramenta.
- **Não-gaps confirmados (correção de análises anteriores):**
  reducer/aggregator estruturado, comunicação inter-worker no fluxo default
  e padrões hierárquicos formais foram inicialmente listados como gaps, mas
  inspeção dos fontes mostrou que **nenhum dos dois rivais os implementa de
  fato** — CC só tem comunicação inter-worker no modo Agent Teams
  experimental gated, e tanto CC quanto OpenCode codificam padrões
  hierárquicos via prompt (não código). Manter esses itens como "atrás"
  era ruído.
- **Onde Aru está à frente:** **recuperação reativa de overflow** —
  `_aggressive_prune` em `cache_patch.py` é disparado por
  `_is_context_overflow_error` quando o provider rejeita a chamada por
  contexto cheio, mantém apenas os últimos N tool_results compactáveis e
  re-tenta uma vez. OpenCode explicitamente **não faz isso**
  (`session/retry.ts:53` descarta `ContextOverflowError` da policy de
  retry); CC trata overflow no caminho de compact mas sem o fallback
  reativo idêntico. Outro ponto: densidade de código (~85 arquivos vs 301
  de OpenCode core vs 753 de CC `src/` apenas) — não é mérito por si só,
  mas significa que cada arquivo carrega responsabilidade clara e a base é
  fácil de navegar. E documentação interna excelente (`AGENTS.md` com 407
  linhas e mapeamento exaustivo).
