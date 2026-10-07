"""Offline tests for the Datadog trigger definitions and datadog/apply.sh (deployment §6.3).

The JSON under datadog/ is checked against the contract the bridge depends on: the
webhook body the Event Grid input mapping reads, alert-only notify, and the
`deploy_status:failed` marker that splits deploy_failure from runtime_error. apply.sh
runs with stub `curl`, `az`, `gh` and `terraform` first on PATH, so the create-or-update
flow and the secret handling are exercised without a network. Needs bash and jq.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATADOG = ROOT / "datadog"
APPLY = DATADOG / "apply.sh"
WEBHOOK = DATADOG / "webhook.json"
DEPLOY_FAILURE = DATADOG / "monitors" / "deploy-failure.json"
SYNTHETICS = sorted((DATADOG / "synthetics").glob("*.json"))
MONITORS = sorted((DATADOG / "monitors").glob("*.json"))

# Built, not literals, and low-entropy: secret scanners must never see a key-shaped value.
API_KEY = "fake" * 8
APP_KEY = "appk" * 10
EG_KEY = "egk" * 11 + "="
ENDPOINT = "https://evgt-sentinel-dev.canadacentral-1.eventgrid.azure.net/api/events"
APP_URL = "https://app-sentinel-dev-b136.azurewebsites.net"
MENTION = "@webhook-sentinel-event-grid"

# What Datadog substitutes into the webhook body: the bridge's two inputs, one sample each.
SAMPLES = {
    "$EVENT_TITLE": "[Triggered] sentinel-deploy-failure",
    "$TAGS": "deploy_status:failed,env:dev,service:sentinel-watchtower",
    "$ALERT_TRANSITION": "Triggered",
    "$LINK": "https://us5.datadoghq.com/event/jump_to?event_id=1",
    "$ALERT_ID": "1234",
    "$DATE": "1759651200000",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text())


IS_ALERT = re.compile(r"\{\{#is_alert\}\}(.*?)\{\{/is_alert\}\}", re.DOTALL)


def notify_targets(message: str) -> tuple[str, str]:
    """Split a message into (inside every is_alert block, everything else)."""
    return "".join(IS_ALERT.findall(message)), IS_ALERT.sub("", message)


# ── Definitions ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", sorted(DATADOG.rglob("*.json")), ids=lambda p: p.name)
def test_every_definition_is_one_json_object(path):
    assert isinstance(load(path), dict)


def test_webhook_body_renders_to_the_flat_contract():
    webhook = load(WEBHOOK)
    assert webhook["name"] == "sentinel-event-grid"
    assert set(webhook["custom_headers"]) == {"aeg-sas-key"}
    # apply.sh sends the payload as Datadog stores it: a string with $VARS in it.
    rendered = json.dumps(webhook["payload"])
    for var, sample in SAMPLES.items():
        rendered = rendered.replace(var, sample)
    body = json.loads(rendered)
    # Event Grid's publish API takes an array of events, CustomEventSchema topics included:
    # a bare object is rejected. One alert is one event.
    assert body == [
        {
            "title": SAMPLES["$EVENT_TITLE"],
            "tags": SAMPLES["$TAGS"],
            "alert_transition": "Triggered",
            "link": SAMPLES["$LINK"],
            "alert_id": "1234",  # the Event Grid input mapping's `subject`
            "date": SAMPLES["$DATE"],
        }
    ]
    assert "$" not in rendered


def bridge_signal(body: dict) -> str:
    """The bridge's classification (Sentinel-infra modules/functions/src/bridge)."""
    blob = body["tags"] + " " + body["title"]
    return "deploy_failure" if "deploy_status:failed" in blob else "runtime_error"


@pytest.mark.parametrize("path", MONITORS + SYNTHETICS, ids=lambda p: p.name)
def test_webhook_is_mentioned_only_on_alert(path):
    inside, outside = notify_targets(load(path)["message"])
    assert MENTION in inside
    assert "@" not in outside, "a recovery or no-data notification would dispatch"


@pytest.mark.parametrize("path", MONITORS + SYNTHETICS, ids=lambda p: p.name)
def test_every_trigger_is_tagged_for_the_service(path):
    tags = load(path)["tags"]
    assert {"service:sentinel-watchtower", "env:dev"} <= set(tags)


def test_deploy_failure_monitor_matches_failed_deploy_events():
    monitor = load(DEPLOY_FAILURE)
    assert monitor["name"] == "sentinel-deploy-failure"
    assert monitor["type"] == "event-v2 alert"
    assert monitor["query"] == (
        'events("deploy_status:failed service:sentinel-watchtower")'
        '.rollup("count").last("5m") > 0'
    )
    assert monitor["options"]["thresholds"] == {"critical": 0}
    assert monitor["options"]["renotify_interval"] == 0
    # $TAGS carries the monitor's own tags: this is what the bridge keys on.
    body = {
        "tags": ",".join(monitor["tags"]),
        "title": f"[Triggered] {monitor['name']}",
    }
    assert bridge_signal(body) == "deploy_failure"


