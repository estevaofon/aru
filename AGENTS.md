# Aru — AI Coding Assistant

Aru is a multi-agent CLI coding assistant supporting multiple LLM providers (Anthropic, OpenAI, Ollama, Groq, OpenRouter, DeepSeek) via the Agno framework. It provides an interactive REPL where users describe tasks in natural language, and agents plan and execute code changes using a composable tool set (17 tools in the full set: 13 core + 4 task-management).

## Architecture

```
main.py → cli.run_cli() → REPL loop
                             ├─ Build Agent     (conversation + full tool set, primary)
                             ├─ Plan Agent      (read-only, structured plan output)
                             ├─ Executor Agent  (runs each plan step via runner)
                             └─ Explorer Agent  (subagent spawned via delegate_task)
```

Agents are described by `AgentSpec` entries in `agents/catalog.py` and instantiated lazily via `agent_factory.create_agent_from_spec`. All agents stream responses through Agno's `Agent` class.

## Project Structure

```
aru/
├── cli.py              # Main REPL loop, argument parsing, entry point
├── agent_factory.py    # Agent instantiation from AgentSpec (catalog-driven)
├── runtime.py          # RuntimeContext via contextvars; fork_ctx() for sub-agents
├── runner.py           # Agent execution orchestration with streaming
├── session.py          # Session state, persistence, plan tracking
├── commands.py         # Slash commands, help display, shell execution
├── completers.py       # Input completions, paste detection, @file mentions
├── context.py          # Token optimization (pruning, truncation, compaction)
├── cache_patch.py      # Prune-aware cache boundary patching (Anthropic cache breakpoints)
├── history_blocks.py   # Conversation history block helpers
├── checkpoints.py      # Pre-edit file checkpoints for undo support
├── display.py          # Terminal display (logo, status bar, streaming output)
├── config.py           # Loads AGENTS.md, .agents/commands/, .agents/skills/
├── providers.py        # Multi-provider LLM abstraction (anthropic, openai, ollama, groq, etc.)
├── permissions.py      # Granular permission system (allow/ask/deny per tool+pattern)
├── agents/
│   ├── base.py         # Shared instruction templates (BASE_INSTRUCTIONS, roles)
│   ├── catalog.py      # AgentSpec catalog — build/plan/executor/explorer specs
│   └── planner.py      # Plan reviewer — one-shot scope check, no tools
├── plugins/
│   ├── __init__.py     # Public API: tool, Hooks, HookEvent, PluginInput
│   ├── tool_api.py     # @tool decorator for custom tools
│   ├── custom_tools.py # Discovery, loading, and registration of custom tool files
│   ├── hooks.py        # Hook system: Hooks, HookEvent, PluginInput
│   └── manager.py      # PluginManager — loads plugins, fires hooks
└── tools/
    ├── codebase.py     # Compat shim — re-exports from the modules below
    ├── _shared.py      # Cross-cutting helpers (notify mutation, thread_tool, truncate_output)
    ├── _diff.py        # Unified-diff rendering for permission prompts and LLM context
    ├── file_ops.py     # read / write / edit / list / get_project_tree (+ async wrappers)
    ├── search.py       # glob / grep (ripgrep fast path + pure-Python fallback)
    ├── shell.py        # bash / run_command / background process tracking
    ├── web.py          # web_search / web_fetch / HTML-to-text
    ├── delegate.py     # delegate_task, sub-agent lifecycle, set_custom_agents
    ├── registry.py     # Tool set composition, TOOL_REGISTRY, resolve_tools, MCP gateway loader
    ├── tasklist.py     # create_task_list / update_task / update_plan_step
    ├── plan_mode.py    # enter_plan_mode tool — autonomous counterpart to /plan
    ├── mcp_client.py   # MCP server gateway for external tool integration
    ├── ast_tools.py    # Tree-sitter Python AST analysis (classes, functions, imports)
    ├── ranker.py       # Multi-factor file relevance scoring
    └── gitignore.py    # .gitignore-aware file filtering with caching
```

## Key Modules

### CLI Modules (refactored from `cli.py`)

