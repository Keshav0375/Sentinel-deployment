"""Offline tests for the shell behind ci_app_deployment.yml (deployment §3.1).

`deploy-metadata.sh` (Stage 1) runs inside a throwaway git repo; `deploy-status.sh`
(the verdict for Stages 5/6) runs on step outcomes alone; `set-env.sh` is the
$GITHUB_ENV writer both use. Each test reads $GITHUB_ENV back with the runner's own
`NAME<<DELIMITER` rules, so an injected variable would show up as an extra key. Needs
bash, git and jq, as the runner does.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
SET_ENV = SCRIPTS / "set-env.sh"
METADATA = SCRIPTS / "deploy-metadata.sh"
STATUS = SCRIPTS / "deploy-status.sh"

RUN_URL = "https://github.com/Keshav0375/Sentinel-deployment/actions/runs/42"


def parse_github_env(text: str) -> dict[str, str]:
    """Parse a $GITHUB_ENV file the way the runner does; a repeated name keeps the last value."""
    env: dict[str, str] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line:
            continue
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            name, delimiter = line.split("<<", 1)
            value: list[str] = []
            while lines[i] != delimiter:
                value.append(lines[i])
                i += 1
            i += 1
            env[name] = "\n".join(value)
        else:
            name, value_ = line.split("=", 1)
            env[name] = value_
    return env


def run_script(
    script: Path,
    tmp_path: Path,
    env: dict[str, str],
    *args: str,
    cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    github_env = tmp_path / "github_env"
    github_env.touch()
    proc = subprocess.run(
        ["bash", str(script), *args],
        env={"PATH": os.environ["PATH"], "GITHUB_ENV": str(github_env), **env},
        cwd=cwd or tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc, parse_github_env(github_env.read_text())


# ---------------------------------------------------------------- set-env.sh


def test_set_env_keeps_an_injected_line_inside_the_value(tmp_path: Path) -> None:
    hostile = "feat: x\nINJECTED=1\nPATH=/evil"
    github_env = tmp_path / "github_env"
    github_env.touch()
    proc = subprocess.run(
        ["bash", str(SET_ENV), "PR_TITLE"],
        input=hostile,
        env={"PATH": os.environ["PATH"], "GITHUB_ENV": str(github_env)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert parse_github_env(github_env.read_text()) == {"PR_TITLE": hostile}


@pytest.mark.parametrize("name", ["", "lower", "A-B", "X=Y", "1ABC", "A B"])
def test_set_env_refuses_a_name_that_is_not_an_identifier(
    tmp_path: Path, name: str
) -> None:
    proc, env = run_script(SET_ENV, tmp_path, {}, name)
    assert proc.returncode == 2
    assert env == {}


# ---------------------------------------------------------------- deploy-metadata.sh


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "Keshav",
            "GIT_AUTHOR_EMAIL": "k@example.com",
            "GIT_COMMITTER_NAME": "Keshav",
            "GIT_COMMITTER_EMAIL": "k@example.com",
        },
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose HEAD changes `app/main.py`, adds `docs/a file.md`, deletes `old.txt`."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text("v1\n")
    (repo / "old.txt").write_text("gone soon\n")
    (repo / "keep.txt").write_text("untouched\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    (repo / "app" / "main.py").write_text("v2\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "a file.md").write_text("doc\n")
    (repo / "old.txt").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def run_metadata(repo: Path, tmp_path: Path, **overrides: str):
    env = {
        "GITHUB_SHA": git(repo, "rev-parse", "HEAD"),
        "COMMIT_MESSAGE": "feat: add retry config (#47)\n\n* squashed detail",
        "COMMIT_AUTHOR_USERNAME": "Keshav0375",
        "COMMIT_AUTHOR_NAME": "Keshav",
        **overrides,
    }
    return run_script(METADATA, tmp_path, env, cwd=repo)


def test_metadata_from_a_squash_merge(repo: Path, tmp_path: Path) -> None:
    before = int(time.time())
    proc, env = run_metadata(repo, tmp_path)

    assert proc.returncode == 0, proc.stderr
    sha7 = git(repo, "rev-parse", "HEAD")[:7]
    started = int(env.pop("DEPLOY_STARTED_AT"))
    assert before <= started <= int(time.time())
    files = json.loads(env.pop("FILES_CHANGED_JSON"))
    assert sorted(files) == ["app/main.py", "docs/a file.md", "old.txt"]
    assert env == {
        "PR_NUMBER": "47",
        "SHORT_SHA": sha7,
        "PR_TITLE": "feat: add retry config",
        "APP_VERSION": f"pr-47-{sha7}",
        "PR_AUTHOR": "Keshav0375",
    }


def test_metadata_from_a_merge_commit_takes_the_title_from_the_body(
    repo: Path, tmp_path: Path
) -> None:
    proc, env = run_metadata(
        repo,
        tmp_path,
        COMMIT_MESSAGE="Merge pull request #12 from Keshav0375/pass/01\n\n  \nfix: tidy logs\n\nmore",
    )

    assert proc.returncode == 0, proc.stderr
    assert env["PR_NUMBER"] == "12"
    assert env["PR_TITLE"] == "fix: tidy logs"
    assert env["APP_VERSION"].startswith("pr-12-")


@pytest.mark.parametrize(
    ("message", "pr_number", "title"),
    [
        ("chore: direct push to main", "0", "chore: direct push to main"),
        ("", "0", "(no commit message)"),
        # A PR reference in the body is not the merge's own number.
        ("fix: thing\n\nfollow-up to (#9)", "0", "fix: thing"),
        # Ten digits would overflow `::int`: treated as no PR at all.
        ("feat: big (#1234567890)", "0", "feat: big (#1234567890)"),
        ("feat: leading zeros (#007)", "7", "feat: leading zeros"),
        ("feat: crlf (#5)\r\n\r\nbody", "5", "feat: crlf"),
    ],
)
def test_pr_number_falls_back_to_zero_and_stays_an_int(
    repo: Path, tmp_path: Path, message: str, pr_number: str, title: str
) -> None:
    proc, env = run_metadata(repo, tmp_path, COMMIT_MESSAGE=message)

    assert proc.returncode == 0, proc.stderr
    assert env["PR_NUMBER"] == pr_number
    assert env["PR_TITLE"] == title
    assert env["APP_VERSION"] == f"pr-{pr_number}-{env['SHORT_SHA']}"


def test_hostile_commit_message_is_carried_literally(
    repo: Path, tmp_path: Path
) -> None:
    title = 'feat: $(touch pwned) `id` ::warning::x "q" ${HOME}'
    proc, env = run_metadata(
        repo,
        tmp_path,
        COMMIT_MESSAGE=f"{title} (#3)\nINJECTED=1\nghadelimiter_x",
        COMMIT_AUTHOR_USERNAME="",
        COMMIT_AUTHOR_NAME="Evil\nINJECTED=2",
    )

    assert proc.returncode == 0, proc.stderr
    assert env["PR_TITLE"] == title
    assert env["PR_AUTHOR"] == "Evil INJECTED=2"
    assert "INJECTED" not in env
    assert not (repo / "pwned").exists()
    assert title not in proc.stdout, "the title must never be echoed to the log"


def test_author_falls_back_to_name_then_unknown(repo: Path, tmp_path: Path) -> None:
    _, by_name = run_metadata(repo, tmp_path, COMMIT_AUTHOR_USERNAME="")
    assert by_name["PR_AUTHOR"] == "Keshav"
    (tmp_path / "github_env").unlink()
    _, nobody = run_metadata(
        repo, tmp_path, COMMIT_AUTHOR_USERNAME="", COMMIT_AUTHOR_NAME=""
    )
    assert nobody["PR_AUTHOR"] == "unknown"


def test_title_is_bounded(repo: Path, tmp_path: Path) -> None:
    _, env = run_metadata(repo, tmp_path, COMMIT_MESSAGE="x" * 500 + " (#1)")
    assert env["PR_TITLE"] == "x" * 200


def test_files_changed_is_capped_at_100(repo: Path, tmp_path: Path) -> None:
    for n in range(150):
        (repo / f"f{n:03}.txt").write_text(str(n))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "many")

    _, env = run_metadata(repo, tmp_path)

    files = json.loads(env["FILES_CHANGED_JSON"])
    assert len(files) == 100
    assert set(files) <= {f"f{n:03}.txt" for n in range(150)}


def test_root_commit_lists_its_own_files(tmp_path: Path) -> None:
    repo = tmp_path / "root"
    repo.mkdir()
    (repo / "only.txt").write_text("x")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")

    proc, env = run_metadata(repo, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(env["FILES_CHANGED_JSON"]) == ["only.txt"]


def test_unknown_commit_records_an_empty_file_list_with_a_warning(
    repo: Path, tmp_path: Path
) -> None:
    proc, env = run_metadata(repo, tmp_path, GITHUB_SHA="f" * 40)

    assert proc.returncode == 0, proc.stderr
    assert env["FILES_CHANGED_JSON"] == "[]"
    assert "::warning title=deploy-metadata::" in proc.stdout


def test_a_malformed_sha_fails_the_step(repo: Path, tmp_path: Path) -> None:
    proc, env = run_metadata(repo, tmp_path, GITHUB_SHA="not-a-sha")
    assert proc.returncode == 1
    assert env == {}


# ---------------------------------------------------------------- deploy-status.sh

OK = {
    "CHECKOUT_OUTCOME": "success",
    "META_OUTCOME": "success",
    "BUILD_OUTCOME": "success",
    "LOGIN_OUTCOME": "success",
    "DEPLOY_OUTCOME": "success",
    "VERIFY_OUTCOME": "success",
}


def run_status(tmp_path: Path, **overrides: str):
    env = {
        **OK,
        "PR_NUMBER": "47",
        "PR_TITLE": "feat: add retry config",
        "APP_VERSION": "pr-47-a3f9c2e",
        "DEPLOY_STARTED_AT": str(int(time.time()) - 95),
        "DD_SERVICE": "sentinel-watchtower",
        "DD_ENV": "dev",
        "RUN_URL": RUN_URL,
        **overrides,
    }
    return run_script(STATUS, tmp_path, env)


def test_a_clean_deploy_succeeds_with_the_section_3_1_log(tmp_path: Path) -> None:
    proc, env = run_status(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "succeeded none\n"
    assert env["STATUS"] == "succeeded"
    assert env["FAILED_STAGE"] == "none"
    assert env["DEPLOY_ALERT_TYPE"] == "info"
    payload = json.loads(env["DEPLOY_LOG_PAYLOAD"])
    duration = payload["deploy"].pop("duration_seconds")
    assert 95 <= duration <= 100
    assert payload == {
        "message": "deploy.completed",
        "ddsource": "github-actions",
        "ddtags": "version:pr-47-a3f9c2e,service:sentinel-watchtower,env:dev,"
        "deploy_status:succeeded,failed_stage:none",
        "hostname": "gha-runner",
        "service": "sentinel-watchtower",
        "deploy": {
            "pr_number": 47,
            "version": "pr-47-a3f9c2e",
            "pr_title": "feat: add retry config",
            "status": "succeeded",
            "failed_stage": "none",
            "stages": {
                "build": "succeeded",
                "deploy": "succeeded",
                "verify": "succeeded",
            },
        },
    }
    assert env["DEPLOY_SUMMARY_TEXT"].startswith(
        "Stages: build=succeeded, deploy=succeeded, verify=succeeded. Duration "
    )
    assert env["DEPLOY_SUMMARY_TEXT"].endswith(f"Run: {RUN_URL}")


@pytest.mark.parametrize(
    ("outcomes", "failed_stage", "stages"),
    [
        # The login still runs after a failed build (so the row can be written): it is not
        # the deploy stage, which never started.
        (
            {
                "BUILD_OUTCOME": "failure",
                "DEPLOY_OUTCOME": "skipped",
                "VERIFY_OUTCOME": "skipped",
            },
            "build",
            ("failed", "skipped", "skipped"),
        ),
        (
            {
                "BUILD_OUTCOME": "failure",
                "LOGIN_OUTCOME": "failure",
                "DEPLOY_OUTCOME": "skipped",
                "VERIFY_OUTCOME": "skipped",
            },
            "build",
            ("failed", "skipped", "skipped"),
        ),
        (
            {
                "META_OUTCOME": "failure",
                "BUILD_OUTCOME": "skipped",
                "DEPLOY_OUTCOME": "skipped",
                "VERIFY_OUTCOME": "skipped",
            },
            "build",
            ("failed", "skipped", "skipped"),
        ),
        (
            {
                "LOGIN_OUTCOME": "failure",
                "DEPLOY_OUTCOME": "skipped",
                "VERIFY_OUTCOME": "skipped",
            },
            "deploy",
            ("succeeded", "failed", "skipped"),
        ),
        (
            {"DEPLOY_OUTCOME": "failure", "VERIFY_OUTCOME": "skipped"},
            "deploy",
            ("succeeded", "failed", "skipped"),
        ),
        (
            {"VERIFY_OUTCOME": "failure"},
            "verify",
            ("succeeded", "succeeded", "failed"),
        ),
        # A cancelled run: a cancelled step did not succeed, and nothing after it ran.
        (
            {"DEPLOY_OUTCOME": "cancelled", "VERIFY_OUTCOME": "skipped"},
            "deploy",
            ("succeeded", "failed", "skipped"),
        ),
        (
            {"VERIFY_OUTCOME": "skipped"},
            "verify",
            ("succeeded", "succeeded", "skipped"),
        ),
        (
            {key: "" for key in OK},
            "build",
            ("skipped", "skipped", "skipped"),
        ),
    ],
)
def test_a_failed_stage_fails_the_deploy(
    tmp_path: Path,
    outcomes: dict[str, str],
    failed_stage: str,
    stages: tuple[str, str, str],
) -> None:
    proc, env = run_status(tmp_path, **outcomes)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"failed {failed_stage}\n"
    assert env["STATUS"] == "failed"
    assert env["FAILED_STAGE"] == failed_stage
    assert env["DEPLOY_ALERT_TYPE"] == "error"
    deploy = json.loads(env["DEPLOY_LOG_PAYLOAD"])["deploy"]
    assert deploy["status"] == "failed"
    assert deploy["failed_stage"] == failed_stage
    assert tuple(deploy["stages"][s] for s in ("build", "deploy", "verify")) == stages


def test_status_payload_survives_missing_metadata(tmp_path: Path) -> None:
    """Metadata failed: no PR number, version or start time — the summary still goes out."""
    proc, env = run_status(
        tmp_path,
        META_OUTCOME="failure",
        BUILD_OUTCOME="skipped",
        PR_NUMBER="",
        APP_VERSION="",
        DEPLOY_STARTED_AT="",
        PR_TITLE="",
    )

    assert proc.returncode == 0, proc.stderr
    deploy = json.loads(env["DEPLOY_LOG_PAYLOAD"])["deploy"]
    assert deploy["pr_number"] == 0
    assert deploy["version"] == "unknown"
    assert deploy["duration_seconds"] == 0


def test_hostile_title_reaches_the_log_payload_literally(tmp_path: Path) -> None:
    title = 'fix: "x", "status": "succeeded" $(touch pwned)\nINJECTED=1'
    proc, env = run_status(tmp_path, PR_TITLE=title, VERIFY_OUTCOME="failure")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(env["DEPLOY_LOG_PAYLOAD"])
    assert payload["deploy"]["pr_title"] == title
    assert payload["deploy"]["status"] == "failed"
    assert "INJECTED" not in env
    assert not (tmp_path / "pwned").exists()
