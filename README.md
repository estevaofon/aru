# aru

An intelligent coding assistant for the terminal, powered by LLMs and [Agno](https://github.com/agno-agi/agno) agents.
</br></br>
<img width="600" alt="image" src="https://github.com/user-attachments/assets/36001faa-3163-4374-84fd-da8704a4ed9d" />



## Highlights

- **Multi-Agent Architecture** — Specialized agents for planning, execution, and conversation
- **Interactive CLI** — Streaming responses, multi-line paste, session management
- **16 Integrated Tools** — File operations, code search, shell, web search, task delegation
- **Task Planning** — Break down complex tasks into steps with automatic execution
- **Multi-Provider** — Anthropic, OpenAI, Ollama, Groq, OpenRouter, DeepSeek, and others via custom configuration
- **Custom Commands and Skills** — Extend aru via the `.agents/` directory
- **MCP Support** — Integration with Model Context Protocol servers

## Quick Start

### 1. Install

```bash
pip install -e .
```

> **Requirements:** Python 3.13+

### 2. Configure the API Key

Aru uses **Claude Sonnet 4.6** from Anthropic as the default model. You need an [Anthropic API key](https://console.anthropic.com/) to get started.

Create a `.env` file in the project root:

```bash
cp .env.example .env
```

Edit the `.env` with your key:

```env
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

> Using another provider? See the [Models and Providers](#models-and-providers) section to configure OpenAI, Ollama, Groq, etc.

### 3. Run

```bash
aru
```

### Global Installation (run `aru` from anywhere)

To use aru as a global command in the terminal, create a dedicated virtual environment and a wrapper script:

<details>
<summary><strong>Windows</strong></summary>

1. Create the virtual environment and install:
```bash
python -m venv C:\aru-env
C:\aru-env\Scripts\pip install -e C:\path\to\aru
```

2. Create `aru.bat` in a folder on your `PATH` (e.g., `C:\Users\<user>\bin\`):
```bat
@echo off
C:\aru-env\Scripts\python -m aru.cli %*
```

</details>

<details>
<summary><strong>Linux / macOS</strong></summary>

1. Create the virtual environment and install:
```bash
python3 -m venv ~/.aru-env
~/.aru-env/bin/pip install -e /path/to/aru
```

2. Create the script `~/.local/bin/aru`:
```bash
#!/bin/bash
~/.aru-env/bin/python -m aru.cli "$@"
```

3. Make it executable:
```bash
chmod +x ~/.local/bin/aru
```

</details>

Done — now `aru` works from any directory.

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
| **Ollama** | `/model ollama/llama3.1` | — (local) | `pip install -e ".[ollama]"` |
| **OpenAI** | `/model openai/gpt-4o` | `OPENAI_API_KEY` | `pip install -e ".[openai]"` |
| **Groq** | `/model groq/llama-3.3-70b-versatile` | `GROQ_API_KEY` | `pip install -e ".[groq]"` |
| **OpenRouter** | `/model openrouter/deepseek/deepseek-chat-v3-0324` | `OPENROUTER_API_KEY` | `pip install -e ".[openai]"` |

To install all providers at once:

```bash
pip install -e ".[all-providers]"
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
  "models": {
    "default": "openrouter/deepseek/deepseek-chat-v3-0324",
    "minimax": "openrouter/minimax/minimax-m2.5",
    "deepseek-v3": "openrouter/deepseek/deepseek-chat-v3-0324",
    "sonnet-4-6": "anthropic/claude-sonnet-4-6",
    "opus-4-6": "anthropic/claude-opus-4-6"
  }
}
```

The `default` field sets the main model. The other fields are aliases that can be used with `/model <alias>`.

#### Custom providers

You can configure custom providers with specific token limits:

```json
{
  "providers": {
    "deepseek": {
      "models": {
        "deepseek-chat-v3-0324": {"id": "deepseek-chat-v3-0324", "max_tokens": 16384}
      }
    },
    "openrouter": {
      "models": {
        "minimax/minimax-m2.5": {"id": "minimax/minimax-m2.5", "max_tokens": 65536}
      }
    }
  }
}
```

### Permissions (`aru.json`)

The `aru.json` file in the project root controls which shell commands aru can execute **without asking for confirmation**:

```json
{
  "permission": {
    "allow": [
      "git *",
      "npm *",
      "pytest *",
      "python *",
      "uv run pytest *"
    ]
  }
}
```

Each entry is a glob pattern. Any command that doesn't match a listed pattern will prompt for confirmation before executing.

> `aru.json` can also be placed at `.aru/config.json`.

### AGENTS.md

Place an `AGENTS.md` file in your project root with custom instructions that will be appended to all agent system prompts.

### `.agents/` Directory

```
.agents/
├── commands/       # Custom slash commands (filename = command name)
│   └── deploy.md   # Usage: /deploy <args>
└── skills/         # Custom skills/personas
    └── review.md   # Loaded as additional agent instructions
```

Command files support frontmatter with `description` and the `$INPUT` template variable for arguments.

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
# Install with development dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=aru --cov-report=term-missing
```

---

Built with Claude and Agno
