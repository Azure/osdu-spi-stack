// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// main.bicep parameters matching `spi up --env dev1`, for manual
// `az deployment group create --resource-group spi-stack-dev1` only: the CLI
// synthesizes its own. An empty oidcIssuerUrl skips the federated credentials;
// set it from `az aks show --query oidcIssuerProfile.issuerUrl` when a cluster exists.

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
