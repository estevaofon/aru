# Tools

Aru provides 11 built-in tools and supports user-defined custom tools. All tools are callable by the LLM during conversations.

## Built-in Tools

| Tool | Category | Description |
|------|----------|-------------|
| `read_file` | File I/O | Read file contents with optional line range |
| `read_file_smart` | File I/O | Intelligent read — full file or AST summary for large files |
| `write_file` | File I/O | Create or overwrite a file |
| `edit_file` | File I/O | Apply targeted edits (find & replace) to one or more files |
| `glob_search` | Search | Find files matching a glob pattern recursively |
| `grep_search` | Search | Search file contents with regex patterns |
| `list_directory` | Search | List directory contents with depth control |
| `bash` | Shell | Execute shell commands (with permission checks) |
| `web_search` | Web | Search the web via DuckDuckGo |
| `web_fetch` | Web | Fetch and extract text from a URL |
| `delegate_task` | Agent | Spawn a sub-agent for parallel tasks |

Additional tools available in specific contexts:

| Tool | Context | Description |
|------|---------|-------------|
| `rank_files` | Registered | Score files by relevance to a query |
| `create_task_list` | Executor | Create subtask checklist during plan execution |
| `update_task` | Executor | Update subtask status |
| `use_mcp_tool` | MCP loaded | Gateway to call any MCP server tool |

## Custom Tools

Custom tools are Python files that define new tools the LLM can call. Drop a `.py` file in the right directory and Aru picks it up automatically.

### Quick Start

```bash
mkdir -p .aru/tools
```

```python
# .aru/tools/ping.py
from aru.plugins import tool

@tool(description="Ping a network host and return latency results")
def ping(host: str = "8.8.8.8", count: int = 4) -> str:
    """Ping a host address.

    Args:
        host: Hostname or IP to ping.
        count: Number of packets to send.
    """
    import platform, subprocess
    flag = "-n" if platform.system() == "Windows" else "-c"
    result = subprocess.run(
        ["ping", flag, str(count), host],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip() or f"Ping completed (exit {result.returncode})"
```

On startup:

```
Loaded 1 custom tool(s): ping
```

### Two Styles

**With `@tool` decorator (recommended):**

```python
from aru.plugins import tool

@tool(description="Search Jira issues by JQL query")
def search_jira(query: str, max_results: int = 10) -> str:
    """Args:
        query: JQL query string.
        max_results: Maximum number of results.
    """
    # ...
    return f"Found {count} issues"
```

**Bare function (zero imports):**

```python
def hello(name: str) -> str:
    """Greet someone by name.

    Args:
        name: The person to greet.
    """
    return f"Hello, {name}!"
```

Bare functions must have the `-> str` return annotation to be discovered.

### Naming Convention

Mirrors [OpenCode's custom tools](https://opencode.ai/docs/custom-tools/):

| File | Functions | Tool names |
|------|-----------|------------|
| `deploy.py` | `def deploy` | `deploy` |
| `ci.py` | `def build` + `def test` | `ci_build`, `ci_test` |
| `hello.py` | single function | `hello` (filename only) |

### Discovery Paths

Searched in order (later overrides earlier):

| Scope | Paths |
|-------|-------|
| Global | `~/.agents/tools/*.py`, `~/.aru/tools/*.py` |
| Project | `.agents/tools/*.py`, `.aru/tools/*.py` |

### Override Built-in Tools

A custom tool with the same name as a built-in tool replaces it:

```python
# .aru/tools/bash.py — override the built-in bash tool
from aru.plugins import tool

@tool(description="Safe bash — blocks rm commands", override=True)
def bash(command: str, timeout: int = 60) -> str:
    if "rm " in command:
        return "Blocked: rm commands are not allowed"
    from aru.tools.codebase import run_command
    import asyncio
    return asyncio.run(run_command(command, timeout=timeout))
```

### Async Support

Both sync and async functions work:

```python
@tool(description="Fetch data from an API")
async def fetch_api(url: str) -> str:
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        return r.text[:2000]
```

### Accessing Runtime Context

Custom tools can access Aru internals via `get_ctx()`:

```python
from aru.plugins import tool
from aru.runtime import get_ctx

@tool(description="Show current model")
def model_info() -> str:
    ctx = get_ctx()
    return f"Model: {ctx.model_id}"
```

Available on `RuntimeContext`: `console`, `model_id`, `small_model_ref`, `read_cache`, `tracked_processes`, `perm_config`, `plugin_manager`, `mcp_catalog_text`.

### Disabling Tools

In `aru.json`:

```json
{
  "tools": {
    "disabled": ["web_search", "deploy"]
  }
}
```

### Tool Registry

All tools (built-in + custom + plugin + MCP) are registered in `TOOL_REGISTRY` (`aru/tools/codebase.py`). The registry maps `name → function` and is used by `resolve_tools()` when creating agents.

Priority (last wins): built-in → global custom → project custom → plugin tools.

## MCP Tools

Aru supports [Model Context Protocol](https://modelcontextprotocol.io/) servers. MCP tools are loaded at startup and exposed through a single gateway tool (`use_mcp_tool`) or individually (eager mode).

Configuration in `aru.json`:

```json
{
  "mcp": {
    "servers": {
      "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": { "GITHUB_TOKEN": "ghp_..." }
      }
    }
  }
}
```

See `aru/tools/mcp_client.py` for details.
