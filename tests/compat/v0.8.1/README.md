# Sanitized 0.8.1 fixtures

These documents were produced by the **released 0.8.1 implementation** and are
what proves that 0.9.0 can read what 0.8.1 wrote. They are regenerated only from
0.8.1 code — never from the current tree — and are frozen here.

## Provenance

- Source: the `v0.8.1` tag (`03c772c849bf919828a506037f8e8eedf645bb84`), installed
  into a clean virtual environment as `chassis-harness 0.8.1`.
- Generator: [`generate.py`](generate.py), executed with that environment's
  interpreter (see the command in its docstring). It drives a real 0.8.1
  harness — real plugin setup, a real tool call through the executor's
  recording boundary, a real `ReplayChatModel` model call, real lifecycle and
  snapshot records — through the public 0.8.1 serialization (`to_dict()`/`save()`).
- Sanitization (applied by the generator, verified by assertions in it): fixed
  timestamps (`created_at` = 1760000000.0, `duration_seconds` = 0.25),
  deterministic stand-ins for runtime instance ids (`plugin_fixture_N`), UUIDs
  (`id_fixture_N`), run ids (`run_fixture_1`), and tool call ids
  (`call_fixture_1`). No secrets, paths, hostnames, or machine-specific data.

| File | SHA-256 |
| --- | --- |
| `runtime-snapshot.json` | `fe9d048740f8fb3931f6dfb05f8cb3c373a64b727835c6b27d54c60220fc3a8c` |
| `replay-recording.json` | `c8b276ada30b9a57d3078ce406b7c37232b52aa3c97f30aaea12f817a6a1d751` |
| `configuration.yaml` | `5839c987aa7cce30612d2767ea5b3298eb1f313982f41bd5dba8079a7a7dbf72` |

## What each fixture exercises

- **`runtime-snapshot.json`** — one `RuntimeSnapshot` record as 0.8.1 serialized
  it: two plugins with one resolved requirement, capability versions, the hash
  family, a redacted-metadata field, and the physical scope tree (provider
  *instance* ids and requirement selections). It declares no `format_version`
  (the pre-versioning shape, format 0).
- **`replay-recording.json`** — one `ReplaySession` recording as 0.8.1 wrote it:
  lifecycle events (mount, publish, draining, retired, shutdown), two runtime
  snapshot records, one tool interaction recorded by the executor
  (`redacted: false`, semantic content), and one model interaction recorded by
  `ReplayChatModel` (request messages, generations, `llm_output`).
- **`configuration.yaml`** — one declarative configuration document (schema
  `version: 1`) as 0.8.1 parsed it.

## What 0.9.0 must preserve when reading these

Generation identity and sequence; agent identity and revision; capability
versions; plugin identity and versions; the hash family; metadata keys;
replay boundary kind and key; redaction status (`redacted`); tool and model
result semantics (`content`, `artifact`, `error`, generations, `llm_output`);
and attribution fields (`generation_id`, `run_id`). See
`tests/test_format_compat.py`, which asserts exactly this.

What cannot be migrated: the *semantic* scope provider map of a snapshot
(0.8.1 recorded provider instance ids and persisted no instance-to-entry
mapping). The migration reconstructs the rest of the semantic scope tree and
reports empty provider maps rather than guessing
(`docs/compatibility.md#persisted-formats`).
