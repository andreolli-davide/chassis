# Contributing

Thanks for considering it. This project optimises for lifecycle correctness, resource
ownership, and honest documentation — in that order — and the rules below are what
keep those properties from eroding.

## Set up

```bash
uv sync
uv run pytest
```

[uv](https://docs.astral.sh/uv/) is the canonical project manager; `pyproject.toml`
and `uv.lock` are the canonical dependency state. Please do not add a parallel
`requirements.txt` or a second tool for the same job.

## The gates

Every pull request must leave these green. They are the same commands CI runs.

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright          # strict
uv run pytest
```

Additional jobs run in CI and should be run if you touch what they cover:

```bash
uv build                                        # the wheel must build
uv run --group docs mkdocs build --strict       # if you touched docs/
uvx --from actionlint-py actionlint .github/workflows/*.yml   # if you touched CI
```

## What a change should look like

- **One coherent unit per commit**, conventional-commit style
  (`feat(runtime): …`, `fix(lifecycle): …`, `docs: …`, `test: …`). A commit that mixes
  a behavioural change with unrelated cleanup will be asked to split.
- **The change and its tests travel together** when the tests define the same unit.
- **No placeholder implementations.** A stub, a `TODO: implement`, or a fake fallback
  presented as functionality is a defect, not a draft.
- **Clean cutovers.** Migrate every caller and delete what the change obsoletes:
  aliases, shims, re-exports, dead options. Do not leave a second way to do the same
  thing beside the new one.
- **Documentation is part of the change** when it introduces or alters a public API,
  an invariant, or a limitation.

## Tests worth writing

A test earns its place if a plausible bug would fail it. Prefer behaviour, boundaries,
invariants, transitions, and real errors over plumbing:

- good: "a leased generation's provider is not disposed while the lease is held";
- good: "a nested run cannot spend more than its parent has left";
- not useful: asserting that a function was called, that a field was copied, or that
  a value is non-empty.

Concurrency claims need concurrency tests: real tasks and event barriers, not
sequential calls to an async API (`tests/concurrency/` is the model to follow).

Documentation has one test: every relative link in `README.md`, `CHANGELOG.md`, and
`docs/` must resolve (`tests/test_docs_links.py`).

## Documentation rules

- `docs/` is the product documentation and is published with `mkdocs-material`
  (`mkdocs.yml`). Cross-boundary links (examples, changelog) are absolute GitHub URLs,
  because the site only serves `docs/`.
- State limitations as plainly as capabilities. Chassis does not claim to be a
  sandbox, does not claim deterministic replay of external systems, and does not claim
  a budget dimension the harness cannot enforce. Keep it that way.
- If a documented guarantee changes, update `docs/design.md` and the test that backs
  it in the same commit.

## Releasing

Maintainers only. Record the change in `CHANGELOG.md` under a `## [x.y.z]` heading, set
the same version in `pyproject.toml`, then tag `vX.Y.Z`. The release workflow refuses
to publish unless the tag, the project version, and the changelog agree.

## Conduct

Be specific, be kind, assume good faith. Argue about mechanisms and evidence, not
about people. Reviews here will name files, lines, and consequences; expect that and
extend the same precision back.
