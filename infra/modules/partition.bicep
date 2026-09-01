// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Per-partition data plane: Cosmos SQL (osdu-db, plus osdu-system-db on the
// primary partition), Service Bus topics and subscriptions, blob storage.

@description('OSDU data partition identifier used in resource and secret names.')
param partition string

@description('Azure region where the partition resources are deployed.')
param location string

@description('Globally unique Cosmos DB SQL account name for this partition.')
param cosmosSqlName string

@description('Globally unique Service Bus namespace name for this partition.')
param serviceBusName string

@description('Globally unique storage account name for this partition.')
param storageAccountName string

@description('Whether this partition owns the shared osdu-system-db database and its secrets.')
param isPrimaryPartition bool = false

@description('Existing Key Vault that receives partition secrets; an empty string omits all secret resources.')
param keyVaultName string = ''

@description('Principal ID of the OSDU workload identity granted Cosmos SQL data-plane access.')
param principalId string

var osduDbContainers = [
  { name: 'Authority', partitionKey: '/id' }
  { name: 'EntityType', partitionKey: '/id' }
  { name: 'FileLocationEntity', partitionKey: '/id' }
  { name: 'IngestionStrategy', partitionKey: '/workflowType' }
  { name: 'LegalTag', partitionKey: '/id' }
  { name: 'MappingInfo', partitionKey: '/sourceSchemaKind' }
  { name: 'RegisterAction', partitionKey: '/dataPartitionId' }
  { name: 'RegisterDdms', partitionKey: '/dataPartitionId' }
  { name: 'RegisterSubscription', partitionKey: '/dataPartitionId' }
  { name: 'RelationshipStatus', partitionKey: '/id' }
  { name: 'ReplayStatus', partitionKey: '/id' }
  { name: 'SchemaInfo', partitionKey: '/partitionId' }
  { name: 'Source', partitionKey: '/id' }
  { name: 'StorageRecord', partitionKey: '/id' }
  { name: 'StorageSchema', partitionKey: '/kind' }
  { name: 'TenantInfo', partitionKey: '/id' }
  { name: 'UserInfo', partitionKey: '/id' }
  { name: 'Workflow', partitionKey: '/workflowId' }
  { name: 'WorkflowCustomOperatorInfo', partitionKey: '/operatorId' }
  { name: 'WorkflowCustomOperatorV2', partitionKey: '/partitionKey' }
  { name: 'WorkflowRun', partitionKey: '/partitionKey' }
  { name: 'WorkflowRunV2', partitionKey: '/partitionKey' }
  { name: 'WorkflowRunStatus', partitionKey: '/partitionKey' }
  { name: 'WorkflowV2', partitionKey: '/partitionKey' }
]

var osduSystemDbContainers = [
  { name: 'Authority', partitionKey: '/id' }
  { name: 'EntityType', partitionKey: '/id' }
  { name: 'SchemaInfo', partitionKey: '/partitionId' }
  { name: 'Source', partitionKey: '/id' }
  { name: 'WorkflowV2', partitionKey: '/partitionKey' }
]

var serviceBusTopicDefs = [
  { name: 'indexing-progress', maxSizeInMegabytes: 1024 }
  { name: 'legaltags', maxSizeInMegabytes: 1024 }
  { name: 'recordstopic', maxSizeInMegabytes: 1024 }
  { name: 'recordstopicdownstream', maxSizeInMegabytes: 1024 }
  { name: 'recordstopiceg', maxSizeInMegabytes: 1024 }
  { name: 'schemachangedtopic', maxSizeInMegabytes: 1024 }
  { name: 'schemachangedtopiceg', maxSizeInMegabytes: 1024 }
  { name: 'legaltagschangedtopiceg', maxSizeInMegabytes: 1024 }
  { name: 'statuschangedtopic', maxSizeInMegabytes: 5120 }
  { name: 'statuschangedtopiceg', maxSizeInMegabytes: 1024 }
  { name: 'recordstopic-v2', maxSizeInMegabytes: 1024 }
  { name: 'reindextopic', maxSizeInMegabytes: 1024 }
  { name: 'entitlements-changed', maxSizeInMegabytes: 1024 }
  { name: 'replaytopic', maxSizeInMegabytes: 1024 }
]

// Bicep cannot nest for-expressions in a variable, so the pairs are explicit.
// entitlements-changed has no subscription.
var serviceBusSubscriptionDefs = [
  { topicName: 'indexing-progress', subName: 'indexing-progresssubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'legaltags', subName: 'legaltagssubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'recordstopic', subName: 'recordstopicsubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'recordstopic', subName: 'wkssubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'recordstopicdownstream', subName: 'downstreamsub', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'recordstopiceg', subName: 'eg_sb_wkssubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'schemachangedtopic', subName: 'schemachangedtopicsubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'schemachangedtopiceg', subName: 'eg_sb_schemasubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'legaltagschangedtopiceg', subName: 'eg_sb_legaltagssubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'statuschangedtopic', subName: 'statuschangedtopicsubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'statuschangedtopiceg', subName: 'eg_sb_statussubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'recordstopic-v2', subName: 'recordstopic-v2-subscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'reindextopic', subName: 'reindextopicsubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
  { topicName: 'replaytopic', subName: 'replaytopicsubscription', maxDeliveryCount: 5, lockDuration: 'PT5M' }
]

var partitionStorageContainerNames = [
  'legal-service-azure-configuration'
  'osdu-wks-mappings'
  'wdms-osdu'
  'file-staging-area'
  'file-persistent-area'
]

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2023-11-15' = {
  name: cosmosSqlName
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
  }
}

