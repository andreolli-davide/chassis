"""Release-channel safety for the 1.0 beta line."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

import yaml
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str = "release.yml") -> dict[str, Any]:
    workflow: dict[str | bool, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 loader interprets the Actions `on` key as true.
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return cast("dict[str, Any]", workflow)


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_the_development_version_is_the_canonical_second_1_0_beta() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = Version(project["version"])

    assert str(version) == "1.0.0b2"
    assert version.is_prerelease is True


def test_the_release_job_classifies_the_project_version() -> None:
    prepare = _workflow()["jobs"]["prepare"]
    script = _step(prepare, "Read the project version")["run"]

    assert prepare["outputs"]["prerelease"] == "${{ steps.version.outputs.prerelease }}"
    assert "version=$(uv run --no-project python" in script
    assert "Version(sys.argv[1]).is_prerelease" in script
    assert 'echo "prerelease=$prerelease" >> "$GITHUB_OUTPUT"' in script


def test_a_prerelease_cannot_advance_the_stable_github_or_docs_channels() -> None:
    jobs = _workflow()["jobs"]
    release_script = _step(
        jobs["github-release"], "Create the GitHub Release and attach the artifacts"
    )["run"]

    assert "release_flags=(--latest)" in release_script
    assert "release_flags=(--prerelease)" in release_script
    assert '"${release_flags[@]}"' in release_script

    docs = jobs["docs"]
    assert set(docs["needs"]) == {"prepare", "github-release"}
    assert "needs.prepare.outputs.prerelease != 'true'" in docs["if"]


def test_release_reuses_the_complete_ci_matrix_and_promotes_one_build() -> None:
    release = _workflow()
    jobs = release["jobs"]
    ci_jobs = _workflow("ci.yml")["jobs"]

    assert jobs["quality"]["uses"] == "./.github/workflows/ci.yml"
    assert set(jobs["bundle"]["needs"]) == {"prepare", "quality"}
    assert "uv build" not in (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert {
        "checks",
        "coverage",
        "minimum-dependencies",
        "latest-dependencies",
        "package",
        "audit",
        "documentation",
    } <= set(ci_jobs)

    ci_package = ci_jobs["package"]
    upload = _step(ci_package, "Upload the verified distributions")
    assert upload["with"]["name"] == "verified-distributions"
    assert ci_package["steps"][-1] == upload

    release_jobs = release["jobs"]
    for job_name in ("publish", "github-release"):
        download = next(
            step
            for step in release_jobs[job_name]["steps"]
            if step.get("uses") == "actions/download-artifact@v8"
        )
        assert download["with"]["name"] == "release-bundle"


def test_docs_deployment_is_explicit_and_archives_the_previous_stable_release() -> None:
    workflow = _workflow("docs.yml")
    docs = workflow["jobs"]
    ci = _workflow("ci.yml")["jobs"]

    triggers = workflow["on"]
    assert triggers["workflow_dispatch"]["inputs"]["deploy"]["default"] is False
    deploy_condition = (
        "inputs.deploy == true && ((github.event_name == 'push' && "
        "startsWith(github.ref, 'refs/tags/v')) || "
        "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'))"
    )
    assert docs["deploy"]["if"] == deploy_condition
    assert _step(docs["build"], "Configure Pages")["if"] == deploy_condition
    assert _step(docs["build"], "Upload the site")["if"] == deploy_condition
    stable_checkout = _step(docs["build"], "Check out the 0.9.1 documentation")
    assert stable_checkout["with"]["ref"] == "v0.9.1"
    assert stable_checkout["if"] == "inputs.deploy == true"
    archive_script = _step(docs["build"], "Build the immutable 0.9.1 archive")["run"]
    assert '--site-dir "$GITHUB_WORKSPACE/site/0.9.1"' in archive_script
    assert "mkdocs build --strict" in archive_script
    documentation = ci["documentation"]
    assert documentation["runs-on"] == "ubuntu-latest"
    docs_commands = "\n".join(step.get("run", "") for step in documentation["steps"])
    assert "uv sync --locked --no-dev --group docs" in docs_commands
    assert "uv run mkdocs build --strict" in docs_commands
