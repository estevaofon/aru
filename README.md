# aru

An intelligent coding assistant for the terminal, powered by LLMs and [Agno](https://github.com/agno-agi/agno) agents.

![0329(3)](https://github.com/user-attachments/assets/e84d5139-ebaa-4d12-bbae-628fae7dbc7a)

## Highlights

- **Multi-Agent Architecture** — Specialized agents for planning, execution, and conversation
- **Interactive CLI** — Streaming responses, multi-line paste, session management
- **16 Integrated Tools** — File operations, code search, shell, web search, task delegation
- **Task Planning** — Break down complex tasks into steps with automatic execution
- **Multi-Provider** — Anthropic, OpenAI, Ollama, Groq, OpenRouter, DeepSeek, and others via custom configuration
- **Custom Commands, Skills, and Agents** — Extend aru via the `.agents/` directory
- **MCP Support** — Integration with Model Context Protocol servers

## Quick Start

### 1. Install

```bash
pip install aru-code
```

> **Requirements:** Python 3.13+

### 2. Configure the API Key

Aru uses **Claude Sonnet 4.6** from Anthropic as the default model. You need an [Anthropic API key](https://console.anthropic.com/) to get started.

Set your API key as an environment variable or create a `.env` file in your project directory:

```env
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

> Using another provider? See the [Models and Providers](#models-and-providers) section to configure OpenAI, Ollama, Groq, etc.

### 3. Run

```bash
aru
```

That's it — `aru` is available globally after install.

## Usage

### Commands

| Command | Description |
|---------|-------------|
| Natural language | Just type — aru handles the rest |
| `/plan <task>` | Creates a detailed implementation plan |
| `/model [provider/model]` | Switch models and providers |
| `/mcp` | List available MCP servers and tools |
| `/commands` | List custom commands |
| `/skills` | List available skills |
| `/agents` | List custom agents |
| `/sessions` | List recent sessions |
| `/help` | Show all commands |
| `! <command>` | Execute shell commands |
| `/quit` or `/exit` | Exit aru |

### CLI Options

```bash
aru                                    # Start new session
aru --resume <id>                      # Resume session
aru --resume last                      # Resume last session
aru --list                             # List sessions
aru --dangerously-skip-permissions     # Skip permission prompts
```

### Examples

```
aru> /plan create a REST API with FastAPI to manage users

aru> refactor the authentication module to use JWT tokens

aru> ! pytest tests/ -v

aru> /model ollama/codellama
```

## Configuration

### Models and Providers

By default, aru uses **Claude Sonnet 4.6** (Anthropic). You can switch to any supported provider during a session with `/model`:

| Provider | Command | API Key (`.env`) | Extra Installation |
|----------|---------|-------------------|------------------|
| **Anthropic** | `/model anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY` | — (included) |
| **Ollama** | `/model ollama/llama3.1` | — (local) | `pip install "aru-code[ollama]"` |
| **OpenAI** | `/model openai/gpt-4o` | `OPENAI_API_KEY` | `pip install "aru-code[openai]"` |
| **Groq** | `/model groq/llama-3.3-70b-versatile` | `GROQ_API_KEY` | `pip install "aru-code[groq]"` |
| **OpenRouter** | `/model openrouter/deepseek/deepseek-chat-v3-0324` | `OPENROUTER_API_KEY` | `pip install "aru-code[openai]"` |
| **MiniMax** | `/model openrouter/minimax/minimax-m2.7` | `OPENROUTER_API_KEY` | `pip install "aru-code[openai]"` |

To install all providers at once:

```bash
pip install "aru-code[all-providers]"
```

#### Ollama (local models)

To run models locally without an API key, install [Ollama](https://ollama.com/), start the server, and use any installed model:

```bash
ollama serve                    # Start the Ollama server
ollama pull codellama           # Download a model
aru                             # Start aru
# Inside aru:
/model ollama/codellama
```

#### Configuring the default model

You can set the default provider/model in `aru.json` so you don't need to switch manually every session:

```json
{
  "default_model": "openrouter/minimax/minimax-m2.7",
  "model_aliases": {
    "minimax": "openrouter/minimax/minimax-m2.5",
    "minimax-m2.7": "openrouter/minimax/minimax-m2.7",
    "deepseek-v3": "openrouter/deepseek/deepseek-chat-v3-0324",
    "sonnet-4-6": "anthropic/claude-sonnet-4-6",
    "opus-4-6": "anthropic/claude-opus-4-6"
  }
}
```

The `default_model` field sets the main model. The `model_aliases` are shortcuts that can be used with `/model <alias>`.

#### Custom providers

You can configure custom providers with specific token limits:

```json
{
  "providers": {
    "deepseek": {
      "models": {
        "deepseek-chat-v3-0324": { "max_tokens": 16384 }
      }
    },
    "openrouter": {
      "models": {
        "minimax/minimax-m2.5": { "max_tokens": 65536 },
        "minimax/minimax-m2.7": { "max_tokens": 131072 }
      }
    }
  }
}
```

### Permissions (`aru.json`)

Aru uses a granular permission system where each tool action resolves to one of three outcomes:

- **`allow`** — executes without asking
- **`ask`** — prompts for confirmation (once / always / no)
- **`deny`** — blocks the action silently

Configure permissions per tool category with glob patterns:

```json
{
  "permission": {
    "*": "ask",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "edit": {
      "*": "allow",
      "*.env": "deny"
    },
    "write": {
      "*": "allow",
      "*.env": "deny"
    },
    "bash": {
      "*": "ask",
      "git *": "allow",
      "npm *": "allow",
      "pytest *": "allow",
      "rm -rf *": "deny"
    },
    "web_search": "allow",
    "web_fetch": "allow",
    "delegate_task": "allow"
  }
}
```

#### Available categories

| Category | Matched against | Default |
|----------|----------------|---------|
| `read` | file path | `allow` |
| `edit` | file path | `ask` |
| `write` | file path | `ask` |
| `bash` | command string | safe prefixes = `allow`, rest = `ask` |
| `glob` | — | `allow` |
| `grep` | — | `allow` |
| `list` | — | `allow` |
| `web_search` | — | `allow` |
| `web_fetch` | URL | `allow` |
| `delegate_task` | — | `allow` |

#### Rule precedence

Rules use **last-match-wins** ordering. Place catch-all `"*"` first, then specific patterns:

```json
{
  "edit": {
    "*": "allow",
    "*.env": "deny",
    "*.env.example": "allow"
  }
}
```

#### Shorthands

```json
"permission": "allow"
```
Allows everything (equivalent to `--dangerously-skip-permissions`).

```json
"permission": { "read": "allow", "edit": "ask" }
```
String value applies to all patterns in that category.

#### Defaults

Without any `aru.json` config, aru applies safe defaults:
- Read-only tools (`read`, `glob`, `grep`, `list`) → `allow`
- Mutating tools (`edit`, `write`) → `ask`
- Bash → ~40 safe command prefixes auto-allowed (`ls`, `git status`, `grep`, etc.), rest → `ask`
- Sensitive files (`*.env`, `*.env.*`) → `deny` for read/edit/write (except `*.env.example`)

> `aru.json` can also be placed at `.aru/config.json`.
>
> A full `aru.json` config reference here: [`aru.json`](./aru.json)

### AGENTS.md

Place an `AGENTS.md` file in your project root with custom instructions that will be appended to all agent system prompts.

### `.agents/` Directory

```
.agents/
├── agents/         # Custom agents with their own model, tools, and prompt
│   └── reviewer.md # Usage: /reviewer <args>
├── commands/       # Custom slash commands (filename = command name)
│   └── deploy.md   # Usage: /deploy <args>
└── skills/         # Custom skills/personas
    └── review/
        └── SKILL.md
```

Command files support frontmatter with `description` and the `$INPUT` template variable for arguments.

### Custom Agents

Custom agents are Markdown files with YAML frontmatter stored in `.agents/agents/`. Each agent runs with its own system prompt, model, and tool set — unlike commands and skills, which reuse the General Agent.

```markdown
---
name: Code Reviewer
description: Review code for quality, bugs, and best practices
model: anthropic/claude-sonnet-4-5
tools: read_file, grep_search, glob_search, code_structure
max_turns: 15
mode: primary
---

You are an expert code reviewer. Analyze code for bugs, security,
performance, and readability. Do NOT modify files.
```

#### Frontmatter fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Display name of the agent |
| `description` | Yes | When to use this agent (shown in `/agents` and tab completion) |
| `model` | No | Provider/model reference (e.g., `anthropic/claude-sonnet-4-5`). Defaults to session model |
| `tools` | No | Comma-separated tool names (allowlist) or JSON object for granular control (e.g., `{"bash": false}`). Defaults to all general tools |
| `max_turns` | No | Max tool calls before the agent stops. Default: 20 |
| `mode` | No | `primary` (invocable via `/name`) or `subagent` (only via `delegate_task`). Default: `primary` |
| `permission` | No | Permission overrides (same format as `aru.json` permission section). Replaces global rules for specified categories while the agent runs |

#### Invocation

```
aru> /reviewer src/auth.py        # invoke by slash + filename (without .md)
aru> /agents                       # list all custom agents
```

#### Discovery paths

Agents are discovered from multiple locations (later overrides earlier):

1. `~/.agents/agents/` — global (available in all projects)
2. `~/.claude/agents/` — global (Claude Code compatible path)
3. `.agents/agents/` — project-local
4. `.claude/agents/` — project-local

#### Agent-level permissions

Agents can override global permission rules. Overrides replace the entire category — unspecified categories inherit from global config.

```markdown
---
name: Code Reviewer
description: Read-only code reviewer
permission:
  edit: deny
  write: deny
  bash:
    git diff *: allow
    grep *: allow
---
```

You can also set agent permissions in `aru.json` (overrides frontmatter):

```json
{
  "agent": {
    "reviewer": {
      "permission": { "edit": "deny", "write": "deny" }
    }
  }
}
```

Each agent gets its own isolated "always" memory — approvals during an agent's run don't carry over to the global scope.

#### Subagent mode

Agents with `mode: subagent` can be referenced by the LLM via `delegate_task(task, agent="name")` but are not directly invocable from the CLI.

### MCP Support (Model Context Protocol)

Aru can load tools from MCP servers. Configure in `.aru/mcp_config.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/allowed/path"]
    }
  }
}
```

## Agents

| Agent | Role | Tools |
|-------|------|-------|
| **Planner** | Analyzes codebase, creates structured implementation plans | Read-only tools, search, web |
| **Executor** | Implements code changes based on plans or instructions | All tools including delegation |
| **General** | Handles conversation and simple operations | All tools including delegation |

## Tools

### File Operations
- `read_file` — Reads files with line range support and binary detection
- `read_file_smart` — Smart file reading focused on relevant snippets for the query
- `write_file` / `write_files` — Writes single or batch files
- `edit_file` / `edit_files` — Find-replace edits across multiple files

### Search & Discovery
- `glob_search` — Find files by pattern (respects .gitignore)
- `grep_search` — Content search with regex and file filtering
- `list_directory` — Directory listing with gitignore filtering
- `rank_files` — Multi-factor file relevance ranking (name, structure, recency)

### Code Analysis
- `code_structure` — Extracts classes, functions, imports via tree-sitter AST
- `find_dependencies` — Analyzes import relationships between files

### Shell & Web
- `bash` — Executes shell commands with permission gates
- `web_search` — Web search via DuckDuckGo
- `web_fetch` — Fetches URLs and converts HTML to readable text

### Advanced
- `delegate_task` — Spawns autonomous sub-agents for parallel task execution

## Architecture

```
aru-code/
├── aru/
│   ├── cli.py              # Interactive CLI with streaming display
│   ├── config.py           # Configuration loader (AGENTS.md, .agents/)
│   ├── providers.py        # Multi-provider LLM abstraction
│   ├── permissions.py      # Granular permission system (allow/ask/deny)
│   ├── agents/
│   │   ├── planner.py      # Planning agent
│   │   └── executor.py     # Execution agent
│   └── tools/
│       ├── codebase.py     # 16 core tools
│       ├── ast_tools.py    # Tree-sitter code analysis
│       ├── ranker.py       # File relevance ranking
│       ├── mcp_client.py   # MCP client
│       └── gitignore.py    # Gitignore-aware filtering
├── aru.json                # Permissions and model configuration
├── .env                    # API keys (not committed)
├── .aru/                   # Local data (sessions)
└── pyproject.toml
```

## Built With

- **[Agno](https://github.com/agno-agi/agno)** — Agent framework with tool orchestration
- **[Anthropic Claude](https://www.anthropic.com/)** — Sonnet 4.6, Opus 4.6, Haiku 4.5
- **[tree-sitter](https://tree-sitter.github.io/)** — AST-based code analysis
- **[Rich](https://rich.readthedocs.io/)** — Terminal UI
- **[prompt-toolkit](https://python-prompt-toolkit.readthedocs.io/)** — Advanced input handling

## Development

```bash
# Clone and install in editable mode with dev dependencies
git clone https://github.com/estevaofon/aru.git
cd aru
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=aru --cov-report=term-missing
```

---

Built with Claude and Agno
