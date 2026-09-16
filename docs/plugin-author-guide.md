# Plugin author guide

A plugin declares what it provides and requires, and creates its effects through a
context that owns them. Nothing else is required: activation, ordering, cleanup,
task ownership, and generation consistency are the harness's job.

## Writing a plugin

```python
from chassis import DATABASE, PluginContext, PluginManifest, Plugin, plugin
from chassis.hooks import HookEvent, HookMode
from chassis.tools import ToolPolicy

@plugin(
    name="web-search",
    version="1.2.0",
    provides={"tools": "1.0.0"},
    requires={"database": ">=1,<2"},
    permissions=["network.fetch"],
)
async def web_search(ctx: PluginContext) -> None:
    database = ctx.require(DATABASE)          # resolved for this composition
    ctx.tools.register(
        search_tool,
        policy=ToolPolicy(permissions=("network.fetch",), idempotent=True),
    )
    ctx.hooks.register(HookEvent.AFTER_TOOL_EXECUTE, audit)
```

The class form uses the same machinery:

```python
class SearchPlugin(Plugin):
    manifest = PluginManifest(name="search", version="1.0.0", requires={"http": ">=1,<2"})

    async def setup(self, ctx: PluginContext) -> None:
        ctx.tools.register(self.tool())

    async def teardown(self, ctx: PluginContext) -> None:
        ...  # ordering-sensitive shutdown, before the scope unwinds
```

`teardown` is for graceful shutdown (flushing, draining). Ordinary cleanup belongs
in the scope, which is unwound automatically.

## The context

| API | Owned effect |
| --- | --- |
| `ctx.capabilities.provide(key, value, version=...)` | capability provider registration |
| `ctx.tools.register(tool, policy=...)` | tool registration |
| `ctx.hooks.register(event, handler, mode=..., priority=...)` | hook registration |
| `ctx.agents.register(runtime)` | agent runtime registration |
| `ctx.create_task(coro, name=...)` | background task |
| `ctx.cleanup(description, func)` | arbitrary reversible effect (use it to record the inverse of an operation that has no registry) |
| `ctx.require(key)` / `ctx.get(key)` | resolved provider object |
| `ctx.secrets` | secret provider (composition-resolved, else the harness default) |

Everything is reverted when the plugin unloads. `ctx.require` reads the providers
resolved for *this* composition, so a plugin never observes "whatever is current".

## Manifests

```python
PluginManifest(
    name="memory", version="2.1.0",
    provides={"memory": "2.1.0"},          # capability -> implementation version
    requires={"database": ">=1,<2"},       # capability -> PEP 440 specifier
    optional={"vector": ">=2"},            # unsatisfied optional requirements do not block
    permissions=["database.query"],
    config_version=1,
    metadata={...},
)
```

Versions and specifiers use `packaging`; a bare version is normalized to an exact
match. Manifests validate when they are constructed, so an unparseable requirement
fails immediately rather than at resolution time.

`permissions` declares what the plugin needs at harness boundaries. It is not a
sandbox: see [security.md](security.md).

## Tools

Tools are `langchain-core` objects. Chassis wraps rather than replaces them, so
names, descriptions, schemas, and execution behaviour stay upstream-compatible, and
`ToolNode`, `bind_tools`, and LangSmith keep working.

What Chassis adds is the semantics langchain-core does not model:

```python
ToolPolicy(
    permissions=("filesystem.write:/workspace/**",),
    idempotent=False,
    side_effects=("destructive",),
    timeout_seconds=30.0,
    cost_class="expensive",
    approval_required=True,
)
```

