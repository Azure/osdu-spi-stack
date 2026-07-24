// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// CosmosDB Gremlin account for the OSDU Entitlements graph.
// Single-region, Session consistency, autoscale up to 4000 RU/s.
//
// Also writes ``graph-db-primary-key`` to Key Vault via ``listKeys()`` on
// the same-module account. This must happen in the same module as the
// account itself: ``existing`` references from the parent cannot carry
// the implicit dependency on the creating module, so an ``existing`` +
// ``listKeys()`` pattern at the parent level fails with ResourceNotFound
// when ARM evaluates the property.

@description('CosmosDB Gremlin account name.')
param name string

@description('Azure region.')
param location string

@description('Key Vault name that receives the primary key. Empty string skips the secret write.')
param keyVaultName string = ''

@description('Principal ID (object ID) of the OSDU managed identity that accesses Gremlin data. Empty string skips the data-plane role assignment.')
param principalId string = ''

resource gremlinAccount 'Microsoft.DocumentDB/databaseAccounts@2023-11-15' = {
  name: name
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableGremlin'
      }
    ]
  }
}

resource gremlinDatabase 'Microsoft.DocumentDB/databaseAccounts/gremlinDatabases@2023-11-15' = {
  parent: gremlinAccount
  name: 'osdu-graph'
  properties: {
    resource: {
      id: 'osdu-graph'
    }
  }
}

resource entitlementsGraph 'Microsoft.DocumentDB/databaseAccounts/gremlinDatabases/graphs@2023-11-15' = {
  parent: gremlinDatabase
  name: 'Entitlements'
  properties: {
    resource: {
      id: 'Entitlements'
      partitionKey: {
        paths: [
          '/dataPartitionId'
        ]
        kind: 'Hash'
      }
    }
    options: {
      autoscaleSettings: {
        maxThroughput: 4000
      }
    }
  }
}

// Key Vault secret -- written only if a KV name was provided. Scoped to
// the same module so ``listKeys()`` has an implicit dependency on the
// account resource above.

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = if (!empty(keyVaultName)) {
  name: keyVaultName
}

resource primaryKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName)) {
  name: 'graph-db-primary-key'
  parent: keyVault
  properties: {
    value: gremlinAccount.listKeys().primaryMasterKey
  }
}

// Gremlin data-plane role for the OSDU managed identity. The target tenant's
// Cosmos modify policy flips disableLocalAuth at creation time, making the
// primary key above unusable there; this role is the Entra-token path the
// MSI-aware entitlements image uses instead. Additive and harmless where
// key auth still works. Like sqlRoleAssignments, this is Cosmos-native RBAC
// (not `az role assignment`) with ~5-15 minute propagation.
var gremlinDataContributorRoleId = '00000000-0000-0000-0000-000000000004'

resource osduIdentityGremlinDataContributor 'Microsoft.DocumentDB/databaseAccounts/gremlinRoleAssignments@2024-12-01-preview' = if (!empty(principalId)) {
  parent: gremlinAccount
  name: guid(gremlinAccount.id, principalId, gremlinDataContributorRoleId)
  properties: {
    roleDefinitionId: '${gremlinAccount.id}/gremlinRoleDefinitions/${gremlinDataContributorRoleId}'
    principalId: principalId
    scope: gremlinAccount.id
  }
}

output resourceId string = gremlinAccount.id
output documentEndpoint string = gremlinAccount.properties.documentEndpoint
