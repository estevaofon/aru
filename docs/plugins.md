# Plugins

Plugins extend Aru beyond what custom tools offer. While [custom tools](tools.md) add new functions the LLM can call, plugins can also intercept tool execution, inject environment variables, auto-approve permissions, and modify conversation history before compaction.

Inspired by [OpenCode's plugin system](https://opencode.ai/docs/custom-tools/) — the hook API is designed to make porting straightforward.

## Quick Start

```bash
mkdir -p .aru/plugins
```

```python
# .aru/plugins/my_plugin.py
from aru.plugins import Hooks, PluginInput

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()

    # Add a tool
    hooks.tools["hello"] = lambda name="world": f"Hello, {name}!"

    # Hook into tool execution
    @hooks.on("tool.execute.before")
    def log_tools(event):
        print(f"[my_plugin] Tool called: {event.tool_name}")

    return hooks
```

On startup:

```
Loaded 1 plugin(s): my_plugin (1 tool(s))
```

## Plugin Structure

Every plugin is a Python module that exports a `plugin()` function:

```python
def plugin(ctx: PluginInput, options: dict | None = None) -> Hooks:
```

- Can be `def` or `async def`
- Receives context about the current session
- Returns a `Hooks` object with tools and/or event handlers
- If loading fails, Aru logs a warning and continues without it

### PluginInput

```python
@dataclass
class PluginInput:
    directory: str     # Project root (os.getcwd())
    config_path: str   # Path to aru.json (or "")
    model_ref: str     # Current model (e.g. "anthropic/claude-sonnet-4-5")
```

### Hooks Object

```python
hooks = Hooks()

# Register tools (dict of name → callable or dict)
hooks.tools["my_tool"] = my_function

# Register event handlers
@hooks.on("hook_name")
def handler(event: HookEvent):
    ...
```

### HookEvent

All hook handlers receive a `HookEvent` with a mutable `data` dict:

```python
event.hook        # hook name (str)
event.data        # full payload (dict) — mutate this to change behavior
event.tool_name   # shortcut for event.data["tool_name"]
event.args        # shortcut for event.data["args"]
event.result      # shortcut for event.data["result"]
event.env         # shortcut for event.data["env"]
```

## Hooks Reference

| Hook | When it fires | Payload | What you can do |
|------|--------------|---------|-----------------|
| `tool.execute.before` | Before any tool runs | `tool_name`, `args` | Inspect/modify arguments, block by raising |
| `tool.execute.after` | After any tool runs | `tool_name`, `args`, `result` | Inspect/modify the result |
| `permission.ask` | Before prompting user for permission | `category`, `subject` | Set `event.data["allow"] = True/False` to auto-decide |
| `shell.env` | Before `bash` subprocess starts | `cwd`, `command`, `env` | Inject environment variables via `event.env` |
| `session.compact` | Before context compaction | `history` | Pre-process or filter conversation history |
| `config` | After config is loaded | `AgentConfig` | Modify configuration |
| `tool.definition` | When tools are resolved | `tools` | Add or remove tools from the set |

## Examples

### Inject Environment Variables

```python
# .aru/plugins/env_vars.py
from aru.plugins import Hooks, PluginInput

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()

    @hooks.on("shell.env")
    def inject_env(event):
        event.env["NODE_ENV"] = "development"
        event.env["DATABASE_URL"] = "postgres://localhost/mydb"
        event.env["AWS_PROFILE"] = "dev"

    return hooks
```

Every `bash` command now runs with these variables set.

### Auto-approve Specific Commands

```python
# .aru/plugins/auto_approve.py
from aru.plugins import Hooks, PluginInput

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()

    @hooks.on("permission.ask")
    def auto_approve(event):
        subject = event.data.get("subject", "")
        # Auto-approve git and pytest commands
        if subject.startswith(("git ", "pytest")):
            event.data["allow"] = True
        # Always deny dangerous commands
        if "rm -rf" in subject:
            event.data["allow"] = False

    return hooks
```

### Log All Tool Calls

```python
# .aru/plugins/tool_logger.py
from aru.plugins import Hooks, PluginInput

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()

    @hooks.on("tool.execute.before")
    def log_before(event):
        with open("aru_tool_log.txt", "a") as f:
            f.write(f"CALL: {event.tool_name}({event.args})\n")

    @hooks.on("tool.execute.after")
    def log_after(event):
        with open("aru_tool_log.txt", "a") as f:
            f.write(f"RESULT: {event.tool_name} -> {str(event.result)[:200]}\n")

    return hooks
```

### Notifications (Windows/macOS/Linux)

```python
# .aru/plugins/notifier.py
import sys, subprocess
from aru.plugins import Hooks, PluginInput

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()

    @hooks.on("permission.ask")
    def notify_permission(event):
        title = "Aru — Permission Required"
        msg = f"{event.data.get('category')}: {event.data.get('subject', '')[:80]}"
        if sys.platform == "win32":
            # Windows toast via PowerShell
            ps = f'[System.Windows.Forms.NotifyIcon]::new().ShowBalloonTip(3000,"{title}","{msg}","Info")'
            subprocess.Popen(["powershell", "-c", ps], creationflags=0x08000000)
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", f'display notification "{msg}" with title "{title}"'])
        else:
            subprocess.Popen(["notify-send", title, msg])

    return hooks
```

### Register Tools via Plugin

```python
# .aru/plugins/devops.py
from aru.plugins import Hooks, PluginInput, tool

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()

    @tool(description="Check service health")
    def health_check(service: str) -> str:
        """Args:
            service: Service name to check.
        """
        import httpx
        r = httpx.get(f"https://{service}.internal/health", timeout=5)
        return f"{service}: {r.status_code}"

    hooks.tools["health_check"] = health_check

    @tool(description="List running deployments")
    def list_deploys(env: str = "staging") -> str:
        """Args:
            env: Environment to query.
        """
        # ... implementation ...
        return "deploy-abc, deploy-def"

    hooks.tools["list_deploys"] = list_deploys
    return hooks
```

### Rewrite Bash Commands (rtk)

Port of an [OpenCode TS plugin](https://github.com/example/rtk):

```python
# .aru/plugins/rtk.py
import shutil, subprocess
from aru.plugins import Hooks, PluginInput

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()
    if not shutil.which("rtk"):
        return hooks  # rtk not installed, skip

    @hooks.on("tool.execute.before")
    def rewrite_bash(event):
        if event.tool_name not in ("bash", "shell"):
            return
        command = event.args.get("command")
        if not command:
            return
        try:
            result = subprocess.run(
                ["rtk", "rewrite", command],
                capture_output=True, text=True, timeout=5,
            )
            rewritten = result.stdout.strip()
            if rewritten and rewritten != command:
                event.args["command"] = rewritten
        except Exception:
            pass

    return hooks
```

## Plugin with Options

Configure options in `aru.json`:

```json
{
  "plugins": [
    "my-plugin",
    ["./plugins/notifier.py", {"sound": true, "notification": false}]
  ]
}
```

Access in your plugin:

```python
def plugin(ctx: PluginInput, options=None) -> Hooks:
    cfg = options or {}
    sound_enabled = cfg.get("sound", True)
    # ...
```

## Plugin Discovery

Plugins are loaded from multiple sources (in order, later overrides earlier):

| Source | Path / Config |
|--------|--------------|
| Global directory | `~/.agents/plugins/*.py`, `~/.aru/plugins/*.py` |
| Project directory | `.agents/plugins/*.py`, `.aru/plugins/*.py` |
| Config (explicit) | `aru.json` → `"plugins": [...]` |
| Installed package | `pip install aru-my-plugin` (entry point `aru.plugins`) |

Files starting with `_` are ignored.

## Publishing a Plugin as a Package

```toml
# pyproject.toml
[project]
name = "aru-my-plugin"
version = "0.1.0"

[project.entry-points."aru.plugins"]
my-plugin = "my_package.plugin"
```

```python
# my_package/plugin.py
from aru.plugins import Hooks, PluginInput

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()
    # ...
    return hooks
```

Users install with `pip install aru-my-plugin` — Aru discovers it automatically via entry points.

## OpenCode TS Plugin Bridge

For running OpenCode TypeScript plugins directly (without porting), drop the `opencode_bridge.py` plugin into `.aru/plugins/`. It:

1. Scans `.aru/plugins/*.ts` for TypeScript plugin files
2. Starts a Bun subprocess with an embedded JSON-RPC bridge
3. Registers TS tools and forwards hooks automatically

**Requirements:** [Bun](https://bun.sh/) in PATH.

On startup:

```
Loaded 1 plugin(s): opencode_bridge
  TS plugins via bridge: rtk, 1 hook(s)
```

### Supported OpenCode hooks via bridge

| OpenCode Hook | Forwarded | Notes |
|--------------|-----------|-------|
| `tool` (custom tools) | Yes | Callable by the LLM |
| `tool.execute.before/after` | Yes | Payload serialized as JSON |
| `permission.ask` | Yes | Can auto-approve/deny |
| `shell.env` | Yes | Inject env vars |
| `auth` / `provider` | No | Python-only; port manually |
| TUI plugins | No | Aru has no TUI plugin system |

### Porting TS to Python (recommended)

Porting is usually simpler and more reliable than running through the bridge:

```typescript
// OpenCode (TS)
export const MyPlugin: Plugin = async ({ $ }) => ({
    "tool.execute.before": async (input, output) => {
        if (input.tool !== "bash") return
        output.args.command = output.args.command.replace("npm", "bun")
    },
})
```

```python
# Aru (Python)
from aru.plugins import Hooks, PluginInput

def plugin(ctx: PluginInput, options=None) -> Hooks:
    hooks = Hooks()

    @hooks.on("tool.execute.before")
    def rewrite(event):
        if event.tool_name != "bash":
            return
        event.args["command"] = event.args["command"].replace("npm", "bun")

    return hooks
```

## Diagnostics

### Startup Messages

```
Loaded 2 custom tool(s): ping, plugins_info
Loaded 2 plugin(s): notifier, opencode_bridge (0 tool(s))
  TS plugins via bridge: rtk, 1 hook(s)
```

If a plugin doesn't appear, check:
- File is in `.aru/plugins/` (not `.aru/tools/`)
- File exports a `plugin()` function returning `Hooks`
- File doesn't start with `_`

### plugins_info Tool

Drop `plugins_info.py` in `.aru/tools/` to add a diagnostic tool:

```python
# .aru/tools/plugins_info.py
from aru.plugins import tool

@tool(description="List all loaded plugins and custom tools")
def plugins_info() -> str:
    from aru.runtime import get_ctx
    from aru.tools.codebase import TOOL_REGISTRY
    lines = []
    ctx = get_ctx()
    mgr = ctx.plugin_manager
    if mgr and mgr.loaded:
        lines.append(f"Plugins ({mgr.plugin_count}): {', '.join(mgr.plugin_names)}")
    lines.append(f"Tools ({len(TOOL_REGISTRY)}): {', '.join(sorted(TOOL_REGISTRY))}")
    return "\n".join(lines)
```

Ask the LLM: "list loaded plugins" and it will call this tool.

### Debug Logging

```bash
LOGLEVEL=DEBUG aru
```

Shows directories scanned, files loaded, tools registered, hooks wired, and any errors.
