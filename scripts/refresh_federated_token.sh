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

# Write a fresh GitHub OIDC assertion for the Entra ID token exchange to
# $AZURE_FEDERATED_TOKEN_FILE. A kubeconfig converted with `kubelogin
# convert-kubeconfig -l workloadidentity` reads the file on every kubectl
# token exchange. GitHub's assertion is valid for 5 minutes, so a job that
# outlives it runs this once in the foreground, failing on a bad first write,
# then in the background with --every to keep the file fresh until the job
# ends. A failed rewrite keeps the previous assertion and retries on the next
# interval.
#
# Exit codes:
#   0  assertion written (--every never returns)
#   1  the assertion request failed
#   2  prerequisite missing or bad argument

set -u
set -o pipefail

EVERY=""

usage() {
    cat <<'EOF'
Usage: refresh_federated_token.sh [--every SECONDS]
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --every) EVERY="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -n "$EVERY" ] && ! [[ "$EVERY" =~ ^[1-9][0-9]*$ ]]; then
    echo "error: --every takes a positive whole number of seconds, got '$EVERY'" >&2
    exit 2
fi

for var in ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN AZURE_FEDERATED_TOKEN_FILE; do
    if [ -z "${!var:-}" ]; then
        echo "error: $var is not set; the job needs id-token: write and a token file path" >&2
        exit 2
    fi
done
for tool in curl jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: $tool is required but not on PATH" >&2
        exit 2
    fi
done

write_assertion() {
    local tmp
    # A sibling temp file keeps the rename atomic, so a reader never sees a
    # partial JWT; -j omits the newline kubelogin would send as part of it.
    tmp=$(mktemp "${AZURE_FEDERATED_TOKEN_FILE}.XXXXXX") || return 1
    if curl -fsS --max-time 30 \
            -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
            "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=api://AzureADTokenExchange" \
        | jq -ej '.value' > "$tmp" && [ -s "$tmp" ] \
        && mv -f "$tmp" "$AZURE_FEDERATED_TOKEN_FILE"; then
        return 0
    fi
    rm -f "$tmp"
    return 1
}

if [ -z "$EVERY" ]; then
    if ! write_assertion; then
        echo "error: could not get a GitHub OIDC assertion for $AZURE_FEDERATED_TOKEN_FILE" >&2
        exit 1
    fi
    exit 0
fi

while :; do
    if ! write_assertion; then
        echo "warning: $(date -u +%H:%M:%S) assertion refresh failed; keeping the previous one" >&2
    fi
    sleep "$EVERY"
done
