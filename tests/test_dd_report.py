"""Offline tests for the dd-report composite action's script (deployment §3.2).

`dd-report.sh` runs with a stub `curl` first on PATH. The stub records each call's URL,
headers (resolving `-H @file`), argv and body, and answers like Datadog would, so the
tests assert the exact JSON sent, the site-built URLs, the fail-soft behaviour and that
the API key never leaks — without touching the network. Needs bash and jq, as the
action does on the runner.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "actions"
    / "dd-report"
    / "dd-report.sh"
)
# Built, not a literal, and low-entropy: secret scanners must never see a key-shaped value here.
API_KEY = "fake" * 8
SITE = "us5.datadoghq.com"
EVENT_ID = "7613449929768408044"  # > 2**53: must come through exactly

STUB_CURL = r"""#!/usr/bin/env bash
dir="$STUB_LOG/call-$(find "$STUB_LOG" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')"
mkdir -p "$dir"
printf '%s\n' "$@" >"$dir/argv"
out=/dev/null
while (($#)); do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) shift ;;
    -H) if [[ "$2" == @* ]]; then cat "${2#@}" >>"$dir/headers"; else echo "$2" >>"$dir/headers"; fi; shift ;;
    --data-binary) cat "${2#@}" >"$dir/body"; shift ;;
    https://*) echo "$1" >"$dir/url" ;;
  esac
  shift
done
if [[ "$(cat "$dir/url")" == */api/v1/events ]]; then
  echo '{"status":"ok","event":{"id":7613449929768408044,"id_str":"7613449929768408044"}}' >"$out"
else
  echo '{}' >"$out"
fi
printf '%s' "${STUB_STATUS:-202}"
exit "${STUB_EXIT:-0}"
"""


@dataclass
class Call:
    url: str
    headers: list[str]
    argv: list[str]
    body: object


@dataclass
class Run:
    returncode: int
    output: str
    calls: list[Call]
    github_output: str


def run_action(tmp_path: Path, **overrides: str) -> Run:
    """Run dd-report.sh as the action would, with `overrides` replacing the default env."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    curl = stub_bin / "curl"
    curl.write_text(STUB_CURL)
    curl.chmod(0o755)
    log = tmp_path / "calls"
    log.mkdir()
    github_output = tmp_path / "github_output"
    github_output.touch()

    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "GITHUB_OUTPUT": str(github_output),
        "RUNNER_TEMP": str(tmp_path),
        "DD_TITLE": "Deploy v1.2.3",
        "DD_TEXT": "deployed",
        "DD_TAGS": "service:sentinel-watchtower,env:dev",
        "DD_ALERT_TYPE": "info",
        "DD_LOG_PAYLOAD": "",
        "DD_API_KEY": API_KEY,
        "DD_SITE": SITE,
    }
    env.update(overrides)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    calls = []
    for call_dir in sorted(log.iterdir(), key=lambda p: int(p.name.split("-")[1])):
        calls.append(
            Call(
                url=(call_dir / "url").read_text().strip(),
                headers=(call_dir / "headers").read_text().splitlines(),
                argv=(call_dir / "argv").read_text().splitlines(),
                body=json.loads((call_dir / "body").read_text()),
            )
        )
    return Run(
        proc.returncode, proc.stdout + proc.stderr, calls, github_output.read_text()
    )


def assert_key_not_leaked(run: Run) -> None:
    """The key may appear only in the runner's own ::add-mask:: line — never in a warning."""
    leaked = [
        line
        for line in run.output.splitlines()
        if API_KEY in line and line != f"::add-mask::{API_KEY}"
    ]
    assert leaked == []
    for call in run.calls:
        assert all(API_KEY not in arg for arg in call.argv), (
            "key must not be in curl's argv"
        )


def test_event_body_url_and_headers(tmp_path: Path) -> None:
    run = run_action(tmp_path)

    assert run.returncode == 0
    assert f"::add-mask::{API_KEY}" in run.output.splitlines()
    assert len(run.calls) == 1
    event = run.calls[0]
    assert event.url == f"https://api.{SITE}/api/v1/events"
    assert f"DD-API-KEY: {API_KEY}" in event.headers
    assert "Content-Type: application/json" in event.headers
    assert event.body == {
        "title": "Deploy v1.2.3",
        "text": "deployed",
        "tags": ["service:sentinel-watchtower", "env:dev"],
        "alert_type": "info",
        "source_type_name": "github",
    }
    assert run.github_output == f"event-id={EVENT_ID}\n"
    assert "::warning" not in run.output
    assert_key_not_leaked(run)


