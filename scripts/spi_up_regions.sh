#!/usr/bin/env bash
# Run `spi up` in each region in turn. It moves to the next region only when
# AKS turns the cluster down for lack of capacity; any other failure stops.
#
# Usage: spi_up_regions.sh <resource-group> <env> <profile> <region> [fallback-region...]
set -uo pipefail

if [ $# -lt 4 ]; then
  echo "usage: $0 <resource-group> <env> <profile> <region> [fallback-region...]" >&2
  exit 2
fi
resource_group=$1
env_name=$2
profile=$3
shift 3

log=$(mktemp)
trap 'rm -f "$log"' EXIT

# aks.bicep creates the VNet and cluster identity before AKS rejects the
# cluster, and their locations are immutable, so the next region needs a fresh
# group. Its tags carry the sweeper's eligibility and spi's naming suffix.
recreate_group() {
  local region=$1 tags
  tags=$(az group show --name "$resource_group" --query tags -o json) || return 1
  local -a tag_args=()
  local tag
  while IFS= read -r tag; do
    tag_args+=("$tag")
  done < <(jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$tags")
  echo "Deleting $resource_group to retry in $region"
  az group delete --name "$resource_group" --yes || return 1
  az group create --name "$resource_group" --location "$region" --tags "${tag_args[@]}" -o none
}

regions=("$@")
for i in "${!regions[@]}"; do
  region=${regions[$i]}
  if [ "$i" -gt 0 ] && ! recreate_group "$region"; then
    echo "::error::Could not recreate $resource_group in $region."
    exit 1
  fi
  echo "spi up in $region"
  uv run spi up --env "$env_name" --profile "$profile" --location "$region" 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  if [ "$status" -eq 0 ]; then
    exit 0
  fi
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
