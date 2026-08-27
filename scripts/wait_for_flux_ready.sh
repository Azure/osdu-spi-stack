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

# Watch Flux Kustomizations across the cluster and exit when all are Ready.
#
# Designed for the Azure smoke CI job. After `spi up` returns, the AKS Flux
# extension has been told to install via `--no-wait`, so CRDs and the
# GitRepository may not yet exist; reconciliation then proceeds in the
# background. This script:
#
#   1. Polls `kubectl get kustomizations -A` and groups results by the
#      `spi-stack.layer` label so progress is visible per dependency tier.
#   2. Emits a compact heartbeat every --heartbeat seconds so a human
#      tailing the log never sees more than 30s of silence.
#   3. Prints a checkpoint banner the moment a layer flips to fully Ready.
#   4. Dumps `spi status` every --status-interval seconds for the rich
#      table view in the log and one final time on exit (success or fail).
#
# An upgrade re-points the source at a new revision while every Kustomization
# still reports Ready for the previous one, so --expect-revision additionally
# requires status.lastAppliedRevision to name the given commit (ADR-029).
#
# Exit codes:
#   0  every Kustomization Ready=True
#   1  --timeout elapsed, or --grace elapsed before any Kustomization appeared
#   2  prerequisite missing (kubectl/jq)

set -u
set -o pipefail

TIMEOUT=1800
HEARTBEAT=30
STATUS_INTERVAL=180
GRACE=600
EXPECT_REVISION=""

usage() {
    cat <<'EOF'
Usage: wait_for_flux_ready.sh [--timeout SECONDS] [--heartbeat SECONDS]
                              [--status-interval SECONDS] [--grace SECONDS]
                              [--expect-revision COMMIT]
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --heartbeat) HEARTBEAT="$2"; shift 2 ;;
        --status-interval) STATUS_INTERVAL="$2"; shift 2 ;;
        --grace) GRACE="$2"; shift 2 ;;
        --expect-revision) EXPECT_REVISION="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

for tool in kubectl jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: $tool is required but not on PATH" >&2
        exit 2
    fi
done

fmt_mmss() { printf '%02d:%02d' $(( $1 / 60 )) $(( $1 % 60 )); }

dump_status() {
    # Prefer an installed `spi` (the lifecycle workflows' declared release
    # wheel); fall back to `uv run spi` only for a source checkout that has
    # never installed the CLI, e.g. local development or the Smoke job.
    if command -v spi >/dev/null 2>&1; then
        spi status 2>&1 || true
    elif command -v uv >/dev/null 2>&1; then
        uv run spi status 2>&1 || true
    else
        kubectl get kustomizations -A 2>&1 || true
    fi
}

print_banner() {
    echo
    echo "─── $1 ───"
    echo
}

START=$SECONDS
LAST_STATUS=0
LAST_FED_REFRESH=0
SEEN_KUSTOMIZATIONS=0
declare -A LAYER_DONE

# When run inside a GitHub Actions job with id-token:write permission, the
# OIDC JWT minted by azure/login@v3 lives only 5 minutes. az caches AAD
# access tokens for 1h, but if kubelogin/az ever needs a refresh past the
# 5-min JWT window, AADSTS700024 kills kubectl mid-wait. Refresh the
# assertion file every 60s so the next az exchange (whenever it lands)
# has a valid JWT to swap in.
refresh_federated_assertion() {
    if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] \
       || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ] \
       || [ -z "${AZURE_FEDERATED_TOKEN_FILE:-}" ]; then
        return 0
    fi
    local url="${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=api://AzureADTokenExchange"
    local tmp="${AZURE_FEDERATED_TOKEN_FILE}.new"
    if curl -fsSL -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" "$url" 2>/dev/null \
        | jq -r '.value // empty' > "$tmp" 2>/dev/null; then
        if [ -s "$tmp" ]; then
            mv -f "$tmp" "$AZURE_FEDERATED_TOKEN_FILE"
        else
            rm -f "$tmp"
        fi
    else
        rm -f "$tmp" 2>/dev/null || true
    fi
}

BANNER="wait_for_flux_ready · timeout $(fmt_mmss "$TIMEOUT") · heartbeat ${HEARTBEAT}s · spi status every ${STATUS_INTERVAL}s"
if [ -n "$EXPECT_REVISION" ]; then
    BANNER="$BANNER · revision $EXPECT_REVISION"
fi
print_banner "$BANNER"

