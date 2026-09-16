# Security assumptions

## In-process plugins are trusted code

This is the central invariant, and Chassis does not soften it:

> In-process Python plugins are trusted code.

A plugin can import any module, open sockets, read files, and bypass every Chassis
API. **The policy engine is not a sandbox.** It governs what the *harness* performs
on behalf of a caller -- typically a model-issued tool call -- and nothing else.

True isolation requires a stronger execution boundary: a subprocess, a container, or
a remote service. Chassis's architecture does not prevent those (`Scope` and
`PluginRegistry` treat a plugin as an opaque unit with a lifecycle), but only
in-process execution is implemented today, and no part of the API implies otherwise.

## What policy does cover

Capability *availability* and *authorization* are different questions. A plugin may
provide `filesystem` without every caller being permitted to `filesystem.delete`.

```python
from chassis.policy import GrantPolicy
from chassis.tools import ToolPolicy

harness = Harness(policy=GrantPolicy([
    "filesystem.read",
    "filesystem.write:/workspace/output/**",
]))
```

- A grant without a resource pattern covers the permission at any resource.
- A scoped grant only matches resources matching its glob.
- **A scoped grant cannot satisfy an unscoped request**, so narrowing a grant never
  silently widens it.
- Permissions listed in `approval_required` are allowed only after an approval gate
  approves them; with no gate configured, approval fails closed.

The default policy (`AllowAllPolicy`) permits everything. That default is explicit
rather than implicit: Chassis is a harness, not a security product, and a policy
that silently denied everything would be just as misleading.

Policy is enforced at harness-controlled boundaries: tool execution today, and any
future boundary the harness mediates. It is evaluated before a replayed tool call is
answered, because a recording says what a tool returned, never whether the harness
was allowed to ask.

## Tool metadata

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

Declared permissions are what the harness checks; they are not a capability the
plugin can grant itself beyond its own declared scope, and they appear in
diagnostics so an operator can audit what a plugin asks for.

## Secrets

Plugins read secrets through a provider rather than the process environment, so a
future Vault/AWS/1Password provider does not change plugin code:

```python
value = (await ctx.secrets.get("openai.api_key")).reveal()
```

`ctx.secrets` returns the provider resolved for the plugin's own composition when
the plugin declares the `secrets` capability, and the harness's configured provider
otherwise. Either way plugin code never reaches into the process environment
directly.

Secret values must never appear in logs, traces, snapshots, diagnostics, replay
records, or exception strings. That is enforced by construction, not by convention:

- `SecretValue` refuses to render itself; `repr`, `str`, and diagnostics show
  `<redacted>`, and material leaves only through an explicit `reveal()`.
- `RedactingSecretProvider` wraps the configured provider, registering each value
  with the redactor the moment it is resolved. Redaction can only scrub what it
  knows about, so this is where that knowledge comes from.
- Errors carry the secret *name* and provider, never the material.
- Configuration snapshots contain no configuration values at all: configuration is
  represented by a hash over the redacted payload, and configuration is redacted by
  key name as well as by value.
- Recorded boundaries redact request fields whose names look sensitive.

Explicit tests assert each of these, including that a tool failure whose text
contains a secret does not reach telemetry or a snapshot unredacted.

**Known limitation:** a configuration value that is neither known to the redactor
nor under a secret-looking key name is hashed into `config_hash`. A digest is not
plaintext, but it is a confirmation oracle for someone who can guess candidates.
Redact such values through the provider rather than relying on the hash.

## Replay

Replay answers recorded boundaries. It does not virtualize clocks, randomness, HTTP,
databases, or any other external system, and it must not be treated as a
reproduction of production state. See [replay.md](replay.md).

## Diagnostics

Diagnostics describe configuration by key, never by value; provider payloads are
excluded from capability, tool, and hook descriptions; and everything they emit is
generated from authoritative state rather than scraped from logs.

## Checklist for reviewers

- Can a plugin resource outlive its scope? Only if a plugin bypasses `ctx`.
- Can policy be bypassed through the harness? Only by a plugin calling a tool
  directly, which is the trust assumption above.
- Can a secret reach a trace, snapshot, diagnostic, or error? There are explicit
  tests for each path.
- Does observability failure break runtime correctness? No: tracing failures are
  logged and the operation continues.
