// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Federates the OSDU workload identity to workload-identity-sa in each
// configured namespace, and creates the environment's deploy identity, which
// carries no federated credential until spi onboard trusts a repository.

@description('Resource name for the OSDU workload identity.')
param name string

@description('Resource name for the deploy identity fork CI federates to.')
param deployIdentityName string

@description('Azure region where the managed identity is deployed.')
param location string

@description('OIDC issuer URL of the AKS cluster; use an empty string only to omit federation.')
param oidcIssuerUrl string

@description('Kubernetes namespaces whose workload-identity-sa service account binds to this identity.')
param federatedNamespaces array = [
  'default'
  'osdu-core'
  'airflow'
  'osdu-system'
  'osdu-auth'
  'osdu-reference'
  'osdu'
  'platform'
]

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
}

resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: deployIdentityName
  location: location
}

// The Managed Identity RP rejects concurrent federated credential writes on
// one identity (ConcurrentFederatedIdentityCredentialsWritesForSingleManagedIdentity).
@batchSize(1)
resource federatedCredentials 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = [for ns in federatedNamespaces: if (!empty(oidcIssuerUrl)) {
  parent: identity
  name: 'federated-ns-${ns}'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${ns}:workload-identity-sa'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}]

@description('Azure resource ID of the OSDU workload identity.')
output resourceId string = identity.id

@description('Client ID used by workload identity service account annotations.')
output clientId string = identity.properties.clientId

@description('Principal ID used for Azure data-plane role assignments.')
output principalId string = identity.properties.principalId

@description('Azure resource ID of the deploy identity.')
output deployIdentityResourceId string = deployIdentity.id

@description('Client ID a trusted repository holds as AZURE_CLIENT_ID.')
output deployIdentityClientId string = deployIdentity.properties.clientId

@description('Principal ID bound as the User subject of the fork RoleBindings.')
output deployIdentityPrincipalId string = deployIdentity.properties.principalId
