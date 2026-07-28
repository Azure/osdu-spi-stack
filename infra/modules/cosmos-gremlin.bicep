// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// CosmosDB Gremlin account for the OSDU Entitlements graph.
// Single-region, Session consistency, autoscale up to 4000 RU/s.
// Local/key auth is disabled; Entitlements must use Microsoft Entra tokens.

@description('CosmosDB Gremlin account name.')
param name string

@description('Azure region.')
param location string

@description('Principal ID (object ID) of the OSDU managed identity that accesses Gremlin data.')
param principalId string

var gremlinDataContributorRoleId = '00000000-0000-0000-0000-000000000004'

resource gremlinAccount 'Microsoft.DocumentDB/databaseAccounts@2023-11-15' = {
  name: name
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
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

resource osduIdentityGremlinDataContributor 'Microsoft.DocumentDB/databaseAccounts/gremlinRoleAssignments@2024-12-01-preview' = {
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
