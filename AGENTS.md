# Aru — AI Coding Assistant

Aru is a multi-agent CLI coding assistant powered by Claude (via Agno framework). It provides an interactive REPL where users describe tasks in natural language, and agents plan and execute code changes using 16 integrated tools.

## Architecture

```
main.py → cli.run_cli() → REPL loop
                             ├─ General Agent (conversation + tool use)
                             ├─ /plan → Planner Agent (read-only, creates step-by-step plans)
                             └─ /plan steps → Executor Agent (implements each step)
```

All agents use Claude models (sonnet/opus/haiku) via Agno's `Agent` class with streaming responses.

## Project Structure

```
aru/
├── cli.py              # Interactive CLI, session management, command routing (1260 LOC)
├── config.py           # Loads AGENTS.md, .agents/commands/, .agents/skills/
├── agents/
│   ├── planner.py      # Planning agent — read-only tools, outputs structured plans
│   └── executor.py     # Execution agent — all tools, implements plan steps
└── tools/
    ├── codebase.py     # 16 core tools (read/write/edit/search/bash/web/delegate)
    ├── ast_tools.py    # Tree-sitter Python AST analysis (classes, functions, imports)
    ├── ranker.py        # Multi-factor file relevance scoring
    └── gitignore.py     # .gitignore-aware file filtering with caching
```

## Key Modules

### `cli.py` — Entry Point & REPL

- `run_cli()`: Main async loop — loads config, creates session, processes input
- `Session`: Conversation history (last 20 msgs), plan tracking, model selection, token metrics. Persisted as JSON in `.aru/sessions/`
- Command routing: `/` slash commands, `!` shell passthrough, natural language → agent
- `StreamingDisplay` + `StatusBar`: Rich-based live terminal rendering

### `config.py` — Project Configuration

Loads project-level customization into an `AgentConfig` object:
- `AGENTS.md` → extra instructions appended to all agent prompts
- `.agents/commands/*.md` → custom slash commands (filename = command name)
- `skills/<name>/SKILL.md` → custom skills (agentskills.io format, searched in `.agents/`, `.claude/`, and `~/`)

### `agents/planner.py` — Planner Agent

- Model: Sonnet (4K max tokens)
- Tools: Read-only subset (read, search, glob, grep, code_structure, web)
- Output: Markdown with `## Summary` and `## Steps` sections

### `agents/executor.py` — Executor Agent

- Model: Sonnet (8K max tokens)
- Tools: ALL_TOOLS (including write, edit, bash)
- Receives plan steps with full context

### `tools/codebase.py` — Tool Registry

16 tools exported as `ALL_TOOLS`:

| Category | Tools |
|----------|-------|
| File I/O | `read_file`, `write_file`, `write_files`, `edit_file`, `edit_files` |
| Search | `glob_search`, `grep_search`, `rank_files`, `list_directory` |
| Analysis | `code_structure`, `find_dependencies` |
| Shell | `bash`, `run_command` |
| Web | `web_search`, `web_fetch` |
| Agent | `delegate_task` (spawns sub-agents) |

Permission model: read-only tools auto-approve; write/bash tools prompt user (with "allow all" option). Safe command prefixes whitelist ~40 read-only shell commands.

### `tools/ranker.py` — File Relevance Ranking

Score = `0.50 * name_match + 0.30 * structural + 0.20 * recency`

### `tools/ast_tools.py` — AST Analysis

Tree-sitter based Python parser. Extracts imports, classes, functions, decorators with line numbers.

## Data & Config Files

- `.env` → `ANTHROPIC_API_KEY`
- `.aru/sessions/` → Saved conversation sessions (JSON)
- `.claude/settings.local.json` → Permission allowlists

## Development Workflow

### Running Tests

The project uses a local `.venv` virtual environment. When using the `bash` tool, **DO NOT** use `source .venv/bin/activate` in subprocesses (it doesn't work and will hang).

**For bash tool commands, use one of these patterns:**

```bash
# Windows (correct form)
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest --cov=aru --cov-report=term-missing
```

**Important:** Always prefer `--cov-report=term-missing` over `--cov-report=html` when running coverage tests. The HTML report consumes significantly more memory and can cause the process to be killed by the OOM killer, especially in memory-constrained environments like WSL2.

**For manual interactive shell usage (not tool calls):**

```bash
# Linux/macOS
source .venv/bin/activate
pytest

# PowerShell (Windows)
.\.venv\Scripts\activate.ps1
pytest
```

## Conventions

- Async throughout (`asyncio`, `arun()` for agent execution)
- Agent instructions are composed from: hardcoded base prompt + AGENTS.md extras + environment context (git status, project tree)
- Output truncation at 10K chars for shell, 30KB for file reads
- Windows-aware process management (taskkill for subprocess cleanup)
- `.gitignore` respected in all file discovery operations
