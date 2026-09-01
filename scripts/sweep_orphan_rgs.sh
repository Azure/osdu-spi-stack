#!/usr/bin/env bash
# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Reap resource groups that spi-stack CI provisioned and a cancelled pipeline
# never tore down. A group is reaped only when all three hold: its name starts
# with SWEEP_NAME_PREFIX, it carries the tag spi-ci-sweep-eligible=true, and its
# spi-created-utc tag is older than SWEEP_AGE_HOURS. The prefix gate keeps a
# production group that inherited the tag out of reach. A group with a broken
# tag is logged and skipped; the exit status is 0 either way.
#
# Env:
#   SWEEP_NAME_PREFIX  resource group name prefix (default "spi-stack-ci-")
#   SWEEP_AGE_HOURS    minimum age in hours (default 3)
#   SWEEPER_DRY_RUN    "true" lists candidates without deleting (default "false")

set -euo pipefail

: "${SWEEP_NAME_PREFIX:=spi-stack-ci-}"
: "${SWEEP_AGE_HOURS:=3}"
: "${SWEEPER_DRY_RUN:=false}"

now_epoch=$(date -u +%s)
age_threshold_sec=$((SWEEP_AGE_HOURS * 3600))

echo "=== spi orphan RG sweeper ==="
echo "  prefix:        ${SWEEP_NAME_PREFIX}"
echo "  age_threshold: ${SWEEP_AGE_HOURS}h"
echo "  dry_run:       ${SWEEPER_DRY_RUN}"
echo "  subscription:  $(az account show --query name -o tsv)"
echo

candidates=$(az group list --tag spi-ci-sweep-eligible=true --output json)
total=$(echo "${candidates}" | jq 'length')
echo "Candidates (tagged spi-ci-sweep-eligible=true): ${total}"
echo

reaped=0
while IFS=$'\t' read -r name created; do
  [[ -z "${name}" ]] && continue

  case "${name}" in
    "${SWEEP_NAME_PREFIX}"*) ;;
    *)
      echo "  skip (prefix mismatch): ${name}"
      continue
      ;;
  esac

  if [[ -z "${created}" ]]; then
    echo "  skip (no spi-created-utc tag): ${name}"
    continue
  fi

  # GNU date first, then BSD date for macOS.
  if created_epoch=$(date -u -d "${created}" +%s 2>/dev/null); then
    :
  elif created_epoch=$(date -u -jf "%Y-%m-%dT%H:%M:%SZ" "${created}" +%s 2>/dev/null); then
    :
  else
    echo "  skip (cannot parse spi-created-utc='${created}'): ${name}"
    continue
  fi

  age=$((now_epoch - created_epoch))
  age_hours=$((age / 3600))

  if (( age < age_threshold_sec )); then
    echo "  skip (age ${age_hours}h < ${SWEEP_AGE_HOURS}h): ${name}"
    continue
  fi

  if [[ "${SWEEPER_DRY_RUN}" == "true" ]]; then
    echo "  DRY RUN would delete (age ${age_hours}h): ${name}"
  else
    echo "  DELETE (age ${age_hours}h): ${name}"
    az group delete --name "${name}" --yes --no-wait
    reaped=$((reaped + 1))
  fi
done < <(echo "${candidates}" | jq -r '.[] | "\(.name)\t\(.tags["spi-created-utc"] // "")"')

echo
if [[ "${SWEEPER_DRY_RUN}" == "true" ]]; then
  echo "Sweeper complete (dry run); no groups were deleted."
else
  echo "Sweeper complete; ${reaped} delete request(s) accepted (async)."
fi
