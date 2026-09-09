// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Federates the OSDU workload identity to workload-identity-sa in each
// configured namespace, and creates the environment's deploy identity and
// no-access identity. Each of those trusts one cluster ServiceAccount, so a
// developer with cluster access can mint the same app-only token fork CI
// mints through GitHub federation (spi token); repositories are trusted later
// by spi onboard. The no-access identity never receives a role assignment or
// an entitlements group; fork CI uses it to prove 403 paths.

@description('Resource name for the OSDU workload identity.')
param name string

@description('Resource name for the deploy identity fork CI federates to.')
param deployIdentityName string

@description('Resource name for the no-access identity fork CI federates to for 403 tests.')
param noAccessIdentityName string

@description('Azure region where the managed identity is deployed.')
param location string

@description('OIDC issuer URL of the AKS cluster; use an empty string only to omit federation.')
param oidcIssuerUrl string

@description('Namespace of the ServiceAccounts the deploy and no-access identities trust.')
param testerNamespace string = 'spi-test'

@description('ServiceAccount the deploy identity trusts; spi token mints through it.')
param deployerServiceAccountName string = 'spi-deployer'

@description('ServiceAccount the no-access identity trusts; spi token --no-access mints through it.')
param noAccessServiceAccountName string = 'spi-no-access'

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

resource noAccessIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: noAccessIdentityName
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

// The onboard roster projection ignores these two credentials by issuer, but
// they count against the twenty-credential cap on each identity.
resource deployerClusterCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (!empty(oidcIssuerUrl)) {
  parent: deployIdentity
  name: 'cluster-${testerNamespace}'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${testerNamespace}:${deployerServiceAccountName}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

resource noAccessClusterCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (!empty(oidcIssuerUrl)) {
  parent: noAccessIdentity
  name: 'cluster-${testerNamespace}'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${testerNamespace}:${noAccessServiceAccountName}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

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

@description('Azure resource ID of the no-access identity.')
output noAccessIdentityResourceId string = noAccessIdentity.id

@description('Client ID a trusted repository reads from spi info as no_access_client_id.')
output noAccessIdentityClientId string = noAccessIdentity.properties.clientId

@description('Principal ID of the no-access identity; bound nowhere.')
output noAccessIdentityPrincipalId string = noAccessIdentity.properties.principalId
