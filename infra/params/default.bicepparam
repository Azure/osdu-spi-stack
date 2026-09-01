// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// main.bicep parameters for the base environment (envName ''), matching what
// Config.from_env('') produces; the double-dash names are that template with
// an empty env. For manual `az deployment group create` only: the CLI
// synthesizes its own parameters. An empty oidcIssuerUrl skips the federated
// credentials so the template deploys without an AKS cluster.

using '../main.bicep'

param envName = ''
param location = 'eastus2'

param identityName = 'spi-stack-osdu-identity'
param keyVaultName = 'osduspistack'
param acrName = 'osduspistack'

// The object ID of the principal running the deployment.
param deployerPrincipalId = '00000000-0000-0000-0000-000000000000'
param deployerPrincipalType = 'User'

param dataPartitions = [
  'opendes'
]
param primaryPartition = 'opendes'

param gremlinAccountName = 'osdu--graph'
param commonStorageName = 'osducommon'

param cosmosSqlNames = [
  'osdu--opendes-cosmos'
]
param serviceBusNames = [
  'osdu--opendes-bus'
]
param partitionStorageNames = [
  'osduopendes'
]

param oidcIssuerUrl = ''
