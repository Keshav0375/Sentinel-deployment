#!/usr/bin/env bash
# deploy-metadata.sh — Stage 1 of ci_app_deployment.yml (deployment §3.1): derive the
# deploy's identity from the merge commit and export it to $GITHUB_ENV for every later step.
#
# Reads from env: GITHUB_SHA, COMMIT_MESSAGE (head_commit.message), COMMIT_AUTHOR_USERNAME,
# COMMIT_AUTHOR_NAME. Runs inside the checkout (fetch-depth 2, so the first parent exists).
#
# Exports:
#   PR_NUMBER           from the squash subject `… (#N)` or `Merge pull request #N …`;
#                       0 for a direct push, so it always casts with `::int`
#   SHORT_SHA           GITHUB_SHA[:7]
#   PR_TITLE            the PR title: the squash subject without its `(#N)`, the first body
#                       line of a merge commit, or the subject of a direct push (≤ 200 chars)
#   APP_VERSION         pr-<PR_NUMBER>-<SHORT_SHA>
#   PR_AUTHOR           head_commit author username, else name, else `unknown` (≤ 100 chars)
#   FILES_CHANGED_JSON  JSON array of the paths the commit changed against its first parent
#                       (≤ 100 entries; `[]` with a warning when git cannot tell)
#   DEPLOY_STARTED_AT   epoch seconds, for the summary's duration_seconds
#
# Commit messages and author names are attacker-influenced: they arrive through env, are
# written with set-env.sh's random delimiter, and are never echoed to the log (a line
# starting `::` would be read as a workflow command).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set_env() { bash "$here/set-env.sh" "$1"; }

MAX_FILES=100
MAX_TITLE=200
MAX_AUTHOR=100

sha="${GITHUB_SHA:-}"
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error title=deploy-metadata::GITHUB_SHA is not a full commit SHA"
  exit 1
fi
short_sha="${sha:0:7}"

message="${COMMIT_MESSAGE:-}"
message="${message//$'\r'/}"
subject="${message%%$'\n'*}"
body=""
[[ "$message" == *$'\n'* ]] && body="${message#*$'\n'}"

# Nine digits at most: always a valid int4 for `:'pr'::int`.
merge_re='^Merge pull request #([0-9]{1,9})( |$)'
squash_re='^(.*[^ ]) +\(#([0-9]{1,9})\) *$'
pr_number=0
title="$subject"
if [[ "$subject" =~ $merge_re ]]; then
  pr_number="${BASH_REMATCH[1]}"
  # A merge commit carries the PR title as the first non-blank line of its body.
  while IFS= read -r line; do
    if [[ "$line" =~ [^[:space:]] ]]; then
      title="$line"
      break
    fi
  done <<<"$body"
elif [[ "$subject" =~ $squash_re ]]; then
  title="${BASH_REMATCH[1]}"
  pr_number="${BASH_REMATCH[2]}"
fi
pr_number="$((10#$pr_number))"
[[ -n "$title" ]] || title="(no commit message)"
title="${title:0:MAX_TITLE}"

author="${COMMIT_AUTHOR_USERNAME:-}"
[[ -n "$author" ]] || author="${COMMIT_AUTHOR_NAME:-}"
author="${author//[$'\r\n']/ }"
[[ -n "$author" ]] || author="unknown"
author="${author:0:MAX_AUTHOR}"

# -z + split on NUL keeps paths with spaces, quotes or newlines intact; jq builds the JSON.
if git rev-parse -q --verify "${sha}^1" >/dev/null 2>&1; then
  diff_args=("${sha}^1" "$sha")
else
  diff_args=(--root "$sha")
fi
if ! files_json="$(git diff-tree -r -z --no-commit-id --name-only "${diff_args[@]}" |
  jq -Rsc --argjson max "$MAX_FILES" 'split("\u0000") | map(select(length > 0)) | .[:$max]')"; then
  echo "::warning title=deploy-metadata::could not list the files changed by ${short_sha}; recording []"
  files_json='[]'
fi

app_version="pr-${pr_number}-${short_sha}"

set_env PR_NUMBER <<<"$pr_number"
set_env SHORT_SHA <<<"$short_sha"
set_env PR_TITLE <<<"$title"
set_env APP_VERSION <<<"$app_version"
set_env PR_AUTHOR <<<"$author"
set_env FILES_CHANGED_JSON <<<"$files_json"
set_env DEPLOY_STARTED_AT <<<"$(date +%s)"

echo "deploy metadata: PR #${pr_number}, version ${app_version}, $(jq length <<<"$files_json") file(s) changed"
