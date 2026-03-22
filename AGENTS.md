# Arc — AI Coding Assistant

Arc is a multi-agent CLI coding assistant powered by Claude (via Agno framework). It provides an interactive REPL where users describe tasks in natural language, and agents plan and execute code changes using 16 integrated tools.

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
arc/
├── cli.py              # Interactive CLI, session management, command routing (1260 LOC)
├── config.py           # Loads AGENTS.md, .agents/commands/, .agents/skills/
├── agents/
│   ├── planner.py      # Planning agent — read-only tools, outputs structured plans
│   └── executor.py     # Execution agent — all tools, implements plan steps
└── tools/
    ├── codebase.py     # 16 core tools (read/write/edit/search/bash/web/delegate)
    ├── ast_tools.py    # Tree-sitter Python AST analysis (classes, functions, imports)
    ├── indexer.py       # Chromadb semantic indexing (chunked file embeddings)
    ├── ranker.py        # Multi-factor file relevance scoring
    └── gitignore.py     # .gitignore-aware file filtering with caching
```

## Key Modules

### `cli.py` — Entry Point & REPL

- `run_cli()`: Main async loop — loads config, creates session, processes input
- `Session`: Conversation history (last 20 msgs), plan tracking, model selection, token metrics. Persisted as JSON in `.arc/sessions/`
- Command routing: `/` slash commands, `!` shell passthrough, natural language → agent
- `StreamingDisplay` + `StatusBar`: Rich-based live terminal rendering

### `config.py` — Project Configuration

Loads project-level customization into an `AgentConfig` object:
- `AGENTS.md` → extra instructions appended to all agent prompts
- `.agents/commands/*.md` → custom slash commands (filename = command name)
- `.agents/skills/*.md` → custom skills with YAML frontmatter

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
| Search | `glob_search`, `grep_search`, `semantic_search`, `rank_files`, `list_directory` |
| Analysis | `code_structure`, `find_dependencies` |
| Shell | `bash`, `run_command` |
| Web | `web_search`, `web_fetch` |
| Agent | `delegate_task` (spawns sub-agents) |

Permission model: read-only tools auto-approve; write/bash tools prompt user (with "allow all" option). Safe command prefixes whitelist ~40 read-only shell commands.

### `tools/indexer.py` — Semantic Search

- Chromadb vector DB persisted in `.arc/chroma/`
- Chunks files (1500 chars, 200 overlap), tracks mtimes in `.arc/index_meta.json`
- Lazy init on first `semantic_search` call

### `tools/ranker.py` — File Relevance Ranking

Score = `0.45 * semantic + 0.25 * name_match + 0.20 * structural + 0.10 * recency`

### `tools/ast_tools.py` — AST Analysis

Tree-sitter based Python parser. Extracts imports, classes, functions, decorators with line numbers.

## Data & Config Files

- `.env` → `ANTHROPIC_API_KEY`
- `.arc/sessions/` → Saved conversation sessions (JSON)
- `.arc/chroma/` → Chromadb embeddings
- `.arc/index_meta.json` → File indexing metadata
- `.claude/settings.local.json` → Permission allowlists

## Dependencies

Core: `agno`, `anthropic`, `chromadb>=0.5`, `tree-sitter>=0.23`, `tree-sitter-python`, `prompt-toolkit>=3.0`, `rich`, `pathspec>=0.12`, `art>=6.0`, `python-dotenv`, `httpx`, `sqlalchemy>=2.0.48`

Python: **3.13+**

## Conventions

- Async throughout (`asyncio`, `arun()` for agent execution)
- Agent instructions are composed from: hardcoded base prompt + AGENTS.md extras + environment context (git status, project tree)
- Output truncation at 10K chars for shell, 30KB for file reads
- Windows-aware process management (taskkill for subprocess cleanup)
- `.gitignore` respected in all file discovery operations