Execution flows through the harness boundary: hooks, policy, approval, budget and
deadline, tracing, then the tool. See [tool-execution](#tool-execution) below.

## Hooks

```python
ctx.hooks.register(HookEvent.BEFORE_TOOL_EXECUTE, redact, mode=HookMode.TRANSFORM, priority=-10)
ctx.hooks.register(HookEvent.BEFORE_TOOL_EXECUTE, refuse_dangerous, mode=HookMode.BAIL)
```

- `OBSERVE`: return value ignored; use for logging, metrics, audit.
- `TRANSFORM`: a returned mapping replaces the payload for the rest of the chain.
- `BAIL`: a truthy return stops the chain; at the tool and before-agent boundaries
  that refuses the operation.

Ordering is priority first, then registration order. Error semantics are per
registration: record and continue, or fail loudly with `HookExecutionError`.

Every declared event is dispatched at a real boundary:

| Boundary | Events | Refusable |
| --- | --- | --- |
| tool execution | `before_tool_execute`, `after_tool_execute`, `tool_error` | `before_tool_execute` |
| agent run | `before_agent_run`, `after_agent_run`, `agent_error` | `before_agent_run` |
| plugin lifecycle | `plugin_mounting`, `plugin_mounted`, `plugin_unmounting`, `plugin_unmounted` | no |
| runtime generations | `generation_published`, `generation_draining` | no |
| policy | `policy_decision` | no |

A refusal is reported as `PolicyDenied` with `reason="hook"`. Data-plane hooks
(tool and agent events, policy decisions) are dispatched against the run's
generation snapshot, so a run is observed by exactly the hooks that belong to the
composition it acquired. Control-plane hooks (lifecycle and generation events) are
dispatched against the live registry, because they describe the composition being
built rather than a running composition.

Control-plane hook failures are aggregated like cleanup failures and never abort a
lifecycle transition -- a failing observer must not leak the resource the transition
was about to release, so the `raise` error policy degrades to `record` there.

Hooks cover boundaries Chassis owns. Anything inside a graph belongs to LangGraph's
callbacks, and Chassis does not duplicate them.

## Tasks

```python
ctx.create_task(heartbeat(), name="heartbeat")
```

Owned by the plugin scope: cancelled and awaited on unload, with failures reported
rather than swallowed. Plugins are never encouraged to create unowned tasks.

## Testing

```python
from chassis.testing import TestHarness, FakeChatModel, fake_tool

async def test_search_plugin_registers_its_tool() -> None:
    async with TestHarness(plugins=[SearchPlugin]) as harness:
        assert harness.tools.names() == ("search",)

async def test_plugin_activates_when_its_dependency_appears() -> None:
    async with TestHarness(plugins=[MemoryPlugin]) as harness:
        assert harness.plugin_registry.instance("plugin-1") is None
        harness.provide(DATABASE, {"dsn": "..."})
        await harness.reconcile()
        assert harness.instance("plugin-1").state.value == "active"
```

`TestHarness` is a real harness with deterministic doubles: recording telemetry, an
in-memory secret provider, an explicit policy, and helpers such as
`install_tools`, `provide_secret`, and `capability_providers`.

## Tool execution

```text
agent → tool request → ToolExecutor → hooks → policy → approval → budget/deadline
      → tracing → langchain-core tool → normalized result
```

- Authorisation and budgeting run even when a call is replayed from a recording: a
  recording answers what a tool returned, never whether it was allowed to ask.
- A refusal (policy denial, unknown tool) becomes an error `ToolMessage` so the
  model learns it cannot do that and the graph can continue.
- Budget exhaustion propagates: the run is over budget and must stop.
- A tool that ran and failed returns a normalized failure result, so an agent loop
  can react; pass `raise_on_error=True` to raise `ToolExecutionError` instead.
- Synchronous graph invocation is refused rather than silently bypassing the
  boundary.

## Common mistakes

- **Reading capabilities lazily from "the current generation".** Use `ctx.require`
  during setup, or `runtime.context.capabilities` inside a graph node.
- **Starting a task with `asyncio.create_task`.** It will outlive the plugin; use
  `ctx.create_task`.
- **Writing an unregistration call.** `ctx.tools.register` already reverts.
- **Encoding ordering in configuration.** Order comes from declared capabilities.
- **Constructing a plugin requiring a single positional config mapping** if the
  plugin is installed declaratively: `Plugin.__init__(self, config=None)` already
  does that; subclasses with custom constructors must not be used declaratively.
