// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Example parameters for infra/main.bicep -- "dev1" environment.
//
// Matches the values produced by `uv run spi up --env dev1`. Provided for
// humans running `az deployment group create` manually; the CLI does not
// read this file (it synthesizes parameters from Config at deploy time).
//
// To try it:
//   az group create --name spi-stack-dev1 --location eastus2
//   az deployment group create \
//     --resource-group spi-stack-dev1 \
//     --template-file infra/main.bicep \
//     --parameters infra/params/dev1.bicepparam
//
// oidcIssuerUrl is empty so federated credentials are skipped; fill it in
// (`az aks show -g spi-stack-dev1 -n spi-stack-dev1 --query oidcIssuerProfile.issuerUrl -o tsv`)
// if an AKS cluster already exists and you want full RBAC wiring.

using '../main.bicep'

param envName = 'dev1'
param location = 'eastus2'

// Names derived by Config.from_env('dev1') and the _*_name helpers in azure_infra.py
param identityName = 'spi-stack-dev1-osdu-identity'
param keyVaultName = 'osdudev1'
param acrName = 'osdudev1'

// Replace with the object ID of the principal running a manual deployment.
param deployerPrincipalId = '00000000-0000-0000-0000-000000000000'
param deployerPrincipalType = 'User'

param dataPartitions = [
  'opendes'
]
param primaryPartition = 'opendes'

param gremlinAccountName = 'osdu-dev1-graph'
param commonStorageName = 'osdudev1common'

param cosmosSqlNames = [
  'osdu-dev1-opendes-cosmos'
]
param serviceBusNames = [
  'osdu-dev1-opendes-bus'
]
param partitionStorageNames = [
  'osdudev1opendes'
]

param oidcIssuerUrl = ''
