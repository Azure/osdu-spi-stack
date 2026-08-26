// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//

@description('Globally unique name of the Cosmos DB Gremlin account used by Entitlements.')
param name string

@description('Azure region where the Gremlin account is deployed.')
param location string

@description('Principal ID of the OSDU workload identity granted Gremlin data-plane access.')
param principalId string

var gremlinDataContributorRoleId = '00000000-0000-0000-0000-000000000004'

resource gremlinAccount 'Microsoft.DocumentDB/databaseAccounts@2023-11-15' = {
  name: name
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    // Entitlements uses Entra-backed Workload Identity for data-plane access.
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

// Cosmos Gremlin RBAC is data-plane native, not an Azure role assignment.
// This role is required because local authentication is disabled.
resource osduIdentityGremlinDataContributor 'Microsoft.DocumentDB/databaseAccounts/gremlinRoleAssignments@2024-12-01-preview' = {
  parent: gremlinAccount
  name: guid(gremlinAccount.id, principalId, gremlinDataContributorRoleId)
  properties: {
    roleDefinitionId: '${gremlinAccount.id}/gremlinRoleDefinitions/${gremlinDataContributorRoleId}'
    principalId: principalId
    scope: gremlinAccount.id
  }
}

@description('Azure resource ID of the Cosmos DB Gremlin account.')
output resourceId string = gremlinAccount.id

@description('Document endpoint used by Entitlements to connect to the Gremlin account.')
output documentEndpoint string = gremlinAccount.properties.documentEndpoint
