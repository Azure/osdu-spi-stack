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

# Build every kustomization.yaml under software/ with kustomize and validate
# the output with kubeconform, collecting every failure before exiting.
# Requires kustomize and kubeconform.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOFTWARE_DIR="$REPO_ROOT/software"

# Vendored CRD schemas under schemas/ take precedence over the datreeio catalog.
LOCAL_CRD_SCHEMA_LOCATION="$REPO_ROOT/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
CRD_SCHEMA_LOCATION='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

for tool in kustomize kubeconform; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: $tool is required but not on PATH" >&2
        exit 2
    fi
done

# Unresolved Flux postBuild ${VAR} references trip kubeconform's strict mode.
sub_vars() {
    sed -E 's/\$\{[A-Z_][A-Z0-9_]*\}/placeholder/g'
}

failures=()
count=0

while IFS= read -r kustomization; do
    dir="$(dirname "$kustomization")"
    rel="${dir#"$REPO_ROOT/"}"
    count=$((count + 1))
    echo ">> $rel"

    if ! build_output="$(kustomize build "$dir" 2>&1)"; then
        echo "   FAIL: kustomize build" >&2
        echo "$build_output" | sed 's/^/     /' >&2
        failures+=("$rel (kustomize)")
        continue
    fi

    if ! conform_output="$(printf '%s\n' "$build_output" | sub_vars | kubeconform \
        -strict \
        -ignore-missing-schemas \
        -schema-location default \
        -schema-location "$LOCAL_CRD_SCHEMA_LOCATION" \
        -schema-location "$CRD_SCHEMA_LOCATION" \
        -summary \
        - 2>&1)"; then
        echo "   FAIL: kubeconform" >&2
        echo "$conform_output" | sed 's/^/     /' >&2
        failures+=("$rel (kubeconform)")
        continue
    fi
done < <(find "$SOFTWARE_DIR" -name 'kustomization.yaml' -type f | sort)

if (( count == 0 )); then
    echo "error: no kustomization.yaml files found under $SOFTWARE_DIR" >&2
    exit 2
fi

echo ""
if (( ${#failures[@]} > 0 )); then
    echo "Manifest validation failed for ${#failures[@]} of $count kustomization(s):" >&2
    printf '  - %s\n' "${failures[@]}" >&2
    exit 1
fi

echo "All $count kustomizations built and validated successfully."
