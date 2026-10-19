// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.

@description('Resource name for the Reservoir workload identity.')
param name string

@description('Azure region where the managed identity is deployed.')
param location string

@description('OIDC issuer URL of the AKS cluster.')
param oidcIssuerUrl string

@description('Existing Key Vault that stores Reservoir connection metadata.')
param keyVaultName string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
}

resource federatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (!empty(oidcIssuerUrl)) {
  parent: identity
  name: 'federated-osdu-open-etp'
  properties: {
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:osdu:open-etp-sa'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource keyVaultSecretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, identity.id, '4633458b-17de-408a-b874-0445c86b69e6')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('Client ID used by the open-etp-sa annotation.')
output clientId string = identity.properties.clientId

@description('Principal ID configured as each Reservoir PostgreSQL administrator.')
output principalId string = identity.properties.principalId
