// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Default parameters for infra/main.bicep -- BASE environment (envName = '').
//
// The CLI (src/spi/azure_infra.py) synthesizes an ARM parameters JSON from
// the Config object at deploy time, so this file is only used by humans
// running `az deployment group create` manually. For a realistic named
// environment, see dev1.bicepparam alongside this file.
//
// The double-dash names (osdu--graph, osdu--opendes-cosmos, osdu--opendes-bus)
// are intentional: they come from templates like f"osdu-{env}-graph" when
// envName is empty. They match what Config.from_env('') produces and are
// valid Azure resource names.
//
// oidcIssuerUrl is empty here; leave it empty to run main.bicep without the
// AKS cluster present (federated credentials are skipped). A real deploy
// through the CLI populates it from infra/aks.bicep's oidcIssuerUrl output.

using '../main.bicep'

param envName = ''
param location = 'eastus2'

// Names derived by Config.from_env('') and the _*_name helpers in azure_infra.py
param identityName = 'spi-stack-osdu-identity'
param keyVaultName = 'osduspistack'
param acrName = 'osduspistack'

// Replace with the object ID of the principal running a manual deployment.
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
