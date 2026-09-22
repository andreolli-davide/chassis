"""Compare the installed Chassis public API against a machine-readable baseline.

This is the enforcement side of the compatibility policy
(``docs/compatibility.md``). It introspects exactly the documented public
surface declared in ``tests/compat/public-api.json`` — never whatever happens to
be importable — and describes each name's kind, callable signature, enum values,
and public dataclass/Pydantic fields.

Commands::

    python scripts/api_compat.py describe [--surface FILE] [--write FILE]
    python scripts/api_compat.py check BASELINE [--surface FILE]
    python scripts/api_compat.py compare OLD NEW

``describe`` emits (or writes) a descriptor document for the *installed*
``chassis``. To record the 0.8.1 baseline, run ``describe --write`` in an
environment where the 0.8.1 distribution is installed — never from a newer
tree. ``check`` describes the installed package and compares it with a
baseline; ``compare`` diffs two descriptor documents.

The comparison reports incompatible changes — removed public names, moved
public names, incompatible callable signatures, changed enum values, changed
public dataclass or model fields — and exits non-zero when it finds any. Purely
additive names and additive optional parameters are reported as additions and
are compatible. Output is deterministic: sorted keys and stable ordering
throughout.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import inspect
import json
import platform
import sys
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SURFACE = ROOT / "tests" / "compat" / "public-api.json"
DESCRIPTOR_FORMAT_VERSION = 1
REPORT_FORMAT_VERSION = 1

_PARAM_KINDS = {
    inspect.Parameter.POSITIONAL_ONLY: "POSITIONAL_ONLY",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "POSITIONAL_OR_KEYWORD",
    inspect.Parameter.VAR_POSITIONAL: "VAR_POSITIONAL",
    inspect.Parameter.KEYWORD_ONLY: "KEYWORD_ONLY",
    inspect.Parameter.VAR_KEYWORD: "VAR_KEYWORD",
}


def _type_name(annotation: Any) -> str:
    """Stable rendering of a type annotation."""

    if isinstance(annotation, str):
        return annotation
    return getattr(annotation, "__qualname__", str(annotation))


def _params(obj: Any) -> list[dict[str, Any]]:
    """Signature parameters as structured, comparable entries."""

    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return []
    return [
        {
            "name": parameter.name,
            "kind": _PARAM_KINDS[parameter.kind],
            "default": parameter.default is not inspect.Parameter.empty,
        }
        for parameter in signature.parameters.values()
        if parameter.name not in {"self", "cls"}
    ]


def _public_methods(cls: type) -> dict[str, Any]:
    """Public methods and properties of a class, across its MRO."""

    collected: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        for attribute, value in vars(klass).items():
            if attribute.startswith("_"):
                continue
            if isinstance(value, property):
                collected[attribute] = {"params": [], "property": True}
            elif inspect.isfunction(value) or isinstance(value, (staticmethod, classmethod)):
                target = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
                collected[attribute] = {"params": _params(target), "property": False}
    return dict(sorted(collected.items()))


def _dataclass_fields(cls: type) -> dict[str, Any]:
    return {
        field.name: {
            "type": _type_name(field.type),
            "default": (
                field.default is not dataclasses.MISSING
                or field.default_factory is not dataclasses.MISSING
            ),
        }
        for field in dataclasses.fields(cls)
        if not field.name.startswith("_")
    }


def _model_fields(cls: type) -> dict[str, Any]:
    return {
        name: {"type": _type_name(field.annotation), "default": not field.is_required()}
        for name, field in sorted(cls.model_fields.items())
        if not name.startswith("_")
    }


def describe_name(obj: Any) -> dict[str, Any]:
    """Descriptor for one public name: kind, signature, enum values, fields."""

    if isinstance(obj, type) and issubclass(obj, Exception):
        return {
            "kind": "exception",
            "bases": [base.__qualname__ for base in obj.__bases__],
            "params": _params(obj.__init__),
        }
    if isinstance(obj, type) and issubclass(obj, Enum):
        members = getattr(obj, "__members__", {})
        return {
            "kind": "enum",
            "members": {
                name: (
                    member.value
                    if isinstance(member.value, (str, int, float, bool))
                    else str(member.value)
                )
                for name, member in sorted(members.items())
            },
        }
    if isinstance(obj, type) and dataclasses.is_dataclass(obj):
        return {
            "kind": "dataclass",
            "params": _params(obj),
            "fields": _dataclass_fields(obj),
            "methods": _public_methods(obj),
        }
    if isinstance(obj, type) and hasattr(obj, "model_fields"):
        return {
            "kind": "model",
            "fields": _model_fields(obj),
            "methods": _public_methods(obj),
        }
    if isinstance(obj, type):
        return {"kind": "class", "params": _params(obj), "methods": _public_methods(obj)}
    if inspect.isroutine(obj):
        return {"kind": "function", "params": _params(obj)}
    return {"kind": "constant", "type": type(obj).__qualname__}


def describe_surface(surface: dict[str, Any]) -> dict[str, Any]:
    """Descriptor document for the installed package, for the documented surface."""

    from importlib.metadata import PackageNotFoundError, version

    try:
        package_version = version("chassis-harness")
    except PackageNotFoundError:  # pragma: no cover - only outside an install
        package_version = "0.0.0"

    modules: dict[str, Any] = {}
    for module_name, names in sorted(surface["modules"].items()):
        module = importlib.import_module(module_name)
        entries: dict[str, Any] = {}
        for name, meta in sorted(names.items()):
            if not hasattr(module, name):
                # Absent from this install (a name added after the baseline was
                # recorded): an addition when compared, never a guess.
                continue
            descriptor = describe_name(getattr(module, name))
            descriptor["stability"] = meta.get("stability", "stable")
            entries[name] = descriptor
        modules[module_name] = dict(sorted(entries.items()))

    return {
        "format_version": DESCRIPTOR_FORMAT_VERSION,
        "package_version": package_version,
        "python_version": platform.python_version(),
        "modules": modules,
    }


# --------------------------------------------------------------------- comparison


def _incompatible_params(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[str]:
    """Why one callable's signature is incompatible with the recorded one."""

    problems: list[str] = []
    current = {parameter["name"]: parameter for parameter in new}
    for parameter in old:
        name = parameter["name"]
        if name not in current:
            problems.append(f"parameter {name!r} was removed or renamed")
            continue
        found = current[name]
        if found["kind"] != parameter["kind"]:
            problems.append(
                f"parameter {name!r} changed kind {parameter['kind']} -> {found['kind']}"
            )
        if parameter["default"] and not found["default"]:
            problems.append(f"parameter {name!r} lost its default value")
    recorded = {parameter["name"] for parameter in old}
    for parameter in new:
        if parameter["name"] not in recorded and not parameter["default"]:
            problems.append(f"required parameter {parameter['name']!r} was added without a default")
    return problems


