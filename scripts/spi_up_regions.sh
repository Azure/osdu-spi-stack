#!/usr/bin/env bash
# Run `spi up` in each region in turn. It moves to the next region only when
# AKS turns the cluster down for lack of capacity; any other failure stops.
#
# Usage: spi_up_regions.sh <env> <profile> <region> [fallback-region...]
set -uo pipefail

if [ $# -lt 3 ]; then
  echo "usage: $0 <env> <profile> <region> [fallback-region...]" >&2
  exit 2
fi
env_name=$1
profile=$2
shift 2

log=$(mktemp)
trap 'rm -f "$log"' EXIT

for region in "$@"; do
  echo "spi up in $region"
  uv run spi up --env "$env_name" --profile "$profile" --location "$region" 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  if [ "$status" -eq 0 ]; then
    exit 0
  fi
  # The capacity rejection arrives before anything else in the resource group
  # is created, so the next region starts clean in the same group.
  # Rich wraps its error panel, so strip newlines, borders and spaces before
  # matching or the code can be split across lines.
  if tr -d '\r\n│ ' < "$log" | grep -q AKSCapacityHeavyUsage; then
    echo "::warning::AKS has no capacity for AKS Automatic in $region; trying the next region."
    continue
  fi
  exit "$status"
done

echo "::error::No region had AKS Automatic capacity: $*"
exit 1
