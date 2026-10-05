#!/usr/bin/env bash
# dd-report: the deployment §3.2 helpers (send_dd_event, send_dd_log) behind one action.
#
# Reads DD_TITLE, DD_TEXT, DD_TAGS, DD_ALERT_TYPE, DD_LOG_PAYLOAD, DD_API_KEY, DD_SITE
# from env (action.yml maps the inputs). Writes `event-id` to $GITHUB_OUTPUT when the
# event was created; an unset step output reads as empty.
#
# Fail soft: every failure is a ::warning:: and the script always exits 0, so a Datadog
# outage never fails the deploy it is reporting on. JSON bodies are built with jq from
# env, never by string interpolation: titles and texts carry attacker-influenced PR titles
# and commit messages. The key travels in a 0600 header file, never in curl's argv, and
# is never printed.
set -uo pipefail

# Workflow-command escaping (%, CR, LF), so a value echoed back in a warning can never
# start a command line of its own.
warn() {
  local msg="$*"
  msg="${msg//'%'/%25}"
  msg="${msg//$'\r'/%0D}"
  msg="${msg//$'\n'/%0A}"
  echo "::warning title=dd-report::$msg"
}

output="${GITHUB_OUTPUT:-/dev/null}"

if [[ -z "${DD_API_KEY:-}" ]]; then
  warn "dd-api-key is empty; nothing reported."
  exit 0
fi
echo "::add-mask::${DD_API_KEY}"

# A bare hostname only: the site is spliced into the URL, so anything else is refused.
if [[ ! "${DD_SITE:-}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
  warn "dd-site '${DD_SITE:-}' is not a Datadog site hostname (e.g. us5.datadoghq.com); nothing reported."
  exit 0
fi

if ! command -v jq >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  warn "jq and curl are required on the runner; nothing reported."
  exit 0
fi

workdir="$(umask 077 && mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/dd-report.XXXXXX")" || {
  warn "could not create a temp dir; nothing reported."
  exit 0
}
trap 'rm -rf "$workdir"' EXIT
headers="$workdir/headers"
printf 'DD-API-KEY: %s\nContent-Type: application/json\n' "$DD_API_KEY" >"$headers"

# post <url> <body-file> <response-file> — prints the HTTP status (000 when none came back);
# returns curl's exit code.
post() {
  curl -sS --fail-with-body --retry 2 --max-time 15 \
    -X POST "$1" -H @"$headers" --data-binary @"$2" -o "$3" -w '%{http_code}'
}

send_dd_event() {
  local body="$workdir/event.json" response="$workdir/event.response" status rc event_id
  case "${DD_ALERT_TYPE:-}" in
    info | error) ;;
    *)
      warn "alert-type '${DD_ALERT_TYPE:-}' is not info|error; event not sent."
      return 0
      ;;
  esac
  if ! jq -cn \
    --arg title "${DD_TITLE:-}" \
    --arg text "${DD_TEXT:-}" \
    --arg tags "${DD_TAGS:-}" \
    --arg alert_type "$DD_ALERT_TYPE" \
    '{
       title: $title,
       text: $text,
       tags: ($tags | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))),
       alert_type: $alert_type,
       source_type_name: "github"
     }' >"$body"; then
    warn "could not build the event body; event not sent."
    return 0
  fi

  status="$(post "https://api.${DD_SITE}/api/v1/events" "$body" "$response")"
  rc=$?
  if ((rc != 0)); then
    warn "event POST failed (HTTP ${status:-000}, curl exit $rc)."
    return 0
  fi

  # id_str first: event ids exceed 2^53, and a jq built on doubles would round `.event.id`.
  event_id="$(jq -r '.event.id_str // .event.id // empty | tostring' "$response" 2>/dev/null)"
  if [[ "$event_id" =~ ^[0-9]+$ ]]; then
    echo "event-id=$event_id" >>"$output"
  else
    warn "event sent (HTTP $status) but the response carried no event id."
  fi
}

send_dd_log() {
  local body="$workdir/log.json" response="$workdir/log.response" status rc
  [[ -n "${DD_LOG_PAYLOAD:-}" ]] || return 0
  # Slurp, so `{} {}` (two values) is refused instead of producing two bodies.
  if ! jq -cse 'if length == 1 and (.[0] | type) == "object" then . else error("not one object") end' \
    <<<"$DD_LOG_PAYLOAD" >"$body" 2>/dev/null; then
    warn "log-payload is not a JSON object; log not sent."
    return 0
  fi

  status="$(post "https://http-intake.logs.${DD_SITE}/api/v2/logs" "$body" "$response")"
  rc=$?
  if ((rc != 0)); then
    warn "log POST failed (HTTP ${status:-000}, curl exit $rc)."
  fi
}

send_dd_event
send_dd_log
exit 0
