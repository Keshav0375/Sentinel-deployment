"""Offline tests for the inline `run:` blocks of ci_app_deployment.yml (deployment §3.1).

Each test lifts one step's `run: |` block out of the workflow text and runs it the way the
runner does (`bash -eo pipefail`). The workspace is a temp dir, and stub `az`/`psql`
come first on PATH and log their argv, so ordering and failure paths are asserted
without Azure. $GITHUB_ENV is read back with the runner's own rules. Needs bash, jq, zip
and zipinfo, as ubuntu-latest has.
"""

import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from tests.test_deploy_scripts import parse_github_env

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci_app_deployment.yml"
WORKFLOW_TEXT = WORKFLOW.read_text()


def run_block(step_id: str) -> str:
    """The dedented `run: |` script of the step whose `id:` is `step_id`."""
    lines = WORKFLOW_TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"id: {step_id}")
    for i in range(start + 1, len(lines)):
        match = re.match(r"^(\s+)run: \|$", lines[i])
        if match:
            break
        assert not lines[i].lstrip().startswith("- name:"), (
            f"{step_id} has no run block"
        )
    indent = len(match.group(1)) + 2
    body = []
    for line in lines[i + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$(basename "$0") $*" >>"$STUB_LOG"
name="$(basename "$0")"
case "$name $1 $2" in
  "az account get-access-token") echo "tok-fake" ;;
esac
if [[ "$name" == psql ]]; then cat >>"$STUB_LOG"; fi
case "$name" in
  az) fail="${STUB_FAIL_AZ:-}" ;;
  *) fail="${STUB_FAIL_PSQL:-}" ;;
esac
if [[ -n "$fail" && "$*" == *"$fail"* ]]; then
  echo "$name: stubbed failure" >&2
  exit 3
fi
exit 0
"""


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str
    env: dict[str, str]
    calls: list[str]
    workspace: Path


def run_step(tmp_path: Path, step_id: str, env: dict[str, str] | None = None) -> Run:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    scripts = workspace / ".github" / "scripts"
    if not scripts.exists():
        shutil.copytree(ROOT / ".github" / "scripts", scripts)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    for name in ("az", "psql"):
        stub = stub_bin / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    calls = tmp_path / "calls"
    calls.write_text("")
    script = tmp_path / f"{step_id}.sh"
    script.write_text(run_block(step_id))

    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=workspace,
        env={
            "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(workspace),
            "GITHUB_ENV": str(github_env),
            "RUNNER_TEMP": str(runner_temp),
            "STUB_LOG": str(calls),
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return Run(
        proc.returncode,
        proc.stdout,
        proc.stderr,
        parse_github_env(github_env.read_text()),
        calls.read_text().splitlines(),
        workspace,
    )


# ---------------------------------------------------------------- Build zip package


def make_app(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "__init__.py").write_text("")
    (workspace / "app" / "main.py").write_text("print('hi')\n")
    (workspace / "requirements.txt").write_text("fastapi\n")
    return workspace


def assert_no_workflow_command(output: str, allowed: tuple[str, ...] = ()) -> None:
    for line in output.splitlines():
        if line.startswith("::"):
            assert line.startswith(allowed), f"build output reads as a command: {line}"


def test_build_packages_app_and_requirements(tmp_path: Path) -> None:
    workspace = make_app(tmp_path)

    run = run_step(tmp_path, "build")

    assert run.returncode == 0, run.stdout + run.stderr
    names = set(zipfile.ZipFile(workspace / "deploy.zip").namelist())
    assert {"app/main.py", "app/__init__.py", "requirements.txt"} <= names
    assert not any(name.startswith(("deploy_package", ".github")) for name in names)
    assert "| app/main.py" in run.stdout.splitlines()
    assert "BUILD_ERROR_OUTPUT" not in run.env


def test_build_refuses_a_symlink_in_app(tmp_path: Path) -> None:
    workspace = make_app(tmp_path)
    (workspace / "app" / "secrets.py").symlink_to("/etc/hosts")
    # find prints names raw: this one would start a `::warning::` line if unquoted.
    (workspace / "app" / "x\n::warning::pwned").symlink_to("/etc/hosts")

    run = run_step(tmp_path, "build")

    assert run.returncode == 1
    assert "| ::warning::pwned" in run.stdout.splitlines()
    assert_no_workflow_command(run.stdout, allowed=("::error title=build::",))
    assert not (workspace / "deploy.zip").exists()
    assert run.env["BUILD_ERROR_OUTPUT"].startswith("refusing to package symlinks")
    assert "app/secrets.py" in run.env["BUILD_ERROR_OUTPUT"]
    assert "::error title=build::" in run.stdout


def test_build_refuses_a_symlinked_requirements_file(tmp_path: Path) -> None:
    workspace = make_app(tmp_path)
    (workspace / "requirements.txt").unlink()
    (workspace / "requirements.txt").symlink_to("/etc/hosts")

    run = run_step(tmp_path, "build")

    assert run.returncode == 1
    assert "requirements.txt" in run.env["BUILD_ERROR_OUTPUT"]


def test_build_output_never_starts_a_workflow_command(tmp_path: Path) -> None:
    workspace = make_app(tmp_path)
    # A newline in a file name puts `::warning::…` at the start of a listing line.
    (workspace / "app" / "x\n::warning::pwned.py").write_text("")

    run = run_step(tmp_path, "build")

    assert run.returncode == 0, run.stdout + run.stderr
    # zipinfo may escape the newline (`^J`) or not; either way the line is quoted.
    assert any("pwned.py" in line for line in run.stdout.splitlines())
    assert_no_workflow_command(run.stdout + run.stderr)


def test_build_failure_reports_the_error_output(tmp_path: Path) -> None:
    workspace = make_app(tmp_path)
    (workspace / "requirements.txt").unlink()

    run = run_step(tmp_path, "build")

    assert run.returncode != 0
    assert "requirements.txt" in run.env["BUILD_ERROR_OUTPUT"]
    assert_no_workflow_command(run.stdout, allowed=("::error title=build::",))
