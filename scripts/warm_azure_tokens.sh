#!/usr/bin/env bash
# Exchange the current az login for every audience `spi up` reaches, while the
# one-shot GitHub OIDC assertion behind that login is still valid.
set -euo pipefail

# ARM, az ad, the AKS AAD server app (kubelogin), Key Vault.
for resource in \
  https://management.azure.com \
  https://graph.microsoft.com \
  6dae42f8-4368-4678-94ff-3960e28e3630 \
  https://vault.azure.net; do
  az account get-access-token --resource "$resource" -o none
done
