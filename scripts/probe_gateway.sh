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

# Acceptance probes for the Istio ingress gateway. A pod-level check cannot
# catch a dead TLS path: stalled cert-manager issuance leaves every pod Running
# while :443 refuses connections, so the https probe retries through ACME lag.
#
# Usage: probe_gateway.sh <gateway|https>
#   gateway  the add-on Service exists and has a ready endpoint
#   https    a handshake against the Gateway's HTTPS listener; no-op in ip mode
#
# Exit codes: 0 passed or not applicable, 1 failed, 2 usage error.

set -euo pipefail

# The Service the AKS managed Istio add-on owns; the Gateway binds to it by hostname.
ISTIO_NAMESPACE="aks-istio-ingress"
ISTIO_INGRESS_SERVICE="aks-istio-ingressgateway-external"

usage() {
    cat <<'EOF'
Usage: probe_gateway.sh <gateway|https>
EOF
}

probe_gateway() {
    # The jsonpath reads only ready addresses (notReadyAddresses is a separate
    # array), and stderr is kept out of `addresses` so the Kubernetes 1.33+
    # Endpoints deprecation warning cannot satisfy the non-empty check.
    local addresses stderr_file stderr_output
    stderr_file=$(mktemp)
    if ! addresses=$(kubectl get endpoints "$ISTIO_INGRESS_SERVICE" -n "$ISTIO_NAMESPACE" \
        -o jsonpath='{.subsets[*].addresses[*].ip}' 2>"$stderr_file"); then
        stderr_output=$(cat "$stderr_file")
        rm -f "$stderr_file"
        echo "Failed to read endpoints for ${ISTIO_INGRESS_SERVICE} in ${ISTIO_NAMESPACE}: ${stderr_output}" >&2
        kubectl get svc -n "$ISTIO_NAMESPACE" || true
        kubectl get pods -n "$ISTIO_NAMESPACE" || true
        return 1
    fi
    stderr_output=$(cat "$stderr_file")
    rm -f "$stderr_file"
    if [[ -z "$addresses" ]]; then
        echo "${ISTIO_INGRESS_SERVICE} exists but has no ready endpoint addresses; dumping service and pod state" >&2
        [[ -n "$stderr_output" ]] && echo "kubectl stderr: ${stderr_output}" >&2
        kubectl get svc -n "$ISTIO_NAMESPACE" || true
        kubectl get pods -n "$ISTIO_NAMESPACE" || true
        return 1
    fi
    echo "${ISTIO_INGRESS_SERVICE} has ready endpoint(s): ${addresses}"
}

probe_https() {
    local host
    host=$(kubectl get gateway spi-gateway -n "$ISTIO_NAMESPACE" \
        -o jsonpath='{.spec.listeners[?(@.protocol=="HTTPS")].hostname}' | awk '{print $1}')
    if [[ -z "$host" ]]; then
        echo "No HTTPS listener on spi-gateway; skipping TLS probe (ip mode)."
        return 0
    fi
    for i in $(seq 1 20); do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://${host}/" || true)
        if [[ "$code" =~ ^[0-9]+$ && "$code" != "000" ]]; then
            echo "HTTPS terminated on ${host} (HTTP ${code}) after ${i} attempts"
            return 0
        fi
        echo "attempt ${i}: no TLS yet on ${host} (code=${code}); retrying in 30s"
        sleep 30
    done
    echo "HTTPS never terminated on ${host}; dumping certificate state" >&2
    kubectl get certificates,certificaterequests,orders,challenges -A || true
    return 1
}

case "${1:-}" in
    gateway) probe_gateway ;;
    https) probe_https ;;
    -h | --help) usage ;;
    *)
        usage >&2
        exit 2
        ;;
esac
