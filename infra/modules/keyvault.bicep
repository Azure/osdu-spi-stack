// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// The CLI recovers a soft-deleted vault before this module runs; ARM cannot
// branch on that lookup.

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
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
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
