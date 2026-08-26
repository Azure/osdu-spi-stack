// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Federates a user-assigned identity to the in-cluster ExternalDNS service
// account. The sibling role module grants zone-scoped DNS access.

@description('Resource name for the ExternalDNS managed identity.')
param name string

@description('Azure region where the managed identity is deployed.')
param location string

@description('OIDC issuer URL of the AKS cluster; use an empty string only to omit federation.')
param oidcIssuerUrl string

@description('Kubernetes namespace containing the ExternalDNS service account.')
param federatedNamespace string = 'foundation'

@description('Kubernetes service account name that must match the ExternalDNS Helm release.')
param serviceAccountName string = 'external-dns'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
}

resource federatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (!empty(oidcIssuerUrl)) {
  parent: identity
  name: 'federated-external-dns'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${federatedNamespace}:${serviceAccountName}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

@description('Azure resource ID of the ExternalDNS managed identity.')
output resourceId string = identity.id

@description('Client ID used by the ExternalDNS service account annotation.')
output clientId string = identity.properties.clientId

@description('Principal ID used for the DNS Zone Contributor role assignment.')
output principalId string = identity.properties.principalId