- **`cli.py`**: Entry point (`run_cli()`, `run_oneshot()`), main async REPL loop, argument parsing, non-interactive mode
- **`agent_factory.py`**: `create_agent_from_spec(AgentSpec, ...)` — builds Agno `Agent` from a catalog spec, wires tools, permissions, hooks
- **`runtime.py`**: `RuntimeContext` held in `contextvars`; `fork_ctx()` clones for sub-agents with fresh task store and read cache
- **`runner.py`**: Agent execution orchestration with live streaming and plan step tracking
- **`session.py`**: Session state (conversation history, plan tracking, model selection, token metrics). Persisted as JSON in `.aru/sessions/`
- **`commands.py`**: Slash command definitions, help display, shell execution, user prompts
- **`completers.py`**: Input completions, paste detection, `@file` mention resolution
- **`context.py`**: Token optimization — pruning, truncation, and compaction of conversation history
- **`cache_patch.py`**: Prune-aware cache boundary patching so Anthropic cache breakpoints survive pruning (parity with OpenCode)
- **`history_blocks.py`**: Helpers for locating/manipulating conversation blocks during pruning and compaction
- **`checkpoints.py`**: Tracks pre-edit snapshots of mutated files for undo support
- **`display.py`**: Rich-based terminal rendering (`StreamingDisplay`, `StatusBar`, logo)

### `config.py` — Project Configuration

Loads project-level customization into an `AgentConfig` object:
- `AGENTS.md` → extra instructions appended to all agent prompts
- `.agents/commands/*.md` → custom slash commands (filename = command name)
- `skills/<name>/SKILL.md` → custom skills (agentskills.io format, searched in `.agents/`, `.claude/`, `~/.agents/`, `~/.claude/`)

### `providers.py` — Multi-Provider LLM

Abstracts model creation across Anthropic, OpenAI, Ollama, Groq, OpenRouter, DeepSeek. Custom providers configurable via `aru.json`.

### `permissions.py` — Permission System

Granular per-tool rules with three outcomes: `allow`, `ask`, `deny`. Configured in `aru.json` under `permission` with per-category patterns. Safe command prefixes whitelist ~40 read-only shell commands as defaults. Sensitive files (`*.env`) denied by default.

### `agents/catalog.py` — Agent Catalog

Single source of truth for native agents. Each entry is an `AgentSpec` with a lazy `tools_factory` so tool-module import order does not matter:

| Spec key | Role | Mode | Tool set | Max tokens |
|----------|------|------|----------|------------|
| `build` | general | primary | `GENERAL_TOOLS` (17) | 8192 |
| `plan` | planner | primary | `PLANNER_TOOLS` (5) | 4096 |
| `executor` | executor | primary | `EXECUTOR_TOOLS` (17) | 8192 |
| `explorer` | explorer | subagent | `EXPLORER_TOOLS` (7, small model) | 4096 |

Custom agents defined via `.agents/agents/*.md` take a separate path through `create_custom_agent_instance` and are not listed in the catalog.

### `agents/planner.py` — Plan Reviewer

One-shot scope check run after the plan agent produces a draft. No tools, no loop. Trims scope-creep steps while preserving the original plan text verbatim.

### `tools/codebase.py` + submodules — Core Tools + Tool Sets

`codebase.py` is a thin compat shim. The implementations live in:

- `tools/file_ops.py` — `read_file`, `read_files`, `write_file(s)`, `edit_file(s)`, `list_directory`, `get_project_tree` + their async `_thread_tool` wrappers
- `tools/search.py` — `glob_search`, `grep_search` with ripgrep fast path and pure-Python fallback; exposes `_glob_search_tool` / `_grep_search_tool` async wrappers
- `tools/shell.py` — `bash`, `run_command`, background-process tracking, long-running-command detection, Windows `taskkill /T` cleanup
- `tools/web.py` — `web_search` (DuckDuckGo Lite → HTML fallback), `web_fetch` (Jina Reader → direct), local HTML-to-text
- `tools/delegate.py` — `delegate_task`, subagent id counter, `_SUBAGENT_TOOLS` list, `set_custom_agents`, dynamic docstring updater
- `tools/registry.py` — composes `CORE_TOOLS`, `ALL_TOOLS`, `GENERAL_TOOLS`, `EXECUTOR_TOOLS`, `PLANNER_TOOLS`, `EXPLORER_TOOLS`, `_SUBAGENT_TOOLS`, builds `TOOL_REGISTRY`, `resolve_tools`, `load_mcp_tools`, `_build_mcp_gateway`
- `tools/_shared.py` — `_notify_file_mutation`, `_checkpoint_file`, `_get_small_model_ref`, `_truncate_output`, `_thread_tool`
- `tools/_diff.py` — `_format_unified_diff`, `_compact_diff`, colour styles for the permission-prompt diff panel

Composed tool sets (single source of truth — see `CORE_TOOLS`, `_READ_ONLY_TOOLS`, etc. in `tools/registry.py`):

