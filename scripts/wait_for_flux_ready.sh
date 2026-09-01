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

# Poll Flux Kustomizations, reporting progress per spi-stack.layer, until every
# gating one is Ready. The Flux extension installs in the background after
# `spi up` returns, so the CRDs may not exist yet when this starts. An upgrade
# leaves every Kustomization Ready for the previous revision, so
# --expect-revision also requires status.lastAppliedRevision to name the commit.
#
# Exit codes:
#   0  every gating Kustomization is Ready
#   1  --timeout elapsed, or --grace elapsed before any Kustomization appeared
#   2  prerequisite missing (kubectl, jq)

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
    # The installed release wheel first; `uv run spi` only for a source checkout.
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

# The GitHub OIDC JWT azure/login writes lives 5 minutes; an az token refresh
# past that fails with AADSTS700024 and kills kubectl mid-wait. Rewriting the
# assertion file every 60s keeps a valid JWT ready for the next exchange.
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
        # Before the CRDs install, kubectl errors are expected noise.
        head -3 "$kubectl_err" >&2
    fi
    rm -f "$kubectl_err"

    total=$(jq -r '.items | length' <<<"$ksts_json")

    if [ "$total" = "0" ]; then
        # Once a Kustomization has been seen, an empty response is transient
        # (token refresh, API throttling), not a missing Flux extension.
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
            # spi-stack.gating: "false" stays out of the verdict, as in spi status.
            | map(select((.metadata.labels["spi-stack.gating"] // "true") != "false"))
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
