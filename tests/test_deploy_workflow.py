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


# ---------------------------------------------------------------- Deploy to App Service

DEPLOY_ENV = {
    "AZURE_RG": "rg-sentinel-dev-cc",
    "APP_NAME": "app-sentinel-dev-test",
    "APP_VERSION": "pr-47-a3f9c2e",
}


def test_app_version_is_set_only_after_the_zip_deploy(tmp_path: Path) -> None:
    run = run_step(tmp_path, "deploy", DEPLOY_ENV)

    assert run.returncode == 0, run.stderr
    target = "--resource-group rg-sentinel-dev-cc --name app-sentinel-dev-test"
    assert run.calls == [
        f"az webapp deploy {target} --src-path deploy.zip --type zip",
        f"az webapp config appsettings set {target}"
        + " --settings APP_VERSION=pr-47-a3f9c2e --output none",
    ]


def test_a_failed_zip_deploy_leaves_app_version_alone(tmp_path: Path) -> None:
    """The old code keeps its old APP_VERSION, so verify can see it still serving."""
    run = run_step(tmp_path, "deploy", {**DEPLOY_ENV, "STUB_FAIL_AZ": "webapp deploy"})

    assert run.returncode == 3
    assert len(run.calls) == 1
    assert run.calls[0].startswith("az webapp deploy ")


# ---------------------------------------------------------------- Record deployment

RECORD_ENV = {
    "CHECKOUT_OUTCOME": "success",
    "META_OUTCOME": "success",
    "BUILD_OUTCOME": "success",
    "LOGIN_OUTCOME": "success",
    "DEPLOY_OUTCOME": "success",
    "VERIFY_OUTCOME": "failure",
    "LOGIN_RECORD_OUTCOME": "success",
    "PG_HOST": "psql-sentinel-dev.postgres.database.azure.com",
    "PG_DATABASE": "sentinel",
    "PG_USER": "gha-app",
    "DD_SERVICE": "sentinel-watchtower",
    "DD_ENV": "dev",
    "RUN_URL": "https://github.com/Keshav0375/Sentinel-deployment/actions/runs/99",
    "GITHUB_RUN_ID": "99",
    "GITHUB_SHA": "a3f9c2e" + "0" * 33,
    "PR_NUMBER": "47",
    "SHORT_SHA": "a3f9c2e",
    "PR_AUTHOR": "Keshav0375",
    "PR_TITLE": "feat: add retry config",
    "FILES_CHANGED_JSON": '["app/main.py"]',
    "APP_VERSION": "pr-47-a3f9c2e",
    "DEPLOY_STARTED_AT": "1",
}


def psql_vars(run: Run) -> dict[str, str]:
    psql = next(call for call in run.calls if call.startswith("psql "))
    return dict(re.findall(r"-v (\w+)=(\S*)", psql))


def test_record_inserts_every_value_as_a_psql_variable(tmp_path: Path) -> None:
    run = run_step(tmp_path, "record", RECORD_ENV)

    assert run.returncode == 0, run.stderr
    assert run.calls[0] == (
        "az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv"
    )
    assert psql_vars(run) == {
        "ON_ERROR_STOP": "1",
        "service": "sentinel-watchtower",
        "pr": "47",
        "sha": "a3f9c2e",
        "author": "Keshav0375",
        "status": "failed",
        "run": "99",
        "files": '["app/main.py"]',
        "stage": "verify",
        "version": "pr-47-a3f9c2e",
    }
    assert "INSERT INTO deployments" in run.calls
    assert run.env["STATUS"] == "failed"


def test_a_failed_re_login_is_a_record_failure_not_a_status_change(
    tmp_path: Path,
) -> None:
    run = run_step(
        tmp_path,
        "record",
        {**RECORD_ENV, "VERIFY_OUTCOME": "success", "LOGIN_RECORD_OUTCOME": "failure"},
    )

    assert run.returncode == 1
    assert run.calls == [], "no token exchange, no psql after a failed re-login"
    assert run.env["RECORD_ERROR"].startswith("Azure re-login before the record failed")
    assert run.env["STATUS"] == "succeeded"
    assert run.env["FAILED_STAGE"] == "none"


def test_a_failed_insert_reports_the_psql_error(tmp_path: Path) -> None:
    run = run_step(tmp_path, "record", {**RECORD_ENV, "STUB_FAIL_PSQL": "host="})

    assert run.returncode == 3
    assert "psql: stubbed failure" in run.env["RECORD_ERROR"]
    assert run.env["RECORD_ERROR"].endswith(f"Run {RECORD_ENV['RUN_URL']}")


METADATA_KEYS = {
    "PR_NUMBER",
    "SHORT_SHA",
    "PR_AUTHOR",
    "PR_TITLE",
    "FILES_CHANGED_JSON",
    "APP_VERSION",
    "DEPLOY_STARTED_AT",
}


def test_a_failed_metadata_step_still_records_an_insertable_row(tmp_path: Path) -> None:
    env = {k: v for k, v in RECORD_ENV.items() if k not in METADATA_KEYS}
    run = run_step(
        tmp_path,
        "record",
        {**env, "META_OUTCOME": "failure", "BUILD_OUTCOME": "skipped"},
    )

    assert run.returncode == 0, run.stderr
    values = psql_vars(run)
    assert values["pr"] == "0"
    assert values["files"] == "[]"
    assert values["sha"] == "a3f9c2e"
    assert values["status"] == "failed"
    assert values["stage"] == "build"


# ---------------------------------------------------------------- Workflow shape


def test_dd_site_comes_from_the_environment_on_every_report() -> None:
    """Environment vars are invisible at workflow level: a top-level DD_SITE is always empty."""
    workflow_env = WORKFLOW_TEXT.split("\nenv:\n", 1)[1].split("\n\n", 1)[0]
    assert "DD_SITE" not in workflow_env
    reports = WORKFLOW_TEXT.count("uses: ./.github/actions/dd-report")
    assert reports == 5
    assert WORKFLOW_TEXT.count("dd-site: ${{ vars.DD_SITE }}") == reports
