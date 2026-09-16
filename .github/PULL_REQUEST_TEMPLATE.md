## What this changes

<!-- One paragraph. What behaviour, invariant, or documentation is different after this? -->

## Why

<!-- The problem it solves. If it fixes a bug, what was the failure mode? -->

## Checklist

- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest` all pass
- [ ] the change and its tests are in the same commit, and a plausible bug would fail them
- [ ] every caller was migrated and what the change obsoletes is deleted (no shims, aliases, or dead options)
- [ ] `docs/` and `CHANGELOG.md` updated if a public API, invariant, or limitation changed
- [ ] no placeholder implementations, TODOs-as-functionality, or pinned-to-current-text tests

## Notes for the reviewer

<!-- Trade-offs you decided, alternatives you rejected, and anything you are unsure about. -->