| Set | Size | Contents |
|-----|------|----------|
| `CORE_TOOLS` | 13 | read/write/edit × file variants, glob/grep/list, bash, web_search/fetch, delegate_task |
| `ALL_TOOLS` | 17 | `CORE_TOOLS` + `create_task_list`, `update_task`, `update_plan_step`, `enter_plan_mode` |
| `GENERAL_TOOLS` | 17 | alias for `ALL_TOOLS` (build agent) |
| `EXECUTOR_TOOLS` | 17 | alias for `ALL_TOOLS` (executor agent) |
| `PLANNER_TOOLS` | 5 | read-only subset: `read_file`, `read_files`, `glob_search`, `grep_search`, `list_directory` |
| `EXPLORER_TOOLS` | 7 | `PLANNER_TOOLS` + `bash` + `rank_files` |
| `_SUBAGENT_TOOLS` | 13 | tools passed to delegated sub-agents; excludes `delegate_task` to prevent recursion |

Tool categories in the file:

| Category | Tools |
|----------|-------|
| File I/O | `read_file`, `read_files`, `write_file`, `write_files`, `edit_file`, `edit_files` |
| Search | `glob_search`, `grep_search`, `list_directory`, `rank_files` |
| Shell | `bash` |
| Web | `web_search`, `web_fetch` |
| Agent | `delegate_task` (spawns sub-agents via `AgentSpec`) |
| Task mgmt | `create_task_list`, `update_task`, `update_plan_step`, `enter_plan_mode` |

### `tools/tasklist.py` / `tools/plan_mode.py`

Tasklist tracks per-step subtasks during executor runs; `enter_plan_mode` is the autonomous counterpart to the `/plan` slash command, letting the build agent escalate to a structured plan mid-turn.

### `tools/mcp_client.py` — MCP Gateway

Loads tools from MCP servers configured in `aru.json` and exposes them through a single gateway tool that routes calls to the right server.

### `tools/ranker.py` — File Relevance Ranking

Score = `0.50 * name_match + 0.30 * structural + 0.20 * recency`

### `tools/ast_tools.py` — AST Analysis

Tree-sitter based Python parser. Extracts imports, classes, functions, decorators with line numbers.

### `plugins/` — Plugin System (OpenCode-compatible)

Two layers:
1. **Custom Tools**: Python files in `.aru/tools/` or `.agents/tools/` — simplest entry point
2. **Plugins**: Full hook system via `PluginManager` — tools + lifecycle hooks

Custom tool format: `@tool` decorator or bare `def fn() -> str`. Discovery: `~/.aru/tools/`, `.aru/tools/`, `~/.agents/tools/`, `.agents/tools/`. Later roots override earlier.

Plugin hooks: `config`, `tool.execute.before/after`, `tool.definition`, `permission.ask`, `shell.env`, `session.compact`, `chat.message`, `chat.params`, `chat.system.transform`, `chat.messages.transform`, `command.execute.before`, `event`.

## Configuration

- `.env` → `ANTHROPIC_API_KEY`
- `~/.aru/config.json` → global user config (applies to all projects)
- `aru.json` or `.aru/config.json` → project config (deep-merged over global)
- `.agents/commands/*.md` → custom slash commands
- `skills/<name>/SKILL.md` → agentskills.io skills
- `.aru/tools/*.py` → custom tools (Python)
- `.aru/plugins/*.py` → custom plugins (Python)
- `.aru/sessions/` → saved conversation sessions (JSON)

## Development

- **Python:** 3.13+
- **Entry point:** `aru = "aru.cli:main"` (pyproject.toml)
- **Non-interactive mode:** `aru "prompt"` (one-shot with tools), `aru --print "prompt"` (text-only, no tools), `echo "prompt" | aru` (piped input)
- **Async throughout:** asyncio, `arun()` for agent execution
- **Tests:** `tests/` directory, use `pytest-asyncio` with `asyncio_mode = "auto"`

### Running Tests

The project uses a local `.venv` virtual environment. When using the `bash` tool, **DO NOT** use `source .venv/bin/activate` in subprocesses (it doesn't work and will hang).

```bash
# Windows (correct form)
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest --cov=aru --cov-report=term-missing
```

**Always use `--cov-report=term-missing`**, not `--cov-report=html` (HTML causes OOM in WSL2).

## Conventions

- Agent instructions = hardcoded base prompt + AGENTS.md + environment context (git status, project tree)
- Output truncation: shell 10K chars, file reads 30KB
- Windows-aware (UTF-8, taskkill for subprocess cleanup)
- `.gitignore` respected in all file discovery
- Sessions persisted as JSON in `.aru/sessions/`
- Project language: Portuguese comments in some places; code in English
