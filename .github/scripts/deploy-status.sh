#!/usr/bin/env bash
# deploy-status.sh — fold the step outcomes of ci_app_deployment.yml into the deploy's
# verdict (deployment §3.1 Stage 5/6, §6.1) and export what the record and summary steps need.
#
# Reads from env: the `steps.<id>.outcome` of CHECKOUT, META, BUILD, LOGIN, DEPLOY and
# VERIFY (success | failure | cancelled | skipped; empty reads as skipped), plus PR_NUMBER,
# PR_TITLE, APP_VERSION, DEPLOY_STARTED_AT, DD_SERVICE, DD_ENV and RUN_URL.
#
# Stage values are succeeded | failed | skipped:
#   build   failed when checkout, metadata or the zip failed (no package was built)
#   deploy  the Azure login + zip deploy; skipped unless the build succeeded
#   verify  skipped unless the deploy succeeded
# STATUS is exactly `succeeded` (all three succeeded) or `failed` — one value for the
# Datadog `deploy_status` tag and `deployments.deploy_status`. FAILED_STAGE is the first
# stage that failed (or, for a cancelled run, did not succeed); `none` when it succeeded.
#
# Exports to $GITHUB_ENV: STATUS, FAILED_STAGE, DEPLOY_ALERT_TYPE, DEPLOY_SUMMARY_TEXT,
# DEPLOY_LOG_PAYLOAD (the §3.1 `deploy.completed` log). Prints `<STATUS> <FAILED_STAGE>`
# on stdout for the calling step, which cannot read $GITHUB_ENV until it ends.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set_env() { bash "$here/set-env.sh" "$1"; }

# stage_of <outcome> — a step outcome as a stage value; a cancelled step did not succeed.
stage_of() {
  case "$1" in
    success) echo succeeded ;;
    failure | cancelled) echo failed ;;
    *) echo skipped ;;
  esac
}

if [[ "$(stage_of "${CHECKOUT_OUTCOME:-}")" == failed || "$(stage_of "${META_OUTCOME:-}")" == failed ]]; then
  build=failed
else
  build="$(stage_of "${BUILD_OUTCOME:-}")"
fi

deploy=skipped
if [[ "$build" == succeeded ]]; then
  login="$(stage_of "${LOGIN_OUTCOME:-}")"
  zip_deploy="$(stage_of "${DEPLOY_OUTCOME:-}")"
  if [[ "$login" == failed || "$zip_deploy" == failed ]]; then
    deploy=failed
  elif [[ "$login" == succeeded && "$zip_deploy" == succeeded ]]; then
    deploy=succeeded
  fi
fi

verify=skipped
[[ "$deploy" == succeeded ]] && verify="$(stage_of "${VERIFY_OUTCOME:-}")"

status=succeeded
failed_stage=none
if [[ "$verify" != succeeded ]]; then
  status=failed
  for stage in build deploy verify; do
    if [[ "${!stage}" == failed ]]; then
      failed_stage="$stage"
      break
    fi
  done
  # Nothing failed but the chain stopped (a cancelled run): blame the first unfinished stage.
  if [[ "$failed_stage" == none ]]; then
    for stage in build deploy verify; do
      if [[ "${!stage}" != succeeded ]]; then
        failed_stage="$stage"
        break
      fi
    done
  fi
fi

alert_type=info
[[ "$status" == succeeded ]] || alert_type=error

pr_number="${PR_NUMBER:-0}"
[[ "$pr_number" =~ ^[0-9]{1,9}$ ]] || pr_number=0
version="${APP_VERSION:-unknown}"
service="${DD_SERVICE:?DD_SERVICE is not set}"
env_name="${DD_ENV:?DD_ENV is not set}"

duration=0
started="${DEPLOY_STARTED_AT:-}"
if [[ "$started" =~ ^[0-9]+$ ]]; then
  duration=$(($(date +%s) - started))
  ((duration >= 0)) || duration=0
fi

summary="Stages: build=${build}, deploy=${deploy}, verify=${verify}. Duration ${duration}s."
[[ -z "${RUN_URL:-}" ]] || summary+=" Run: ${RUN_URL}"

payload="$(jq -cn \
  --arg service "$service" \
  --arg ddtags "version:${version},service:${service},env:${env_name},deploy_status:${status},failed_stage:${failed_stage}" \
  --argjson pr_number "$pr_number" \
  --arg version "$version" \
  --arg pr_title "${PR_TITLE:-}" \
  --arg status "$status" \
  --arg failed_stage "$failed_stage" \
  --argjson duration "$duration" \
  --arg build "$build" \
  --arg deploy "$deploy" \
  --arg verify "$verify" \
  '{
     message: "deploy.completed",
     ddsource: "github-actions",
     ddtags: $ddtags,
     hostname: "gha-runner",
     service: $service,
     deploy: {
       pr_number: $pr_number,
       version: $version,
       pr_title: $pr_title,
       status: $status,
       failed_stage: $failed_stage,
       duration_seconds: $duration,
       stages: {build: $build, deploy: $deploy, verify: $verify}
     }
   }')"

set_env STATUS <<<"$status"
set_env FAILED_STAGE <<<"$failed_stage"
set_env DEPLOY_ALERT_TYPE <<<"$alert_type"
set_env DEPLOY_SUMMARY_TEXT <<<"$summary"
set_env DEPLOY_LOG_PAYLOAD <<<"$payload"

echo "deploy ${status} (failed_stage ${failed_stage}); build=${build} deploy=${deploy} verify=${verify}" >&2
echo "$status $failed_stage"
