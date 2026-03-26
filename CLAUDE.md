# Aru — AI Coding Assistant

Multi-agent CLI coding assistant powered by Claude (via Agno framework). Interactive REPL where users describe tasks in natural language and agents plan/execute code changes using 16 integrated tools.

**Read `AGENTS.md` for the full architecture reference.** Do not search for information already documented there.

## Quick Architecture

```
main.py → cli.run_cli() → REPL loop
                             ├─ General Agent (conversation + tool use)
                             ├─ /plan → Planner Agent (read-only, creates plans)
                             └─ /plan steps → Executor Agent (implements steps)
```

### Key Files

| File | Purpose |
|------|---------|
| `aru/cli.py` | Interactive CLI, session management, command routing |
| `aru/config.py` | Loads AGENTS.md, `.agents/commands/`, `.agents/skills/` |
| `aru/providers.py` | Multi-provider LLM abstraction (anthropic, openai, ollama, groq, etc.) |
| `aru/agents/base.py` | Shared instruction templates (BASE_INSTRUCTIONS, roles) |
| `aru/agents/planner.py` | Planning agent — read-only tools, structured markdown output |
| `aru/agents/executor.py` | Execution agent — all tools, implements plan steps |
| `aru/tools/codebase.py` | 16 core tools (read/write/edit/search/bash/web/delegate) |
| `aru/tools/ast_tools.py` | Tree-sitter Python AST analysis |
| `aru/tools/indexer.py` | Chromadb semantic indexing |
| `aru/tools/ranker.py` | Multi-factor file relevance scoring |
| `aru/tools/gitignore.py` | .gitignore-aware file filtering |
| `aru.json` | Runtime config (permissions, models, custom providers) |

### Tool Categories (16 tools in `codebase.py`)

- **File I/O:** read_file, write_file, write_files, edit_file, edit_files
- **Search:** glob_search, grep_search, semantic_search, rank_files, list_directory
- **Analysis:** code_structure, find_dependencies
- **Shell:** bash, run_command
- **Web:** web_search, web_fetch
- **Agent:** delegate_task

## Development

- **Python:** 3.13+
- **Entry point:** `aru = "aru.cli:main"` (pyproject.toml)
- **Async throughout:** asyncio, `arun()` for agent execution
- **Tests:** 15 modules in `tests/`, use `pytest-asyncio` with `asyncio_mode = "auto"`

### Running Tests

**Do NOT use `source .venv/bin/activate` in bash tool** — it hangs in subprocesses.

```bash
# Use venv executables directly
.venv/Scripts/pytest
.venv/Scripts/pytest --cov=aru --cov-report=term-missing

# Or if venv already active in parent shell
python -m pytest
python -m pytest --cov=aru --cov-report=term-missing
```

**Always use `--cov-report=term-missing`**, not `--cov-report=html` (HTML causes OOM in WSL2).

## Conventions

- Agent instructions = hardcoded base prompt + AGENTS.md + environment context (git status, project tree)
- Output truncation: shell 10K chars, file reads 30KB
- Windows-aware (UTF-8, taskkill for subprocess cleanup)
- `.gitignore` respected in all file discovery
- Permission gating: read-only auto-approve; write/bash prompt user
- Sessions persisted as JSON in `.aru/sessions/`
- Project language: Portuguese comments in some places; code in English

## Configuration

- `.env` → `ANTHROPIC_API_KEY`
- `aru.json` → permissions, model defaults, custom providers
- `.agents/commands/*.md` → custom slash commands
- `.agents/skills/*.md` → custom skills with YAML frontmatter
- `.aru/` → runtime data (sessions, chroma embeddings, index metadata)