def _compare_name(
    module: str, name: str, old: dict[str, Any], new: dict[str, Any]
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []

    def violation(kind: str, detail: str) -> None:
        violations.append({"kind": kind, "module": module, "name": name, "detail": detail})

    if old["kind"] != new["kind"]:
        violation("changed_kind", f"kind changed {old['kind']} -> {new['kind']}")
        return violations

    if old["kind"] == "enum":
        for member, value in sorted(old["members"].items()):
            if member not in new["members"]:
                violation("changed_enum_value", f"enum member {member!r} was removed")
            elif new["members"][member] != value:
                violation(
                    "changed_enum_value",
                    f"enum member {member!r} changed value {value!r} -> {new['members'][member]!r}",
                )

    if old["kind"] == "exception":
        if old["bases"] != new["bases"]:
            violation(
                "changed_kind",
                f"exception bases changed {old['bases']} -> {new['bases']}",
            )
        for problem in _incompatible_params(old.get("params", []), new.get("params", [])):
            violation("changed_signature", problem)

    if "params" in old and old["kind"] != "exception":
        for problem in _incompatible_params(old.get("params", []), new.get("params", [])):
            violation("changed_signature", problem)

    for field, spec in sorted(old.get("fields", {}).items()):
        if field not in new.get("fields", {}):
            violation("changed_field", f"public field {field!r} was removed or renamed")
            continue
        found = new["fields"][field]
        if found["type"] != spec["type"]:
            violation(
                "changed_field",
                f"public field {field!r} changed type {spec['type']} -> {found['type']}",
            )
        if spec["default"] and not found["default"]:
            violation("changed_field", f"public field {field!r} lost its default value")
    for field, spec in sorted(new.get("fields", {}).items()):
        if field not in old.get("fields", {}) and not spec["default"]:
            violation("changed_field", f"required public field {field!r} was added")

    for method, spec in sorted(old.get("methods", {}).items()):
        if method not in new.get("methods", {}):
            violation("removed_method", f"public method {method!r} was removed")
            continue
        found = new["methods"][method]
        for problem in _incompatible_params(spec.get("params", []), found.get("params", [])):
            violation("changed_signature", f"method {method!r}: {problem}")

    return violations


def compare_documents(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Incompatible changes from ``old`` to ``new``, deterministically ordered."""

    violations: list[dict[str, Any]] = []
    additions: list[dict[str, str]] = []
    new_names_by_module = {module: set(entries) for module, entries in new["modules"].items()}

    for module, entries in sorted(old["modules"].items()):
        for name, descriptor in sorted(entries.items()):
            current = new["modules"].get(module, {}).get(name)
            if current is None:
                holders = sorted(
                    other
                    for other, names in new_names_by_module.items()
                    if name in names and other != module
                )
                if holders:
                    violations.append(
                        {
                            "kind": "moved_name",
                            "module": module,
                            "name": name,
                            "detail": f"moved to {holders[0]}",
                            "to": holders[0],
                        }
                    )
                else:
                    violations.append(
                        {
                            "kind": "removed_name",
                            "module": module,
                            "name": name,
                            "detail": "removed from the public surface",
                        }
                    )
                continue
            violations.extend(_compare_name(module, name, descriptor, current))

    for module, entries in sorted(new["modules"].items()):
        for name in sorted(entries):
            if name not in old["modules"].get(module, {}):
                additions.append({"module": module, "name": name})

    violations.sort(key=lambda item: (item["kind"], item["module"], item["name"], item["detail"]))
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "compatible": not violations,
        "violations": violations,
        "additions": additions,
    }


# -------------------------------------------------------------------------- driver


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--surface",
        type=Path,
        default=DEFAULT_SURFACE,
        help="documented public surface map (default: tests/compat/public-api.json)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    describe = subcommands.add_parser("describe", help="describe the installed surface")
    describe.add_argument("--write", type=Path, help="write the descriptor here instead of stdout")

    check = subcommands.add_parser("check", help="compare the installed surface with a baseline")
    check.add_argument("baseline", type=Path)

    compare = subcommands.add_parser("compare", help="compare two descriptor documents")
    compare.add_argument("old", type=Path)
    compare.add_argument("new", type=Path)

    arguments = parser.parse_args(argv)
    surface = _load(arguments.surface)

    if arguments.command == "describe":
        document = describe_surface(surface)
        rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if arguments.write:
            arguments.write.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0

    if arguments.command == "check":
        report = compare_documents(_load(arguments.baseline), describe_surface(surface))
    else:
        report = compare_documents(_load(arguments.old), _load(arguments.new))

    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["compatible"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised through its tests
    sys.exit(main())
