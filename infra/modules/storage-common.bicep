// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Shared storage account (common across partitions) for OSDU platform
// artifacts: Airflow logs/dags, shared reference data, partition-info table.

@description('Globally unique storage account name of 3-24 lowercase alphanumeric characters.')
param name string

@description('Azure region where the shared storage account is deployed.')
param location string

var containerNames = [
  'system'
  'azure-webjobs-hosts'
  'azure-webjobs-eventhub'
  'airflow-logs'
  'airflow-dags'
  'share-unit'
  'share-crs'
  'share-crs-conversion'
]

var tableNames = [
  'partitionInfo'
]

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: name
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    // Public blob and shared-key access are disabled; workloads use Entra RBAC.
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

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = [for containerName in containerNames: {
  parent: blobService
  name: containerName
}]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource tables 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = [for tableName in tableNames: {
  parent: tableService
  name: tableName
}]

@description('Azure resource ID of the storage account shared across partitions.')
output resourceId string = storageAccount.id