@pytest.mark.parametrize("path", SYNTHETICS, ids=lambda p: p.name)
def test_runtime_tests_ping_the_app_every_five_minutes(path):
    test = load(path)
    route = test["config"]["request"]["url"].removeprefix("__APP_URL__")
    assert route in ("/", "/health")
    assert test["name"] == f"sentinel-runtime-health GET {route}"
    assert (test["type"], test["subtype"], test["status"]) == ("api", "http", "live")
    assert test["config"]["request"]["method"] == "GET"
    assert len(test["locations"]) == 1
    options = test["options"]
    # F1 sleeps after ~20 idle min; a check every 5 min cold-starts it constantly and
    # burned the 60 CPU-min/day quota live (2026-10-07). 30 min keeps it under.
    assert options["tick_every"] == 1800
    assert options["min_failure_duration"] == 0
    assert options["retry"] == {"count": 1, "interval": 60000}
    assert options["min_location_failed"] == 1
    assert options["monitor_options"]["renotify_interval"] == 0
    body = {"tags": ",".join(test["tags"]), "title": f"[Triggered] {test['name']}"}
    assert bridge_signal(body) == "runtime_error"
    assert "deploy_status" not in json.dumps(test)


def test_runtime_health_covers_both_routes():
    routes = {load(p)["config"]["request"]["url"] for p in SYNTHETICS}
    assert routes == {"__APP_URL__/", "__APP_URL__/health"}


# ── apply.sh ──────────────────────────────────────────────────────────────────

STUB_CURL = r"""#!/usr/bin/env bash
dir="$STUB_LOG/call-$(find "$STUB_LOG" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')"
mkdir -p "$dir"
printf '%s\n' "$@" >"$dir/argv"
out=/dev/null method=GET url=""
while (($#)); do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) shift ;;
    -X) method="$2"; shift ;;
    -H) if [[ "$2" == @* ]]; then cat "${2#@}" >>"$dir/headers"; else echo "$2" >>"$dir/headers"; fi; shift ;;
    --data-binary) cat "${2#@}" >"$dir/body"; shift ;;
    https://*) url="$1" ;;
  esac
  shift
done
echo "$method" >"$dir/method"
echo "$url" >"$dir/url"
status=200 resp='{}'
if [[ "$method" == GET ]]; then
  case "$url" in
    */webhooks/sentinel-event-grid)
      if [[ "$STUB_EXISTING" == 1 ]]; then resp='{"name":"sentinel-event-grid"}'; else status=404; fi ;;
    */api/v1/monitor\?name=*)
      if [[ "$STUB_EXISTING" == 1 ]]; then
        resp='[{"id":4242,"name":"sentinel-deploy-failure","type":"event-v2 alert"},
               {"id":9,"name":"sentinel-deploy-failure-old","type":"event-v2 alert"}]'
      else resp='[]'; fi ;;
    */api/v1/synthetics/tests)
      if [[ "$STUB_EXISTING" == 1 ]]; then
        resp='{"tests":[{"public_id":"abc-def-001","name":"sentinel-runtime-health GET /"},
                        {"public_id":"abc-def-002","name":"sentinel-runtime-health GET /health"},
                        {"public_id":"zzz-zzz-999","name":"someone else"}]}'
      else resp='{"tests":[]}'; fi ;;
  esac
fi
echo "$resp" >"$out"
printf '%s' "$status"
"""

STUB_AZ = r"""#!/usr/bin/env bash
echo "az $*" >>"$STUB_LOG/../az.log"
case "$*" in
  "account show"*) echo '{}' ;;
  "eventgrid topic show"*) echo "$STUB_ENDPOINT" ;;
  "eventgrid topic key list"*) printf '%s\r\n' "$STUB_EG_KEY" ;;
  *) exit 1 ;;
esac
"""

STUB_GH = r"""#!/usr/bin/env bash
echo "gh $*" >>"$STUB_LOG/../gh.log"
[[ "$*" == "variable get DEPLOYED_APP_URL --env sentinel-dev --repo Keshav0375/Sentinel-deployment" ]] || exit 1
echo "$STUB_APP_URL/"
"""

