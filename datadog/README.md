# Datadog triggers

These Datadog objects turn a broken deploy or a broken live app into a Sentinel
incident (deployment §6.3):

```
monitor/test alerts ──@webhook-sentinel-event-grid──▶ Event Grid topic ──▶ bridge Function ──▶ repository_dispatch
```

They are versioned JSON and are applied by `apply.sh`. The owner runs it locally,
never CI, because it needs the Datadog application key.

| File | Object | Fires on | Case | Evidence class | Bridge `signal_type` |
|------|--------|----------|------|----------------|----------------------|
| `monitors/deploy-failure.json` | monitor `sentinel-deploy-failure` (event-v2 alert) | any event matching `deploy_status:failed service:sentinel-watchtower` in the last 5 min | ii: the deploy failed and the old version is still live | **C**: the deploy-failure event | `deploy_failure` |
| `synthetics/runtime-health-root.json` | API test `sentinel-runtime-health GET /` | `GET <app>/` failing any of: status 200, `content-type` contains `application/json`, `$.message == "ok"` (attempt and its retry a minute later) | iii: verify passed, but the live app is broken | **B**: runtime | `runtime_error` |
| `synthetics/runtime-health-health.json` | API test `sentinel-runtime-health GET /health` | `GET <app>/health` not returning 200 (attempt and its retry a minute later) | iii | **B**: runtime | `runtime_error` |
| `webhook.json` | Webhooks integration `sentinel-event-grid` | the target of every alert above | | | |

`ci_app_deployment.yml` sends a `deploy_status:failed` event for the failed stage
and another in the final summary. Both land in the same 5-minute window, so the
monitor fires once. A record-stage failure (`stage:record`) carries no
`deploy_status` tag and never fires it.

## How the bridge tells B from C

The bridge reads the posted body as the event's `data`. If
`deploy_status:failed` appears in `data.tags` or `data.title`, it classifies the
alert `deploy_failure`, and anything else as `runtime_error`. `$TAGS` carries the
alerting object's own tags, so:

- `sentinel-deploy-failure` is tagged `deploy_status:failed`. Its alerts always
  classify `deploy_failure`.
- The runtime tests must never carry `deploy_status` anywhere: not in a tag, the
  name or the message. A test asserts this.

Every object is also tagged `service:sentinel-watchtower` and `env:dev`.

## Alert-only notify

Each message mentions the webhook only inside `{{#is_alert}}…{{/is_alert}}`.
Datadog would otherwise notify on recovery too. The bridge would classify that
recovery as `runtime_error` and start an incident for a problem that has already
cleared. Never add a mention outside the block. `tests/test_datadog.py` fails if
one appears.

## Webhook body contract

The webhook posts a one-element JSON array holding one flat object, with the
header `aeg-sas-key: <topic key>`:

```json
[{"title":"$EVENT_TITLE","tags":"$TAGS","alert_transition":"$ALERT_TRANSITION",
  "link":"$LINK","alert_id":"$ALERT_ID","date":"$DATE"}]
```

Event Grid's publish API only accepts an array of events, and that holds for
CustomEventSchema topics too. It rejects a bare object. `apply.sh` refuses a
payload that is not a one-element array.

Datadog has no ISO-8601 date variable, so it cannot build an Event Grid schema
event itself. The topic is created with a **CustomEventSchema** input mapping in
Sentinel-infra:

- `subject` comes from `alert_id`.
- `eventType` defaults to `datadog.monitor` and `dataVersion` to `1`.
- Event Grid stamps `id` and `eventTime` itself.
- The flat object (the array's one element) becomes the event's `data`.

`$DATE` is epoch milliseconds and is kept only for reference. Titles and tags are
substituted into JSON strings unescaped, so keep monitor and test names free of
`"` and `\`.

## Hygiene

- **Renotify off**: `renotify_interval: 0` on the monitor and on both tests'
  monitors. One incident means one dispatch.
- **Recovery before re-alert**:
  - The event monitor resolves only after a full 5-minute window with no failed
    deploy event. A second failure inside that window does not re-alert.
  - A test resolves only on a passing run, and must fail again to re-alert.
- **Every 30 minutes, not 5** (`tick_every: 1800`) from one managed location
  (`aws:ca-central-1`, next to the app). The F1 plan sleeps after ~20 idle minutes and
  has a **60 CPU-minute daily quota**; a 5-minute check cold-started gunicorn all day and
  put the app into `QuotaExceeded` (live, 2026-10-07) — the monitor broke the thing it
  watches. A failed run is retried once a minute later (`retry`), and alerts if the retry
  also fails (`min_failure_duration: 0`), so one slow cold start does not alert. A
  condition-B demo alerts within ~30 min of going live — trigger the test manually
  ("Run test now") to demo faster.
  - Each request times out at 60 s.

## Apply

Prerequisites:

- `DD_API_KEY`, `DD_APP_KEY` and `DD_SITE=us5.datadoghq.com` in
  `../Sentinel-infra/.env`. The application key is required, because the API key
  alone cannot read or write monitors.
- `az login` to the subscription and an applied estate (the topic must exist).
- `gh auth login`, unless you pass `--app-url`.

```bash
bash datadog/apply.sh --dry-run   # look up each object, show create/update, write nothing
bash datadog/apply.sh             # create or update by name
```

Where the targets come from:

- **Topic**: the topic name, endpoint and resource group come from the infra
  outputs `event_grid_topic_name`, `event_grid_endpoint` and
  `deployment_resource_group` (workspace `sentinel-dev`). Without them the script
  uses the naming defaults `evgt-sentinel-dev` / `rg-sentinel-dev-cc` and reads the
  endpoint from `az eventgrid topic show`.
- **Topic key**: read from `az eventgrid topic key list` at apply time.
- **App URL**: the `DEPLOYED_APP_URL` variable of the GitHub environment
  `sentinel-dev`.
- **Overrides**: `--env-file`, `--infra-dir`, `--topic`, `--rg` and `--app-url`.

Re-runs are safe. Every object is looked up by its exact name and updated in
place. If two objects share a name, the script refuses to continue rather than
guessing which one to update.

Secrets never reach argv or output:

- The Datadog keys go to curl through a 0600 header file.
- The topic key goes from `az` into a 0600 file that jq reads.
- A dry run prints each secret as a length.

**Re-run after every estate recreate.** A new topic has a new key and possibly a
new endpoint. Until you re-run, the webhook posts to a dead endpoint and no alert
reaches Sentinel.

## Cost

Synthetics API tests are billed per test run:

- Total: 2 tests × 1 location × 2 runs an hour ≈ **2,900 runs a month** (plus retries).
- Pricing is per 10,000 API test runs. Check the org's plan; the student-pack
  allowance may not include Synthetics.
- When you are not demoing, pause both tests in the Datadog UI, or set
  `"status": "paused"` in the JSON and re-apply.

The event monitor and the webhook cost nothing.
