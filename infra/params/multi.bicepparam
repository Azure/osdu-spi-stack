// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// main.bicep parameters matching `spi up --env dev1 --partition opendes
// --partition tenant1`, for manual `az deployment group create` only: the CLI
// synthesizes its own. The per-partition arrays stay index-aligned with
// dataPartitions, whose first entry is the primary partition and the only one
// with osdu-system-db. An empty oidcIssuerUrl skips the federated credentials.

using '../main.bicep'

param envName = 'dev1'
param location = 'eastus2'

param identityName = 'spi-stack-dev1-osdu-identity'
param keyVaultName = 'osdudev1'
param acrName = 'osdudev1'

// The object ID of the principal running the deployment.
param deployerPrincipalId = '00000000-0000-0000-0000-000000000000'
param deployerPrincipalType = 'User'

param dataPartitions = [
  'opendes'
  'tenant1'
]
param primaryPartition = 'opendes'

param gremlinAccountName = 'osdu-dev1-graph'
param commonStorageName = 'osdudev1common'

param cosmosSqlNames = [
  'osdu-dev1-opendes-cosmos'
  'osdu-dev1-tenant1-cosmos'
]
param serviceBusNames = [
  'osdu-dev1-opendes-bus'
  'osdu-dev1-tenant1-bus'
]
param partitionStorageNames = [
  'osdudev1opendes'
  'osdudev1tenant1'
]

param oidcIssuerUrl = ''