def test_hostile_title_and_text_are_sent_literally(tmp_path: Path) -> None:
    title = 'fix: "quoted" $(touch pwned) `id` \\ ${DD_API_KEY}'
    text = 'line one\nline two", "alert_type": "success'
    run = run_action(
        tmp_path,
        DD_TITLE=title,
        DD_TEXT=text,
        DD_TAGS=" a:1 ,, b:2 ,",
        DD_ALERT_TYPE="error",
    )

    body = run.calls[0].body
    assert body["title"] == title
    assert body["text"] == text
    assert body["tags"] == ["a:1", "b:2"]
    assert body["alert_type"] == "error"
    assert not (Path.cwd() / "pwned").exists()
    assert_key_not_leaked(run)


def test_log_payload_is_sent_as_one_element_array(tmp_path: Path) -> None:
    payload = {
        "message": "deploy.finished",
        "ddtags": "env:dev",
        "deploy": {"status": "succeeded"},
    }
    run = run_action(tmp_path, DD_LOG_PAYLOAD=json.dumps(payload))

    assert run.returncode == 0
    assert [call.url for call in run.calls] == [
        f"https://api.{SITE}/api/v1/events",
        f"https://http-intake.logs.{SITE}/api/v2/logs",
    ]
    log = run.calls[1]
    assert log.body == [payload]
    assert f"DD-API-KEY: {API_KEY}" in log.headers
    assert_key_not_leaked(run)


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", '"a string"', "{} {}"])
def test_invalid_log_payload_warns_and_skips_the_log(
    tmp_path: Path, payload: str
) -> None:
    run = run_action(tmp_path, DD_LOG_PAYLOAD=payload)

    assert run.returncode == 0
    assert [call.url for call in run.calls] == [f"https://api.{SITE}/api/v1/events"]
    assert "::warning title=dd-report::log-payload is not a JSON object" in run.output


def test_http_failure_warns_with_status_and_never_fails(tmp_path: Path) -> None:
    run = run_action(
        tmp_path, STUB_STATUS="403", STUB_EXIT="22", DD_LOG_PAYLOAD='{"message": "x"}'
    )

    assert run.returncode == 0
    assert "event POST failed (HTTP 403, curl exit 22)" in run.output
    assert "log POST failed (HTTP 403, curl exit 22)" in run.output
    assert run.github_output == ""
    assert_key_not_leaked(run)


@pytest.mark.parametrize(
    ("overrides", "warning"),
    [
        ({"DD_SITE": ""}, "is not a Datadog site hostname"),
        ({"DD_SITE": "evil.example/#"}, "is not a Datadog site hostname"),
        ({"DD_API_KEY": ""}, "dd-api-key is empty"),
    ],
)
def test_bad_site_or_key_reports_nothing(
    tmp_path: Path, overrides: dict[str, str], warning: str
) -> None:
    run = run_action(tmp_path, **overrides)

    assert run.returncode == 0
    assert run.calls == []
    assert warning in run.output


def test_unknown_alert_type_skips_the_event_but_not_the_log(tmp_path: Path) -> None:
    run = run_action(
        tmp_path, DD_ALERT_TYPE="success", DD_LOG_PAYLOAD='{"message": "x"}'
    )

    assert run.returncode == 0
    assert [call.url for call in run.calls] == [
        f"https://http-intake.logs.{SITE}/api/v2/logs"
    ]
    assert "alert-type 'success' is not info|error" in run.output


@pytest.mark.parametrize(("curl_exit", "attempts"), [("6", 3), ("7", 3)])
def test_a_request_that_never_left_is_retried(
    tmp_path: Path, curl_exit: str, attempts: int
) -> None:
    run = run_action(tmp_path, STUB_STATUS="000", STUB_EXIT=curl_exit)

    assert run.returncode == 0
    assert len(run.calls) == attempts
    assert all(call.url == f"https://api.{SITE}/api/v1/events" for call in run.calls)
    assert f"event POST failed (HTTP 000, curl exit {curl_exit})" in run.output
    assert all("--retry" not in call.argv for call in run.calls)


@pytest.mark.parametrize(
    ("status", "curl_exit"),
    [("000", "28"), ("500", "22"), ("503", "22"), ("429", "22")],
)
def test_a_timeout_or_http_error_is_never_retried(
    tmp_path: Path, status: str, curl_exit: str
) -> None:
    """Datadog may already hold the event: a second POST would duplicate it."""
    run = run_action(
        tmp_path,
        STUB_STATUS=status,
        STUB_EXIT=curl_exit,
        DD_LOG_PAYLOAD='{"message": "x"}',
    )

    assert run.returncode == 0
    assert [call.url for call in run.calls] == [
        f"https://api.{SITE}/api/v1/events",
        f"https://http-intake.logs.{SITE}/api/v2/logs",
    ]
    assert f"event POST failed (HTTP {status}, curl exit {curl_exit})" in run.output
