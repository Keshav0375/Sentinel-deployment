#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Apply the Datadog side of Sentinel's trigger path (deployment §6.3).
#
#     bash datadog/apply.sh --dry-run    # look up, render, change nothing
#     bash datadog/apply.sh              # create or update by name
#     bash datadog/apply.sh --env-file ../Sentinel-infra/.env --topic evgt-sentinel-dev \
#          --rg rg-sentinel-dev-cc --app-url https://app-sentinel-dev-xxxx.azurewebsites.net
#
# Run by the owner, never CI: it needs the Datadog application key. Re-run after
# every estate recreate, because the Event Grid topic key changes with the topic.
#
# What it applies, in order (a monitor cannot mention a webhook that does not exist):
#   webhook.json      Webhooks-integration webhook `sentinel-event-grid`
#   monitors/*.json   `sentinel-deploy-failure` (event-v2 alert on deploy_status:failed)
#   synthetics/*.json `sentinel-runtime-health GET /` and `GET /health` (API tests)
#
# ── Idempotent by name ───────────────────────────────────────────────────────
# Each object is looked up by its exact name first: found → PUT, missing → POST.
# Two monitors or tests with the same name is refused, never guessed at.
#
# ── Secret handling ──────────────────────────────────────────────────────────
# DD_API_KEY / DD_APP_KEY are parsed (never sourced) from the env file and reach
# curl only through a 0600 header file. The Event Grid key goes from `az` straight
# into a 0600 file that jq reads with --rawfile. No secret is in any argv, and a
# dry run reports each one as a length.
#
# ── Where the target comes from ──────────────────────────────────────────────
# Flags win. Otherwise the topic name and endpoint come from the infra outputs
# `event_grid_topic_name` / `event_grid_endpoint` (and the resource group from
# `deployment_resource_group`) of workspace sentinel-dev, read with TF_WORKSPACE
# so the checkout's selected workspace is not changed. Without them the
# deployment's naming applies: evgt-sentinel-dev in rg-sentinel-dev-cc, endpoint
# from `az eventgrid topic show`. The app URL is --app-url or the
# DEPLOYED_APP_URL variable of the GitHub environment sentinel-dev.
#
# Requires: jq, curl, az (logged in), and gh (authenticated) unless --app-url.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

CR="$(printf '\r')"
nocr() { tr -d "${CR}"; }

# Run from the repo root, so the default ../Sentinel-infra paths resolve the
# same wherever this is invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")/.."
DIR="datadog"

ENV_FILE="../Sentinel-infra/.env"
INFRA_DIR="../Sentinel-infra"
WORKSPACE="sentinel-dev"
GH_REPO="Keshav0375/Sentinel-deployment"
GH_ENVIRONMENT="sentinel-dev"
DEFAULT_TOPIC="evgt-sentinel-dev"
DEFAULT_RG="rg-sentinel-dev-cc"
WEBHOOK_NAME="sentinel-event-grid"
TOPIC=""
RG=""
APP_URL=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)   DRY_RUN=1; shift ;;
    --env-file)  ENV_FILE="$2"; shift 2 ;;
    --infra-dir) INFRA_DIR="$2"; shift 2 ;;
    --topic)     TOPIC="$2"; shift 2 ;;
    --rg)        RG="$2"; shift 2 ;;
    --app-url)   APP_URL="$2"; shift 2 ;;
    -h|--help)   sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

# ── Preconditions, each with the fix in the message ──────────────────────────
for tool in jq curl az; do
  command -v "${tool}" >/dev/null 2>&1 || die "${tool} is not installed."
done
[ -f "${ENV_FILE}" ] || die "${ENV_FILE} not found. Pass --env-file <path> to the .env holding DD_API_KEY, DD_APP_KEY and DD_SITE."

