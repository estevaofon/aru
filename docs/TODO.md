# Aru TODO

Structural follow-ups identified during the plan-mode deadlock investigation
(April 2026). Each item closes a class of bugs rather than a single instance.
Listed in recommended order — #1 has the highest ROI, #3 is the cheapest to
land.

> **A note on references.** Where this doc cites OpenCode (MIT licensed),
> short code excerpts are reproduced with attribution for design
> discussion. References to Claude Code describe architectural patterns
> in our own words, without reproducing its source — that codebase is
> proprietary and we only link to public behavior.

---

## 1. Partition tool dispatch by `is_concurrency_safe`

**Status:** not started
**Size:** medium (~100-200 lines, monkey-patch on Agno's dispatch path)
**Priority:** high — closes a class of races

### Problem

Agno dispatches every round of tool calls in parallel via `asyncio.gather`
(`agno/models/base.py`: *"Create and run all function calls in parallel"*).
When a single batch contains a mix of read-only tools and side-effecting
tools (edit/write/bash/plan-mode/delegate), races can emerge:

- Multiple permission prompts fighting for stdin
- Nested `Live` contexts (plan mode sub-runner vs outer turn) — partially
  fixed by the plan-mode refactor, but the underlying parallelism is still
  there for other nested-agent paths
- Stream arg-delta mis-attribution between tool calls (suspected cause of
  the `rtk npm run build` phantom prefix from the original bug report)

The plan-mode refactor neutralized the *worst* instance of this by making
`enter_plan_mode` a flag flip, but the class is still open.

### Reference: how Claude Code handles it

From our own exploration of Claude Code's CLI (see our earlier research
notes, not reproduced here): every tool exposes a predicate that takes
the tool's input and returns whether it's safe to run concurrently with
other tools. The predicate is **input-dependent** — the same tool can be
safe for one call and unsafe for another (e.g. a read-only shell command
vs. a destructive one).

The dispatcher partitions each batch into contiguous runs of safe/unsafe
tools: safe runs go through a parallel executor (bounded concurrency
cap), unsafe runs execute serially, and the original emission order
from the model is preserved. So a batch like `[read, read, edit, read,
read]` becomes `[read‖read] → edit → [read‖read]`.

### Proposed approach for Aru

Two possible enganche points, pick one:

**Option A — monkey-patch Agno's dispatch.**
Patch `Model.run_function_calls` (or whatever the method is called in the
installed Agno version — need to verify) to intercept the list of
`FunctionCall` objects, classify each by consulting a `tool_registry`
metadata dict, then reorder the `asyncio.gather` call into serial and
parallel segments.

**Option B — wrapper in `agent_factory._wrap_tools_with_hooks`.**
Add an `asyncio.Lock` per session that unsafe tools acquire before
running. Safe tools never touch the lock, so parallelism is preserved for
them. Simpler to reason about; loses the "partition by contiguous run"
optimization but still correct.

**Recommendation:** start with Option B. It's 20 lines and unblocks 80% of
the wins. Option A can come later if we need finer control over ordering.

### Tool classification (starting point)

Safe (run in parallel):
- `read_file`, `read_files`, `glob_search`, `grep_search`, `list_directory`
- `web_search`, `web_fetch`, `rank_files`
- `enter_plan_mode` (flag flip, no side effect on disk/console)
- `create_task_list`, `update_task`, `update_plan_step` (in-memory only)

Unsafe (serialize):
- `edit_file(s)`, `write_file(s)`, `bash` — disk mutation or shell
- `delegate_task` — spawns sub-agent with its own Live
- `exit_plan_mode` — shows approval prompt, needs exclusive stdin

### Tests to add

- Mixed batch of 3 reads + 1 edit runs the reads concurrently, edit
  serially
- Two `edit_file` calls in a batch never overlap (lock contention)
- Sub-agent spawn via `delegate_task` doesn't race with sibling calls

---

## 2. Non-blocking permission prompts (future + callback)

**Status:** not started
**Size:** large — touches `permissions.py`, `display.py`, `runner.py`
**Priority:** medium — eliminates fragility rather than a specific bug

### Problem

Today's permission flow is synchronous and blocks the main event loop:

```python
with ctx.permission_lock:
    if ctx.live: ctx.live.stop()
    if ctx.display: ctx.display.flush()
    ctx.console.print(Panel(...))
    choice = select_option(...)   # blocks until user answers
    if ctx.live: ctx.live.start()
```

Side effects of this pattern:

- Must stop/start `Live` around every prompt (fragile; we've had
  nested-Live bugs because of it)
- The entire async event loop is stuck while the user reads the prompt —
  other progress indicators freeze
- The thread offload in `select.py` is a workaround, not a fix
- The lock serializes prompts, but doesn't prevent a tool that *already
  started running* from mid-execution-prompting while another prompt is
  active

### Reference: OpenCode's approach

`packages/opencode/src/permission/index.ts:193` — uses Effect's
`Deferred` primitive:

