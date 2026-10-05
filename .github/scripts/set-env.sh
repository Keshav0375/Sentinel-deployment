#!/usr/bin/env bash
# set-env.sh NAME — append stdin to $GITHUB_ENV as NAME, for every later step of the job.
#
# Values here carry commit messages, author names and tool output, so a plain `NAME=value`
# line is unsafe: one embedded newline would start a variable of the attacker's choosing.
# The value is written in the multi-line form behind a random delimiter instead, and a name
# that is not a plain upper-case identifier is refused. Trailing newlines are dropped.
set -euo pipefail

name="${1:-}"
if [[ ! "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
  echo "set-env: '$name' is not an upper-case variable name" >&2
  exit 2
fi
: "${GITHUB_ENV:?set-env: GITHUB_ENV is not set}"

value="$(cat)"
delimiter="ghadelimiter_$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
if [[ "$value" == *"$delimiter"* ]]; then
  echo "set-env: value for $name contains the delimiter" >&2
  exit 2
fi
printf '%s<<%s\n%s\n%s\n' "$name" "$delimiter" "$value" "$delimiter" >>"$GITHUB_ENV"
