# Candidate 1.0.0b2 format fixtures

These documents are the second 1.0 beta candidates for every persisted format
family Chassis documents. They are generated from the current `1.0.0b2`
implementation; unlike the historical `v0.8.1` fixtures, they are review
targets rather than a permanent compatibility baseline. The b1 fixtures remain
unchanged under `tests/compat/v1.0.0b1/`.

Regenerate them only through the checked-in generator:

```bash
uv run python tests/compat/v1.0.0b2/generate.py
```

CI runs the same program with `--check`. The generator drives a real harness and
then replaces volatile instance ids and clocks with deterministic fixture
values and orders set-derived diagnostics by semantic identity. `SHA256SUMS`
records every generated document, and `manifest.json` maps each format family
to its fixture and version.

The set covers:

- runtime snapshots and replay recordings, both with exact reader round-trips;
- declarative configuration schema version 1;
- planning output;
- reconciliation output, including the resolution plan and impact analysis;
- configuration-apply diagnostics;
- generation-pressure diagnostics, including resource reachability.

The final `1.0.0` release will regenerate and promote reviewed fixtures as the
permanent baseline. It must not silently relabel these beta candidates.