while :; do
    elapsed=$(( SECONDS - START ))

    if [ $(( elapsed - LAST_FED_REFRESH )) -ge 60 ]; then
        refresh_federated_assertion
        LAST_FED_REFRESH=$elapsed
    fi

    if [ "$elapsed" -ge "$TIMEOUT" ]; then
        echo
        echo "ERROR: timed out after $(fmt_mmss "$elapsed") waiting for Kustomizations to become Ready${EXPECT_REVISION:+ at $EXPECT_REVISION}" >&2
        echo "Final status:" >&2
        dump_status >&2
        exit 1
    fi

    kubectl_err=$(mktemp)
    if ! ksts_json=$(kubectl get kustomizations -A -o json 2>"$kubectl_err"); then
        ksts_json='{"items":[]}'
    fi
    if [ -s "$kubectl_err" ] && [ "$SEEN_KUSTOMIZATIONS" -eq 1 ]; then
        # Only surface kubectl errors once we have a baseline; pre-Flux
        # bootstrap commonly returns "the server doesn't have a resource
        # type" while CRDs are still installing.
        head -3 "$kubectl_err" >&2
    fi
    rm -f "$kubectl_err"

    total=$(jq -r '.items | length' <<<"$ksts_json")

    if [ "$total" = "0" ]; then
        # The grace window only applies before we have *ever* seen a
        # Kustomization. Once we have, a transient empty response from
        # kubectl (AAD token refresh, brief API throttling, etc.) must
        # not be confused with "Flux extension never installed CRDs."
        if [ "$SEEN_KUSTOMIZATIONS" -eq 0 ] && [ "$elapsed" -ge "$GRACE" ]; then
            echo
            echo "ERROR: no Flux Kustomizations visible after ${GRACE}s grace -- AKS Flux extension never installed CRDs" >&2
            dump_status >&2
            exit 1
        fi
        if [ "$SEEN_KUSTOMIZATIONS" -eq 0 ]; then
            printf '[%s / %s] waiting for Flux extension to surface Kustomizations...\n' \
                "$(fmt_mmss "$elapsed")" "$(fmt_mmss "$TIMEOUT")"
        else
            printf '[%s / %s] transient empty kubectl response; retrying...\n' \
                "$(fmt_mmss "$elapsed")" "$(fmt_mmss "$TIMEOUT")"
        fi
    else
        SEEN_KUSTOMIZATIONS=1
        per_layer=$(jq -c --arg rev "$EXPECT_REVISION" '
            def converged:
                ((.status.conditions // []) | any(.type == "Ready" and .status == "True"))
                and ($rev == "" or ((.status.lastAppliedRevision // "") | contains($rev)));
            .items
            | group_by(.metadata.labels["spi-stack.layer"] // "-")
            | map({
                layer: (.[0].metadata.labels["spi-stack.layer"] // "-"),
                total: length,
                ready: ([ .[] | select(converged) ] | length)
              })
            | sort_by(.layer)
        ' <<<"$ksts_json")

        ready_total=$(jq -r '[.[].ready] | add // 0' <<<"$per_layer")
        all_total=$(jq -r '[.[].total] | add // 0' <<<"$per_layer")

        while IFS=$'\t' read -r layer ready total_in_layer; do
            [ -z "$layer" ] && continue
            if [ "$ready" = "$total_in_layer" ] && [ -z "${LAYER_DONE[$layer]:-}" ]; then
                LAYER_DONE[$layer]=1
                print_banner "✓ Layer ${layer} ready (${total_in_layer}/${total_in_layer}) at $(fmt_mmss "$elapsed")"
            fi
        done < <(jq -r '.[] | [.layer, .ready, .total] | @tsv' <<<"$per_layer")

        compact=$(jq -r '
            map("L\(.layer) \(.ready)/\(.total)" + (if .ready == .total then " ✓" else "" end))
            | join(" · ")
        ' <<<"$per_layer")

        printf '[%s / %s] %s/%s Ready · %s\n' \
            "$(fmt_mmss "$elapsed")" "$(fmt_mmss "$TIMEOUT")" \
            "$ready_total" "$all_total" "$compact"

        if [ "$ready_total" = "$all_total" ] && [ "$all_total" -gt 0 ]; then
            print_banner "✓ all $all_total Kustomizations Ready at $(fmt_mmss "$elapsed")"
            dump_status
            exit 0
        fi
    fi

    if [ $(( elapsed - LAST_STATUS )) -ge "$STATUS_INTERVAL" ]; then
        echo
        echo "─── status checkpoint at $(fmt_mmss "$elapsed") ───"
        dump_status
        echo
        LAST_STATUS=$elapsed
    fi

    sleep "$HEARTBEAT"
done
