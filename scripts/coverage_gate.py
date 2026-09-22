"""Focused branch-coverage expectations for the boundaries that carry invariants.

The global branch-coverage floor is necessary but not sufficient: a high global
number can hide an untested resolver or replay boundary. This gate reads the
``.coverage`` database produced by ``pytest --cov=src/chassis --cov-branch`` and
enforces a per-area floor alongside the global one, using the same
statement-plus-branch math the coverage report prints.

Floors sit below the measured 0.5.x baseline so ordinary churn does not flip
the gate; raising them as coverage improves is encouraged.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coverage import Coverage

#: Area name -> module prefixes inside ``src/chassis`` -> minimum coverage %.
AREAS: dict[str, tuple[tuple[str, ...], float]] = {
    "lifecycle": (
        ("core/scope", "core/generation", "core/generations", "plugins/lifecycle"),
        90.0,
    ),
    "resolver": (("plugins/resolver",), 90.0),
    "registry": (
        ("capabilities/registry", "tools/registry", "plugins/registry", "hooks/registry"),
        85.0,
    ),
    "security": (("secrets/", "policy/"), 85.0),
    "replay": (("replay/",), 90.0),
    "telemetry": (("telemetry/",), 85.0),
}

#: Global floor: the measured 0.5.0 branch-coverage baseline.
GLOBAL_FLOOR = 91.0


def ratio(cov: Coverage, paths: list[str]) -> tuple[int, int]:
    """Covered and total statement+branch units, as the report computes them."""

    covered = total = 0
    for path in paths:
        # coverage 7.x exposes per-file report numbers through _analyze;
        # `numbers` is exactly what `coverage report` prints.
        numbers = cov._analyze(path).numbers
        covered += (numbers.n_statements - numbers.n_missing) + (
            numbers.n_branches - numbers.n_missing_branches
        )
        total += numbers.n_statements + numbers.n_branches
    return covered, total


def main() -> int:
    if not Path(".coverage").exists():
        print("no .coverage database; run: uv run pytest --cov=src/chassis --cov-branch")
        return 2

    cov = Coverage()
    cov.load()
    measured = [str(path) for path in cov.get_data().measured_files()]
    chassis_files = [path for path in measured if "src/chassis" in path]

    failures: list[str] = []
    covered, total = ratio(cov, chassis_files)
    global_pct = 100.0 * covered / total if total else 0.0
    print(f"global: {global_pct:.1f}% (floor {GLOBAL_FLOOR}%)")
    if global_pct < GLOBAL_FLOOR:
        failures.append(f"global coverage {global_pct:.1f}% < {GLOBAL_FLOOR}%")

    for area, (prefixes, floor) in AREAS.items():
        files = [
            path
            for path in chassis_files
            if any(prefix in path.split("src/chassis/", 1)[-1] for prefix in prefixes)
        ]
        area_covered, area_total = ratio(cov, files)
        pct = 100.0 * area_covered / area_total if area_total else 0.0
        print(f"{area}: {pct:.1f}% (floor {floor}%)")
        if pct < floor:
            failures.append(f"{area} coverage {pct:.1f}% < {floor}%")

    for failure in failures:
        print(f"FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
