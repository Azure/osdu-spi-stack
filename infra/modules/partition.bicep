// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Per-partition data plane: CosmosDB SQL account with osdu-db (and
// optionally osdu-system-db on the primary partition), Service Bus
// namespace with topics and subscriptions, storage account with blob
// containers.
//
// Container, topic, subscription, and container-name definitions are
// ported literally from azure_infra.py (OSDU_DB_CONTAINERS,
// OSDU_SYSTEM_DB_CONTAINERS, SERVICEBUS_TOPICS, PARTITION_STORAGE_CONTAINERS).

@description('Data partition name, e.g. "opendes".')
param partition string

@description('Azure region.')
param location string

@description('CosmosDB SQL account name for this partition.')
param cosmosSqlName string

@description('Service Bus namespace name for this partition.')
param serviceBusName string

@description('Storage account name for this partition.')
param storageAccountName string

@description('True only for the primary partition; hosts osdu-system-db.')
param isPrimaryPartition bool = false

@description('Key Vault name that receives the Cosmos primary key. Empty string skips the secret write.')
param keyVaultName string = ''

@description('Principal ID (object ID) of the OSDU managed identity that accesses Cosmos SQL data. Empty string skips the SQL data-plane role assignment.')
param principalId string = ''

// ──────────────────────────────────────────────────────────
// Data definitions (ported from azure_infra.py)
// ──────────────────────────────────────────────────────────

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

// Flat subscription list. Bicep for-loops cannot nest inside flatten() at
// var-declaration time, so topic/sub pairs are enumerated explicitly.
// "entitlements-changed" has no subscriptions and is intentionally omitted.
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

// ──────────────────────────────────────────────────────────
// CosmosDB SQL
// ──────────────────────────────────────────────────────────

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2023-11-15' = {
  name: cosmosSqlName
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

// SQL data-plane role assignment for the OSDU managed identity. This grants the
// "Cosmos DB Built-in Data Contributor" role (id ...0002) so that services with
// AZURE_MSI_ISENABLED reach Cosmos with their Workload Identity; without it, their
// data-plane calls fail with 403 "does not have required RBAC permissions". It is
// the SQL equivalent of the Gremlin role assignment in cosmos-gremlin.bicep.
//
// Local (key) auth is intentionally left ENABLED on this SQL account, unlike the
// Gremlin account. The partition service enables Workload Identity for Key Vault
// only (AZURE_PAAS_WORKLOADIDENTITY_ISENABLED, not AZURE_MSI_ISENABLED) and still
// connects to Cosmos with the primary key it reads from Key Vault, so the
// "<partition>-cosmos-primary-key" secret written below is required. Disabling
// local auth here (the ADR-033 end state) is a follow-up gated on the partition
// service supporting the Cosmos data-plane MSI path.
var sqlDataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource osduIdentitySqlDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2023-11-15' = if (!empty(principalId)) {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, principalId, sqlDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${sqlDataContributorRoleId}'
    principalId: principalId
    scope: cosmosAccount.id
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

// ──────────────────────────────────────────────────────────
// Service Bus
// ──────────────────────────────────────────────────────────

resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: serviceBusName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
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

// ──────────────────────────────────────────────────────────
// Storage
// ──────────────────────────────────────────────────────────

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

// The OSDU storage-azure provider writes record blobs to a container named
// after the data partition id (e.g. "opendes"). core-lib-azure's BlobStore
// does NOT auto-create it, so record ingestion fails with a 404
// ContainerNotFound unless the container is pre-created alongside the fixed
// per-service containers above.
resource storageContainerResources 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = [for containerName in union(partitionStorageContainerNames, [partition]): {
  parent: blobService
  name: containerName
}]

// ──────────────────────────────────────────────────────────
// Key Vault secret (same-module listKeys() for natural dependency)
// ──────────────────────────────────────────────────────────
//
// Written inside this module so ``listKeys()`` has an implicit dependency
// on the ``cosmosAccount`` resource above. An ``existing`` reference at
// the parent scope does NOT carry a dependency on the creating module,
// so attempting ``listKeys()`` there fails with ResourceNotFound.

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = if (!empty(keyVaultName)) {
  name: keyVaultName
}

resource cosmosPrimaryKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName)) {
  name: '${partition}-cosmos-primary-key'
  parent: keyVault
  properties: {
    value: cosmosAccount.listKeys().primaryMasterKey
  }
}

// Blob endpoint is needed by partition-init so the partition record can point
// services at the partition's blob storage without every service recomputing it.
resource storageAccountBlobEndpointSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName)) {
  name: '${partition}-storage-account-blob-endpoint'
  parent: keyVault
  properties: {
    value: storageAccount.properties.primaryEndpoints.blob
  }
}

// cosmos-connection, sb-connection, and storage-account-key hold the literal "DISABLED".
// The partition record references these secret names; Workload Identity
// supplies the real credentials at runtime for every code path that supports
// it, and writing "DISABLED" keeps the schema satisfied without exposing
// real credentials.
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

// System-partition Cosmos secrets. OSDU "system" services (schema, workflow,
// ...) resolve the shared catalog from KV secrets prefixed ``system-`` rather
// than ``{partition}-``. The osdu-system-db SQL database lives in the primary
// partition's Cosmos account (see osduSystemDb above), so these point at the
// same account. Without them, system services fail at startup with
// "Failed to retrieve system-cosmos-endpoint. Not found." ->
// "system-cosmos-endpoint cannot be null" -> "Error creating Cosmos Client".
// Only the primary partition owns the system DB, so guard on isPrimaryPartition.
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
    value: cosmosAccount.listKeys().primaryMasterKey
  }
}

resource systemCosmosConnectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(keyVaultName) && isPrimaryPartition) {
  name: 'system-cosmos-connection'
  parent: keyVault
  properties: {
    value: 'DISABLED'
  }
}

// ──────────────────────────────────────────────────────────
// Outputs
// ──────────────────────────────────────────────────────────

output partition string = partition
output cosmosAccountId string = cosmosAccount.id
output cosmosEndpoint string = cosmosAccount.properties.documentEndpoint
output serviceBusId string = serviceBusNamespace.id
output storageId string = storageAccount.id
