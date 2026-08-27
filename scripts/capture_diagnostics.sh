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

# Capture diagnostics from a live cluster into diagnostics/<label>/.
#
# Used by CI smoke-test jobs to collect state at the end of a run so a
# human reviewing a failed pipeline can distinguish "real failure" from
# "slow but fine" from "broken component X". Every command is best-effort
# and tolerates missing kubectl context or tool absence so this script
# never fails the calling job.
#
# Usage: bash scripts/capture_diagnostics.sh <label>

set -u

LABEL="${1:-unlabeled-$(date +%s)}"
OUT="diagnostics/$LABEL"
mkdir -p "$OUT"

echo "Capturing diagnostics to $OUT"

{
    if command -v spi >/dev/null 2>&1; then
        spi status 2>&1 || echo "(spi status failed)"
    elif command -v uv >/dev/null 2>&1; then
        uv run spi status 2>&1 || echo "(spi status failed)"
    else
        echo "(spi and uv not available)"
    fi
} > "$OUT/spi-status.txt"

if command -v kubectl >/dev/null 2>&1; then
    kubectl get kustomizations -A -o yaml > "$OUT/kustomizations.yaml" 2>&1 || true
    kubectl get helmreleases -A -o yaml > "$OUT/helmreleases.yaml" 2>&1 || true
    kubectl get events -A --sort-by=.lastTimestamp 2>&1 | tail -200 > "$OUT/events.txt" || true
    kubectl get pods -A -o wide > "$OUT/pods.txt" 2>&1 || true
    kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded \
        -o wide > "$OUT/pods-not-healthy.txt" 2>&1 || true
    kubectl get nodes -o wide > "$OUT/nodes.txt" 2>&1 || true
else
    echo "(kubectl not available)" > "$OUT/no-kubectl"
fi

echo "Diagnostics written:"
ls -la "$OUT"