# Without STUB_TF the infra checkout has no outputs, as on a destroyed estate.
STUB_TERRAFORM = r"""#!/usr/bin/env bash
echo "TF_WORKSPACE=$TF_WORKSPACE terraform $*" >>"$STUB_LOG/../terraform.log"
[[ "$STUB_TF" == 1 ]] || exit 1
case "${*: -1}" in
  event_grid_topic_name) echo "evgt-from-tf" ;;
  event_grid_endpoint) echo "https://evgt-from-tf.canadacentral-1.eventgrid.azure.net/api/events" ;;
  deployment_resource_group) echo "rg-from-tf" ;;
  *) exit 1 ;;
esac
"""


@dataclass
class Call:
    method: str
    url: str
    headers: str
    argv: str
    body: dict | list | None


@dataclass
class Run:
    returncode: int
    output: str
    calls: list[Call]
    az: str
    terraform: str

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET"]


def run_apply(
    tmp_path: Path,
    *args: str,
    env_lines: tuple[str, ...] | None = None,
    curl: str = STUB_CURL,
    apply: Path = APPLY,
    **stub_env: str,
) -> Run:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for name, body in (
        ("curl", curl),
        ("az", STUB_AZ),
        ("gh", STUB_GH),
        ("terraform", STUB_TERRAFORM),
    ):
        stub = stub_bin / name
        stub.write_text(body)
        stub.chmod(0o755)
    log = tmp_path / "calls"
    log.mkdir()
    env_file = tmp_path / "infra.env"
    if env_lines is None:
        env_lines = (
            f"DD_API_KEY={API_KEY}",
            f'DD_APP_KEY="{APP_KEY}"',
            "DD_SITE=us5.datadoghq.com",
        )
    env_file.write_text("\n".join(env_lines) + "\n")

    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "STUB_LOG": str(log),
        "STUB_ENDPOINT": ENDPOINT,
        "STUB_EG_KEY": EG_KEY,
        "STUB_APP_URL": APP_URL,
        "STUB_EXISTING": "0",
        "STUB_TF": "0",
    }
    env.update(stub_env)
    proc = subprocess.run(
        [
            "bash",
            str(apply),
            "--env-file",
            str(env_file),
            "--infra-dir",
            str(tmp_path),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    def read(path: Path) -> str:
        return path.read_text() if path.exists() else ""

    calls = []
    for call_dir in sorted(log.iterdir(), key=lambda p: int(p.name.split("-")[1])):
        body = read(call_dir / "body")
        calls.append(
            Call(
                method=read(call_dir / "method").strip(),
                url=read(call_dir / "url").strip(),
                headers=read(call_dir / "headers"),
                argv=read(call_dir / "argv"),
                body=json.loads(body) if body else None,
            )
        )
    return Run(
        proc.returncode,
        proc.stdout + proc.stderr,
        calls,
        read(tmp_path / "az.log"),
        read(tmp_path / "terraform.log"),
    )


def assert_no_secret_leaks(run: Run) -> None:
    for secret in (API_KEY, APP_KEY, EG_KEY):
        assert secret not in run.output
        assert secret not in run.az
        for call in run.calls:
            assert secret not in call.argv


def test_dry_run_looks_up_and_writes_nothing(tmp_path):
    run = run_apply(tmp_path, "--dry-run")
    assert run.returncode == 0, run.output
    assert run.writes() == []
    assert {c.url for c in run.calls} == {
        "https://api.us5.datadoghq.com/api/v1/integration/webhooks/configuration/webhooks/sentinel-event-grid",
        "https://api.us5.datadoghq.com/api/v1/monitor?name=sentinel-deploy-failure",
        "https://api.us5.datadoghq.com/api/v1/synthetics/tests",
    }
    assert (
        "would POST /api/v1/integration/webhooks/configuration/webhooks" in run.output
    )
    assert "would POST /api/v1/monitor" in run.output
    assert run.output.count("would POST /api/v1/synthetics/tests/api") == 2
    # Secrets show as lengths only.
    assert f"DD_API_KEY {len(API_KEY)} chars" in run.output
    assert f"DD_APP_KEY {len(APP_KEY)} chars" in run.output
    assert f"aeg-sas-key {len(EG_KEY)} chars" in run.output
    assert_no_secret_leaks(run)


def test_keys_travel_only_in_the_header_file(tmp_path):
    run = run_apply(tmp_path, "--dry-run")
    assert run.returncode == 0, run.output
    for call in run.calls:
        assert f"DD-API-KEY: {API_KEY}" in call.headers
        assert f"DD-APPLICATION-KEY: {APP_KEY}" in call.headers
        assert "-H\n@" in call.argv


def test_first_apply_creates_everything(tmp_path):
    run = run_apply(tmp_path)
    assert run.returncode == 0, run.output
    writes = {
        (c.method, c.url.removeprefix("https://api.us5.datadoghq.com")): c
        for c in run.writes()
    }
    assert set(writes) == {
        ("POST", "/api/v1/integration/webhooks/configuration/webhooks"),
        ("POST", "/api/v1/monitor"),
        ("POST", "/api/v1/synthetics/tests/api"),
    }
    assert len(run.writes()) == 4  # webhook, monitor, two synthetic tests
    # The webhook is the first write: monitors mention it.
    assert run.writes()[0].url.endswith("/webhooks")

    webhook = writes[
        ("POST", "/api/v1/integration/webhooks/configuration/webhooks")
    ].body
    assert webhook["url"] == ENDPOINT
    assert json.loads(webhook["custom_headers"]) == {"aeg-sas-key": EG_KEY}
    payload = json.loads(webhook["payload"])
    assert payload == load(WEBHOOK)["payload"]
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["alert_id"] == "$ALERT_ID"
    assert webhook["encode_as"] == "json"

    assert writes[("POST", "/api/v1/monitor")].body == load(DEPLOY_FAILURE)
    urls = sorted(
        c.body["config"]["request"]["url"]
        for c in run.writes()
        if c.url.endswith("/synthetics/tests/api")
    )
    assert urls == [f"{APP_URL}/", f"{APP_URL}/health"]
    assert_no_secret_leaks(run)


def test_re_apply_updates_by_exact_name(tmp_path):
    run = run_apply(tmp_path, STUB_EXISTING="1")
    assert run.returncode == 0, run.output
    base = "https://api.us5.datadoghq.com"
    assert [(c.method, c.url) for c in run.writes()] == [
        (
            "PUT",
            f"{base}/api/v1/integration/webhooks/configuration/webhooks/sentinel-event-grid",
        ),
        ("PUT", f"{base}/api/v1/monitor/4242"),
        (
            "PUT",
            f"{base}/api/v1/synthetics/tests/api/abc-def-002",
        ),  # health.json sorts first
        ("PUT", f"{base}/api/v1/synthetics/tests/api/abc-def-001"),
    ]


def test_falls_back_to_naming_defaults_and_az(tmp_path):
    run = run_apply(tmp_path, "--dry-run")
    assert run.returncode == 0, run.output
    assert "TF_WORKSPACE=sentinel-dev terraform -chdir=" in run.terraform
    assert (
        "eventgrid topic show --name evgt-sentinel-dev --resource-group rg-sentinel-dev-cc"
        in run.az
    )
    assert "gh variable get DEPLOYED_APP_URL" in (tmp_path / "gh.log").read_text()


def test_prefers_terraform_outputs_and_flags(tmp_path):
    run = run_apply(tmp_path, "--dry-run", "--app-url", APP_URL, STUB_TF="1")
    assert run.returncode == 0, run.output
    assert "evgt-from-tf in rg-from-tf" in run.output
    assert (
        "https://evgt-from-tf.canadacentral-1.eventgrid.azure.net/api/events"
        in run.output
    )
    assert "eventgrid topic show" not in run.az  # the endpoint came from terraform
    assert (
        "eventgrid topic key list --name evgt-from-tf --resource-group rg-from-tf"
        in run.az
    )
    assert not (tmp_path / "gh.log").exists()  # --app-url wins over the GitHub variable


def test_missing_app_key_stops_before_any_call(tmp_path):
    run = run_apply(
        tmp_path,
        "--dry-run",
        env_lines=(f"DD_API_KEY={API_KEY}", "DD_SITE=us5.datadoghq.com"),
    )
    assert run.returncode != 0
    assert "DD_APP_KEY is missing" in run.output
    assert run.calls == []
    assert_no_secret_leaks(run)


def test_failed_write_stops_with_the_status(tmp_path):
    # A write that fails must not be reported as applied.
    stub = r"""#!/usr/bin/env bash
out=/dev/null; m=GET
while (($#)); do case "$1" in -o) out="$2"; shift ;; -X) m="$2"; shift ;; esac; shift; done
if [[ "$m" == GET ]]; then echo '{"tests":[]}' >"$out"; printf 404; else echo '{"errors":["bad"]}' >"$out"; printf 400; fi
"""
    run = run_apply(tmp_path, "--app-url", APP_URL, curl=stub)
    assert run.returncode != 0
    assert "returned HTTP 400" in run.output
    assert "applied." not in run.output


def test_an_object_payload_is_refused_before_any_write(tmp_path):
    """Event Grid rejects a bare object; apply.sh must refuse it, not ship it."""
    copy = tmp_path / "repo" / "datadog"
    shutil.copytree(DATADOG, copy)
    webhook = load(copy / "webhook.json")
    webhook["payload"] = webhook["payload"][0]
    (copy / "webhook.json").write_text(json.dumps(webhook))

    run = run_apply(tmp_path, apply=copy / "apply.sh")

    assert run.returncode == 1
    assert "payload must be a one-element array" in run.output
    assert run.writes() == []