```typescript
const deferred = yield* Deferred.make<void, RejectedError | CorrectedError>()
pending.set(id, { info, deferred })
yield* bus.publish(Event.Asked, info)
return yield* Effect.ensuring(
  Deferred.await(deferred),  // suspends THIS tool only
  Effect.sync(() => { pending.delete(id) }),
)
```

Key property: `Deferred.await` suspends the **calling fiber**, not the
whole event loop. Other in-flight tools keep progressing. The TUI reads
`Event.Asked` from a message bus and renders the prompt in its own
component tree. When the user answers, `permission.reply(id, decision)`
succeeds/fails the specific `Deferred`.

### Proposed approach for Aru

Convert `check_permission` from sync-blocking to async-suspending:

```python
async def check_permission(category, subject, display_details) -> bool:
    ...
    future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    ctx.pending_permissions[request_id] = future
    ctx.permission_requested.send_nowait({
        "id": request_id,
        "category": category,
        "subject": subject,
        "display": display_details,
    })
    return await future  # suspends this tool coroutine only
```

A separate UI task listens on the permission channel, renders prompts
one at a time (queue), reads the user's answer, and resolves the future.
Other tools keep running.

### Blockers to resolve first

- `check_permission` is currently called from sync code paths inside
  tool implementations (e.g., `_edit_file_tool`). These need to either
  become async throughout or use a bridge. Agno's `_thread_tool` wrapper
  complicates this.
- The Rich `Live` display doesn't have a "render this in a sibling
  region" concept. Need to either replace Live entirely, or add a
  secondary output lane for permission panels.
- Queue semantics: if the user is answering prompt A and prompt B
  arrives, how do we show B? Stack them? FIFO? OpenCode queues by
  session and serializes, which is probably the right default.

### Incremental path

1. First make `check_permission` async (no behavior change, just signature)
2. Then route through a future instead of blocking on `console.input`
3. Finally, add the out-of-band rendering lane

Each step is testable and reversible.

---

## 3. Doom-loop detector

**Status:** not started
**Size:** small (~30 lines)
**Priority:** low — defensive, not blocking anything

### Problem

When a model gets stuck retrying the same tool with the same input — for
example, ignoring a tool error and calling `bash("npm test")` 5 times
hoping the 6th will pass — it burns tokens and wastes time without user
awareness. Aru has no automatic detection.

### Reference: OpenCode's implementation

`packages/opencode/src/session/processor.ts:187`:

```typescript
const DOOM_LOOP_THRESHOLD = 3
const recentParts = parts.slice(-DOOM_LOOP_THRESHOLD)
if (
  recentParts.length === DOOM_LOOP_THRESHOLD &&
  recentParts.every(
    (part) =>
      part.type === "tool" &&
      part.tool === value.toolName &&
      JSON.stringify(part.state.input) === JSON.stringify(value.input),
  )
) {
  yield* permission.ask({
    permission: "doom_loop",
    patterns: [value.toolName],
  })
}
```

Threshold of 3: the third identical call pauses and asks the user "this
tool is looping on the same input, continue?" If the user approves, it
keeps going (handy when the loop is legitimate — e.g., polling). If the
user denies, the runner injects an error asking the model to stop.

### Proposed approach for Aru

Plug into the runner's tool-dispatch loop (or the `_wrap_tools_with_hooks`
wrapper in `agent_factory.py`). Track the last N `(tool_name, args_hash)`
tuples per session. If the last 3 match exactly, pause and ask via the
permission prompt before executing the 4th.

Where to store state:

- `session.recent_tool_calls: list[tuple[str, str]]` — name + hash, capped
  at 10 most recent
- Cleared on each user turn boundary
- Incremented in the tool wrapper before execution

Edge cases:

- Tools that are *supposed* to repeat (polling, `sleep`, waiting on a
  process) — the user prompt handles this by letting them approve
- Tools with large payloads — hash the JSON of the args, not the raw
  strings, so whitespace differences still count as identical
- `update_plan_step` / `update_task` — these legitimately fire with
  different indexes every call, and the hash captures that

### Tests to add

- 3 identical `bash` calls in a row triggers the prompt
- 3 `read_file` calls with different paths don't trigger
- After the user approves, a 4th identical call goes through without
  re-prompting for one more cycle
- New user turn resets the counter

---

## Notes on prioritization

**Do #1 first.** It's the closest to a real bug we've already seen — the
`rtk npm run build` and the parallel tool deadlock. The plan-mode refactor
narrowed the impact, but any future "enter some mode then call other tool
in parallel" pattern will hit the same class.

**Do #3 if you have a spare hour.** It's cheap, independent, and gives
real-time protection against API bill blowups. Low risk because the only
side effect is an extra permission prompt.

**Do #2 only when you're ready to commit to a larger refactor.** Most of
the fragility it addresses is latent — things that *could* break under
stress but currently work. Starting it half-way is worse than not starting.
