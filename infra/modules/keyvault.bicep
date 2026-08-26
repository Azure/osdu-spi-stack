// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Soft-deleted vault recovery requires a live lookup and is performed by the
// CLI before this module runs.

@description('Globally unique Key Vault name of 3-24 characters, using alphanumerics and nonconsecutive hyphens; must start with a letter and end with an alphanumeric character.')
param name string

@description('Azure region where the Key Vault is deployed.')
param location string

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenant().tenantId
    // Data-plane access uses Azure RBAC rather than Key Vault access policies.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    // VM, disk encryption, and ARM template integrations cannot read secrets.
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: 'Enabled'
  }
}

@description('Azure resource ID of the Key Vault.')
output resourceId string = keyVault.id

@description('Vault URI used by workloads to retrieve secrets.')
output uri string = keyVault.properties.vaultUri