resource osduDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2023-11-15' = {
  parent: cosmosAccount
  name: 'osdu-db'
  properties: {
    resource: {
      id: 'osdu-db'
    }
    options: {
      autoscaleSettings: {
        maxThroughput: 4000
      }
    }
  }
}

resource osduDbContainerResources 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-11-15' = [for container in osduDbContainers: {
  parent: osduDb
  name: container.name
  properties: {
    resource: {
      id: container.name
      partitionKey: {
        paths: [
          container.partitionKey
        ]
        kind: 'Hash'
      }
    }
  }
}]

resource osduSystemDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2023-11-15' = if (isPrimaryPartition) {
  parent: cosmosAccount
  name: 'osdu-system-db'
  properties: {
    resource: {
      id: 'osdu-system-db'
    }
    options: {
      autoscaleSettings: {
        maxThroughput: 4000
      }
    }
  }
}

resource osduSystemDbContainerResources 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-11-15' = [for container in osduSystemDbContainers: if (isPrimaryPartition) {
  parent: osduSystemDb
  name: container.name
  properties: {
    resource: {
      id: container.name
      partitionKey: {
        paths: [
          container.partitionKey
        ]
        kind: 'Hash'
      }
    }
  }
}]

// Cosmos SQL data-plane RBAC is separate from Azure RBAC and invisible to
// `az role assignment`; without it OSDU data calls fail with 403.
var sqlDataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource osduIdentitySqlDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2023-11-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, principalId, sqlDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${sqlDataContributorRoleId}'
    principalId: principalId
    scope: cosmosAccount.id
  }
}

resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: serviceBusName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  // Runtime access is Workload Identity through the Data Sender and Receiver
  // assignments in rbac.bicep.
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
  }
}

resource serviceBusTopics 'Microsoft.ServiceBus/namespaces/topics@2022-10-01-preview' = [for topic in serviceBusTopicDefs: {
  parent: serviceBusNamespace
  name: topic.name
  properties: {
    maxSizeInMegabytes: topic.maxSizeInMegabytes
  }
}]

resource serviceBusSubscriptions 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2022-10-01-preview' = [for sub in serviceBusSubscriptionDefs: {
  name: '${serviceBusName}/${sub.topicName}/${sub.subName}'
  properties: {
    maxDeliveryCount: sub.maxDeliveryCount
    lockDuration: sub.lockDuration
  }
  dependsOn: [
    serviceBusTopics
  ]
}]

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

// Record ingestion writes to a container named after the partition and returns
// 404 unless it exists.
resource storageContainerResources 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = [for containerName in union(partitionStorageContainerNames, [partition]): {
  parent: blobService
  name: containerName
}]

// The secrets live in this module so their property references carry an
// implicit dependency; a parent-scope reference could hit ResourceNotFound.
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = if (!empty(keyVaultName)) {
  name: keyVaultName
}

resource cosmosPrimaryKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName)) {
  name: '${partition}-cosmos-primary-key'
  parent: keyVault
  properties: {
    value: 'DISABLED'
  }
}

// partition-init publishes the blob endpoint in the partition record.
resource storageAccountBlobEndpointSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName)) {
  name: '${partition}-storage-account-blob-endpoint'
  parent: keyVault
  properties: {
    value: storageAccount.properties.primaryEndpoints.blob
  }
}

// The key and connection secrets hold the literal "DISABLED" only to satisfy
// the partition-record schema; a service that reads one fails.
resource cosmosConnectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName)) {
  name: '${partition}-cosmos-connection'
  parent: keyVault
  properties: {
    value: 'DISABLED'
  }
}

resource serviceBusConnectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName)) {
  name: '${partition}-sb-connection'
  parent: keyVault
  properties: {
    value: 'DISABLED'
  }
}

resource storageAccountKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName)) {
  name: '${partition}-storage-account-key'
  parent: keyVault
  properties: {
    value: 'DISABLED'
  }
}

// System services read the system-* secrets at startup; the system database
// belongs to the primary partition, so only it writes them.
resource systemCosmosEndpointSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName) && isPrimaryPartition) {
  name: 'system-cosmos-endpoint'
  parent: keyVault
  properties: {
    value: cosmosAccount.properties.documentEndpoint
  }
}

resource systemCosmosPrimaryKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName) && isPrimaryPartition) {
  name: 'system-cosmos-primary-key'
  parent: keyVault
  properties: {
    value: 'DISABLED'
  }
}

resource systemCosmosConnectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName) && isPrimaryPartition) {
  name: 'system-cosmos-connection'
  parent: keyVault
  properties: {
    value: 'DISABLED'
  }
}

@description('Data partition identifier represented by these resources.')
output partition string = partition

@description('Azure resource ID of the partition Cosmos DB SQL account.')
output cosmosAccountId string = cosmosAccount.id

@description('Document endpoint for the partition Cosmos DB SQL account.')
output cosmosEndpoint string = cosmosAccount.properties.documentEndpoint

@description('Azure resource ID of the partition Service Bus namespace.')
output serviceBusId string = serviceBusNamespace.id

@description('Azure resource ID of the partition storage account.')
output storageId string = storageAccount.id
