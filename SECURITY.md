# Security policy

## Supported versions

The latest published 1.0 beta is 1.0.0b1 and supports Python 3.12 and 3.13.
The 1.0.0b2 source candidate has the same tested support range but is not
published yet. Newer Python versions are not supported until they are added to
the release test matrix.

The latest stable release, currently 0.9.1, is supported. Security fixes land on
`main` and ship in the next appropriate release. 1.0 betas are evaluation
releases: reported vulnerabilities are fixed in a subsequent beta or final
release, while stable users receive the fix on the supported stable line when
affected. This policy will be replaced by the declared 1.0 support horizon
before 1.0.0 final.

## Reporting a vulnerability

Report it privately through GitHub:
[Security advisories → Report a vulnerability](https://github.com/andreolli-davide/chassis/security/advisories/new).
Please do not open a public issue for a suspected vulnerability.

Useful in a report: the version (`python -c "import chassis; print(chassis.__version__)"`),
a minimal reproduction, and what you expected instead. You will get an
acknowledgement, an assessment, and a fix or an explanation — no fixed deadline is
promised, but silence is not the intent.

## What counts as a vulnerability here

Chassis is a composition and lifecycle layer, so the security-relevant surface is
narrow and worth stating precisely.

**In scope:**

- a secret value reaching a snapshot, diagnostic, replay record, trace, telemetry
  event, or public exception string (the redactor failing to redact, or a path that
  bypasses it);
- a plugin effect, task, or registration surviving its owning scope — including after
  a failed setup, an unload, or a harness shutdown;
- a scope being disposed while a live generation still reaches it, or a plugin being
  reachable from a generation after disposal;
- a run observing a composition other than the generation it acquired;
- a tool call or model invocation bypassing the policy, approval, or budget boundary;
- arbitrary code execution reachable from declarative configuration alone.

**Out of scope, because it is the documented design:**

- in-process Python plugins doing anything at all: **plugins are trusted code**. The
  policy engine controls harness-mediated operations; it is not a sandbox, and it is
  not presented as one;
- prompt injection, model misbehaviour, and anything decided by a model;
- denial of service caused by a plugin the operator installed;
- replay being non-deterministic for clocks, randomness, networks, databases, and the
  filesystem ([replay.md](docs/replay.md));
- an operator leaking secrets through configuration they wrote themselves.

The full trust model, including the future-isolation path, is in
[security.md](docs/security.md).

## Verifying the claims

Several of the guarantees above are executable rather than aspirational:
`tests/test_public_api.py` pins the public surface, `tests/concurrency/` exercises
generation lifetime and shutdown races, `tests/secrets/` covers redaction, and
`docs/design.md` names the test behind each guarantee. Run `uv run pytest` in a
checkout of the tag you are auditing.