# Split on the first `=`, strip a trailing CR and one matched pair of quotes,
# execute nothing (same rules as Sentinel-infra/scripts/push-deploy-config.sh).
env_get() {
  local file="$1" want="$2" line key value first last
  while IFS= read -r line || [ -n "${line}" ]; do
    line="${line%"${CR}"}"
    case "${line}" in ''|\#*) continue ;; esac
    key="$(printf '%s' "${line%%=*}" | tr -d '[:space:]')"
    [ "${key}" = "${want}" ] || continue
    value="${line#*=}"
    first="${value%"${value#?}"}"
    last="${value#"${value%?}"}"
    if [ ${#value} -ge 2 ] && [ "${first}" = "${last}" ] \
       && { [ "${first}" = '"' ] || [ "${first}" = "'" ]; }; then
      value="${value#?}"
      value="${value%?}"
    fi
    printf '%s' "${value}"
    return 0
  done <"${file}"
}

DD_API_KEY="$(env_get "${ENV_FILE}" DD_API_KEY)"
DD_APP_KEY="$(env_get "${ENV_FILE}" DD_APP_KEY)"
DD_SITE="$(env_get "${ENV_FILE}" DD_SITE)"
[ -n "${DD_API_KEY}" ] || die "DD_API_KEY is missing from ${ENV_FILE}."
[ -n "${DD_APP_KEY}" ] || die "DD_APP_KEY is missing from ${ENV_FILE}. The API key alone cannot read or write monitors: create an application key in Datadog (Organization Settings → Application Keys) and add DD_APP_KEY=… to ${ENV_FILE}."
# A bare hostname only: the site is spliced into every URL.
[[ "${DD_SITE}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] \
  || die "DD_SITE in ${ENV_FILE} is not a Datadog site hostname (this org: us5.datadoghq.com)."
API="https://api.${DD_SITE}"

workdir="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/dd-apply.XXXXXX")"
trap 'rm -rf "${workdir}"' EXIT
headers="${workdir}/headers"
eg_key="${workdir}/eg-key"
(umask 077 && printf 'DD-API-KEY: %s\nDD-APPLICATION-KEY: %s\nContent-Type: application/json\n' \
  "${DD_API_KEY}" "${DD_APP_KEY}" >"${headers}")

# ── Target: Event Grid topic and the app ─────────────────────────────────────
tf_output() {
  command -v terraform >/dev/null 2>&1 || return 0
  TF_WORKSPACE="${WORKSPACE}" terraform -chdir="${INFRA_DIR}" output -raw "$1" 2>/dev/null | nocr || true
}

az account show >/dev/null 2>&1 || die "az is not logged in. Run: az login"

ENDPOINT=""
if [ -z "${TOPIC}" ]; then
  TOPIC="$(tf_output event_grid_topic_name)"
  [ -n "${TOPIC}" ] && ENDPOINT="$(tf_output event_grid_endpoint)"
fi
[ -n "${RG}" ] || RG="$(tf_output deployment_resource_group)"
TOPIC="${TOPIC:-${DEFAULT_TOPIC}}"
RG="${RG:-${DEFAULT_RG}}"
if [ -z "${ENDPOINT}" ]; then
  ENDPOINT="$(az eventgrid topic show --name "${TOPIC}" --resource-group "${RG}" \
    --query endpoint -o tsv 2>/dev/null | nocr || true)"
fi
[[ "${ENDPOINT}" == https://* ]] \
  || die "no endpoint for Event Grid topic ${TOPIC} in ${RG}. Is the estate applied? Pass --topic/--rg if it is named differently."

(umask 077 && az eventgrid topic key list --name "${TOPIC}" --resource-group "${RG}" \
  --query key1 -o tsv 2>/dev/null | tr -d '\r\n' >"${eg_key}") || true
[ -s "${eg_key}" ] || die "could not read the key of Event Grid topic ${TOPIC} in ${RG}."

if [ -z "${APP_URL}" ]; then
  command -v gh >/dev/null 2>&1 || die "gh is not installed; pass --app-url."
  APP_URL="$(gh variable get DEPLOYED_APP_URL --env "${GH_ENVIRONMENT}" --repo "${GH_REPO}" 2>/dev/null | nocr || true)"
fi
APP_URL="${APP_URL%/}"
[[ "${APP_URL}" =~ ^https://[A-Za-z0-9.-]+$ ]] \
  || die "app URL '${APP_URL}' is not https://<host>. Pass --app-url, or set DEPLOYED_APP_URL on environment ${GH_ENVIRONMENT}."

echo "Datadog      ${API}  (DD_API_KEY ${#DD_API_KEY} chars, DD_APP_KEY ${#DD_APP_KEY} chars)"
echo "Event Grid   ${TOPIC} in ${RG}  →  ${ENDPOINT}  (aeg-sas-key $(wc -c <"${eg_key}" | tr -d ' ') chars)"
echo "App          ${APP_URL}"
[ "${DRY_RUN}" -eq 1 ] && echo "DRY RUN — lookups only, nothing is written."

# ── Datadog API ──────────────────────────────────────────────────────────────
# dd <method> <path> [body-file] — the response lands in $workdir/response; prints
# the HTTP status. Status handling is the caller's: a 404 is an answer on lookups.
dd() {
  local args=(-sS --max-time 30 -X "$1" "${API}$2" -H @"${headers}"
    -o "${workdir}/response" -w '%{http_code}')
  [ $# -ge 3 ] && args+=(--data-binary @"$3")
  # No response is not "not applied": a write can land after the connection drops
  # (seen live — a synthetics POST created the test but never answered). Every
  # write here is create-or-update by name, so a re-run reconciles it.
  curl "${args[@]}" || die "$1 $2: no response from Datadog — it may have applied it anyway; re-run apply.sh (idempotent) to reconcile."
}

# write <method> <path> <body-file> <label> — the one place that changes Datadog.
write() {
  local status
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "  would $1 $2"
    return 0
  fi
  status="$(dd "$1" "$2" "$3")"
  case "${status}" in
    2??) echo "  $1 $2 → ${status}" ;;
    *) die "$4: $1 $2 returned HTTP ${status}: $(head -c 600 "${workdir}/response")" ;;
  esac
}

# ── 1. Webhook ───────────────────────────────────────────────────────────────
# Datadog stores payload and custom_headers as JSON *strings*. The key is read
# from its file by jq, so it is in the body file (0600) and nowhere else.
# Event Grid's publish API takes an ARRAY of events, CustomEventSchema topics
# included, so the payload must be a one-element array of the flat object;
# anything else is refused here rather than rejected by Event Grid at alert time.
apply_webhook() {
  local body="${workdir}/webhook.body" base="/api/v1/integration/webhooks/configuration/webhooks"
  jq -e --arg url "${ENDPOINT}" --rawfile key "${eg_key}" '
      if (.payload | type) == "array" and (.payload | length) == 1
         and (.payload[0] | type) == "object"
      then . else error("payload must be a one-element array of one event object") end
      | .url = $url
      | .payload |= tojson
      | .custom_headers = ({"aeg-sas-key": $key} | tojson)
    ' "${DIR}/webhook.json" >"${body}" \
    || die "${DIR}/webhook.json: payload must be a one-element array of one event object (Event Grid takes an array)."
  echo "webhook ${WEBHOOK_NAME}  → ${ENDPOINT}"
  if [ "${DRY_RUN}" -eq 1 ]; then
    jq --arg len "$(wc -c <"${eg_key}" | tr -d ' ')" \
      '.custom_headers = "{\"aeg-sas-key\":\"<\($len) chars>\"}" | {payload, custom_headers}' "${body}" \
      | sed 's/^/    /'
  fi
  local status
  status="$(dd GET "${base}/${WEBHOOK_NAME}")"
  case "${status}" in
    200) write PUT "${base}/${WEBHOOK_NAME}" "${body}" "webhook" ;;
    404) write POST "${base}" "${body}" "webhook" ;;
    *) die "webhook lookup returned HTTP ${status}: $(head -c 600 "${workdir}/response")" ;;
  esac
}

# ── 2. Monitors ──────────────────────────────────────────────────────────────
# ?name= is a substring match, so the exact name is selected here. Synthetics
# tests own monitors of their own (type "synthetics alert"); those are left alone.
apply_monitor() {
  local file="$1" name query status ids
  name="$(jq -r .name "${file}")"
  query="$(jq -rn --arg n "${name}" '$n | @uri')"
  echo "monitor ${name}"
  status="$(dd GET "/api/v1/monitor?name=${query}")"
  [ "${status}" = 200 ] || die "monitor lookup returned HTTP ${status}: $(head -c 600 "${workdir}/response")"
  ids="$(jq -r --arg n "${name}" \
    '[.[] | select(.name == $n and .type != "synthetics alert") | .id] | map(tostring) | join(" ")' \
    "${workdir}/response")"
  case "${ids}" in
    "") write POST "/api/v1/monitor" "${file}" "monitor ${name}" ;;
    *" "*) die "more than one monitor is named ${name} (ids ${ids}); delete the extras in Datadog first." ;;
    *) write PUT "/api/v1/monitor/${ids}" "${file}" "monitor ${name}" ;;
  esac
}

# ── 3. Synthetics API tests ──────────────────────────────────────────────────
apply_synthetic() {
  local file="$1" body name ids
  body="${workdir}/$(basename "${file}").body"
  jq --arg app "${APP_URL}" '.config.request.url |= sub("__APP_URL__"; $app)' "${file}" >"${body}"
  name="$(jq -r .name "${body}")"
  echo "synthetic test ${name}  ($(jq -r .config.request.url "${body}"))"
  ids="$(jq -r --arg n "${name}" \
    '[(.tests // [])[] | select(.name == $n) | .public_id] | join(" ")' "${workdir}/tests")"
  case "${ids}" in
    "") write POST "/api/v1/synthetics/tests/api" "${body}" "synthetic ${name}" ;;
    *" "*) die "more than one synthetic test is named ${name} (${ids}); delete the extras in Datadog first." ;;
    *) write PUT "/api/v1/synthetics/tests/api/${ids}" "${body}" "synthetic ${name}" ;;
  esac
}

apply_webhook

for file in "${DIR}"/monitors/*.json; do
  apply_monitor "${file}"
done

status="$(dd GET "/api/v1/synthetics/tests")"
[ "${status}" = 200 ] || die "synthetics lookup returned HTTP ${status}: $(head -c 600 "${workdir}/response")"
cp "${workdir}/response" "${workdir}/tests"
for file in "${DIR}"/synthetics/*.json; do
  apply_synthetic "${file}"
done

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "dry run complete — re-run without --dry-run to apply."
else
  echo "applied. Re-run after every estate recreate: the topic key changes."
fi
