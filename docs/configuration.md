# Configuration and reconciliation

Chassis supports declarative desired state, reconciled into runtime generations.

## Desired state

```yaml
version: 1
plugins:
  - id: primary-model
    plugin: openai-model
    config:
      model: example-model
  - id: search
    plugin: web-search
  - id: legacy
    plugin: web-search
    enabled: false
provider_preferences:
  database: postgres
```

- **`id` is a stable identity.** It must not change when the plugin or its
  configuration changes, or reconciliation sees a remove plus an add instead of a
  replace.
- `plugin` names an implementation resolved through the catalog.
- `enabled: false` parks an entry without deleting it, so it is treated as absent.
- `provider_preference` disambiguates requirements this entry has
  (`consumer:database`), and the top-level map disambiguates globally.
- A `provider_preference` is *resolution intent*, not plugin configuration, so
  changing it never derives `REPLACE`. It is applied before the next
  reconciliation; if it selects a different provider, the consumer is rebuilt and
  reported as `REWIRED`, and if it still selects the same provider nothing changes.
- YAML and JSON ordering never defines dependency semantics: capabilities do.

Configuration loads from a mapping, a YAML or JSON string, or a path:

```python
harness.apply_config("harness.yaml")
harness.apply_config({"plugins": [{"id": "model", "plugin": "openai-model"}]})
```

It can also be handed to the constructor, applied once when the harness starts:

```python
async with Harness("harness.yaml") as harness:
    result = await harness.agents.invoke("research-agent", {"messages": [...]})
```

## Composition scopes

Declarative configuration describes a **flat** composition: every entry it names is
declared in the root scope. Composition scopes are declared programmatically in
0.3, and a harness may mix the two — a configuration file for the root, scopes
created in code:

```python
harness.apply_config("harness.yaml")                       # entries in the root scope
research = harness.composition.child("research")
research.install(SearchPlugin(), entry_id="search")
await harness.reconcile()
```

A scope-aware configuration schema is a candidate for a later release; the scope
primitive is stable, and only the file format is deferred. See
[scopes.md](scopes.md) for the model.

## The catalog

```python
harness.register_plugin_type("openai-model", OpenAIModelPlugin)
```

The mapping is explicit rather than import-time registration, so composition does
not depend on which modules happened to be imported.

## Reconciliation

`apply_config` derives operations and applies them through the same
`install`/`uninstall`/`reconcile` path as programmatic use:

| Action | When |
| --- | --- |
| `ADD` | entry is desired but not installed |
| `REMOVE` | entry is installed but no longer desired |
| `UNCHANGED` | same implementation, same configuration |
| `REPLACE` | implementation or configuration changed |
| `RECONFIGURE` | reserved; never emitted today |

`RECONFIGURE` is deliberately not implemented. In-place mutation of a running
plugin cannot be made safe while older generations may still depend on the previous
semantics, so a configuration change conservatively becomes `REPLACE`: a new
instance in the next generation, while the old one stays reachable to the runs that
already hold it.

Results are ordered by entry id, so a reconciliation run is reproducible and its
diagnostics are stable.

The transaction is the generation publication:

```text
desired state → diff → resolve → mount candidates → validate → publish
                                                              ↓
                                                    old generation drains
```

A failed candidate never becomes visible: the previously published generation stays
current, and the candidate's effects are rolled back.

## Programmatic use

```python
harness.install(MyPlugin(config), entry_id="my-plugin")
harness.provide(MODEL, model_instance)          # application-held capability
harness.uninstall("my-plugin")
await harness.reconcile()                       # or await harness.ensure_ready()
```

`install`, `provide`, and `uninstall` are synchronous desired-state changes; they
take effect on the next reconciliation, which asynchronous entry points such as
agent invocation apply automatically.

## Drift and diagnostics

```python
harness.diagnostics.config()          # the applied configuration
harness.diagnostics.desired_state()   # what reconciliation would change
```

`desired_state` reports drift between the applied configuration and what is
installed -- an entry added outside the configuration appears as `REMOVE`. A freshly
applied configuration converges and reports `UNCHANGED`.

## Out of scope

Arbitrary Python hot-module replacement. Chassis supports runtime *provider and
configuration* replacement; replacing a module or class identity while instances
still reference the previous one is a separate problem with no safe answer while
active generations hold the old semantics.
