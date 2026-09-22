# Replay: what it does, and what it does not

Replay in Chassis is deliberately bounded. It records the boundaries Chassis
actually controls, and it makes no claim to reproduce arbitrary external systems.

## Modes

```python
from chassis.replay import ReplayMode, ReplaySession

recording = ReplaySession(mode=ReplayMode.RECORD, metadata={"dataset": "smoke"})
harness = Harness(replay=recording)
# ... run something ...
recording.save("recording.json")

replaying = ReplaySession.load("recording.json", mode=ReplayMode.REPLAY)
harness = Harness(replay=replaying)
```

| Mode | Behaviour |
| --- | --- |
| `live` | inert; nothing recorded, nothing replayed |
| `record` | every boundary interaction is appended |
| `replay` | a matching record answers; nothing is executed |

## Boundaries

Recorded:

- **tool requests and results**, at the execution boundary;
- **model requests and responses**, when the model is wrapped with `ReplayChatModel`;
- **interrupt values**, whenever an agent run pauses on one;
- **runtime snapshots**, on every generation publication, so a recording explains
  its own composition;
- **selected lifecycle events**: `plugin.mount`, `plugin.unmount`,
  `generation.publish`, `generation.draining`, `generation.retired`, and
  `harness.shutdown`.

Only the tool and model boundaries are *replayable*: they are the operations a
replay answers from. Interrupt, snapshot, and lifecycle records are attribution --
they explain which composition produced the recording and where a run paused --
and replay never fabricates an answer from them. A replayed run still pauses on its
own checkpointer.

Not recorded, and not replayable:

- clocks, randomness, process environment;
- HTTP APIs and other network services;
- databases and queues;
- filesystem state;
- anything else the harness does not mediate.

If an operation is outside these boundaries, replay does not pretend to cover it.

## Matching, and failing to match

Records are matched by `kind` and a canonical `key` covering the boundary identity:
for a tool call, the tool name and canonical arguments; for a model call, the model
identity, the request, normalized stop sequences, and every invocation option
(temperature, tools, structured output, provider options) — a value that cannot be
canonicalized deterministically is rejected rather than omitted. A recording
therefore cannot silently answer the wrong request. Consumption is per key and
cursor-aware (`has_remaining()`/`peek()`): repeated keys replay in recording
order and exhaust cleanly, and once a key's records are gone the call falls back
to live execution (or fails) instead of re-answering.

A replayed tool call follows the live boundary exactly: before hook,
authorization, approval, budget, and the same `tool.execute` span (tagged
`replayed`) all run, and the after hook observes the result. What is
*historical* in a replayed result is the semantic outcome — `content`,
`artifact`, `error`, and the recorded `redacted` flag. What is *current* is the
attribution — `duration_seconds`, `tool_call_id`, `generation_id`, and `run_id` —
stamped fresh from the call being served.

Unrecorded operations follow an explicit policy:

```python
ReplaySession(mode=ReplayMode.REPLAY, fallback=ReplayFallback.ERROR)  # default: raise ReplayMismatch
ReplaySession(mode=ReplayMode.REPLAY, fallback=ReplayFallback.LIVE)   # run live, and record it
```

`ReplayMismatch` carries the mismatch dimensions (`kind`, `key`, count of recorded
interactions of that kind) so a failed replay explains itself, and distinguishes    exhaustion from absence (`reason="exhausted"` when the key's records were
consumed, `"missing"` when none were ever recorded).

## Authorization still applies

A recording answers **what a tool returned**, never whether the harness was allowed
to ask. Hooks, policy, approval, and budgets run before a replayed result is
returned, and a policy denial raises exactly as it would live.

## The model boundary is opt-in

Chassis does not call models itself; graphs do, through the `MODEL` capability. A
boundary the harness does not mediate cannot be recorded honestly, so wrapping is
explicit:

```python
ReplayChatModel(session=session, inner=my_model, model_name="gpt-example")
```

In replay mode `inner` may be `None`, since nothing is delegated.

## Privacy

Recorded requests and responses are redacted: fields whose names look sensitive
(`api_key`, `authorization`, `token`, `password`, `secret`) are replaced, and the
session's `SecretRedactor` scrubs any known secret values. A recording should still
be treated as data worth protecting.

## What replay is for

- reproducing an evaluation run against a fixed model and tool behaviour;
- regression-testing an agent loop without network access;
- auditing exactly which tool calls and model requests produced an outcome.

## What replay is not for

- reproducing production state;
- security enforcement (it is a development and evaluation tool);
- claiming determinism the system does not have.

Unsupported operations fail explicitly by default. Choosing
`ReplayFallback.LIVE` is the explicit opt-in for mixing recorded boundaries with
live execution, and it is recorded as such.
