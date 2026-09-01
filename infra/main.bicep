// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Azure PaaS for the OSDU SPI Stack, deployed between aks.bicep (whose OIDC
// issuer binds the workload identity) and flux.bicep. Local authentication is
// off on Cosmos DB, Service Bus and Storage; the key and connection secrets
// hold "DISABLED" and services use Workload Identity. The CLI supplies the
// top-level resource names; child names are part of the OSDU service contract.

targetScope = 'resourceGroup'

@description('Environment suffix recorded in deployment inputs; use an empty string for the base environment.')
#disable-next-line no-unused-params
param envName string = ''

@description('Azure region where the PaaS resources are deployed.')
param location string = 'westus3'

@description('Resource name for the OSDU workload identity.')
param identityName string

@description('Globally unique name of the Key Vault that stores OSDU configuration secrets.')
param keyVaultName string

@description('Globally unique name of the registry used for custom OSDU images.')
param acrName string

@description('Ordered data partition identifiers; per-partition resource-name arrays must use the same order.')
param dataPartitions array = [
  'opendes'
]

@description('Partition that owns osdu-system-db; must be one of dataPartitions.')
param primaryPartition string

@description('Globally unique name of the shared Cosmos DB Gremlin account used by Entitlements.')
param gremlinAccountName string

@description('Globally unique name of the storage account shared across partitions.')
param commonStorageName string

@description('Ordered Cosmos DB SQL account names, with one entry per dataPartitions item.')
param cosmosSqlNames array

@description('Ordered Service Bus namespace names, with one entry per dataPartitions item.')
param serviceBusNames array

@description('Ordered storage account names, with one entry per dataPartitions item.')
param partitionStorageNames array

@description('OIDC issuer URL of the AKS cluster; use an empty string only to omit federated credentials.')
param oidcIssuerUrl string = ''

@description('Resource name for the ExternalDNS identity; required when dnsZoneName is set.')
param externalDnsIdentityName string = ''

@description('Azure DNS zone managed by ExternalDNS; an empty string omits its identity and role assignment.')
param dnsZoneName string = ''

@description('Resource group containing the Azure DNS zone; required when dnsZoneName is set.')
param dnsZoneResourceGroup string = ''

@description('Object ID granted Key Vault Secrets Officer for writing runtime secrets during bootstrap.')
param deployerPrincipalId string

@description('Entra principal type for deployerPrincipalId; human deployers must use User.')
@allowed([
  'User'
  'ServicePrincipal'
])
param deployerPrincipalType string = 'ServicePrincipal'

@description('Object ID of the AKS kubelet identity; an empty string omits its AcrPull grant.')
param kubeletIdentityObjectId string = ''

@description('Whether to deploy workspace-based Application Insights and Log Analytics.')
param enableApplicationInsights bool = false

@description('Resource name for Application Insights; required when enableApplicationInsights is true.')
param appInsightsName string = ''

@description('Resource name for the backing Log Analytics workspace; required when enableApplicationInsights is true.')
param logAnalyticsName string = ''

module keyvaultModule 'modules/keyvault.bicep' = {
  name: 'spi-keyvault'
  params: {
    name: keyVaultName
    location: location
  }
}

module acrModule 'modules/acr.bicep' = {
  name: 'spi-acr'
  params: {
    name: acrName
    location: location
  }
}

module identityModule 'modules/identity.bicep' = {
  name: 'spi-identity'
  params: {
    name: identityName
    location: location
    oidcIssuerUrl: oidcIssuerUrl
  }
}

module gremlinModule 'modules/cosmos-gremlin.bicep' = {
  name: 'spi-gremlin'
  params: {
    name: gremlinAccountName
    location: location
    principalId: identityModule.outputs.principalId
  }
}

module storageCommonModule 'modules/storage-common.bicep' = {
  name: 'spi-storage-common'
  params: {
    name: commonStorageName
    location: location
  }
}

module partitionModules 'modules/partition.bicep' = [for (p, i) in dataPartitions: {
  name: 'spi-partition-${p}'
  params: {
    partition: p
    location: location
    cosmosSqlName: cosmosSqlNames[i]
    serviceBusName: serviceBusNames[i]
    storageAccountName: partitionStorageNames[i]
    isPrimaryPartition: p == primaryPartition
    keyVaultName: keyVaultName
    principalId: identityModule.outputs.principalId
  }
  dependsOn: [
    keyvaultModule
  ]
}]

module rbacModule 'modules/rbac.bicep' = {
  name: 'spi-rbac'
  params: {
    principalId: identityModule.outputs.principalId
    deployerPrincipalId: deployerPrincipalId
    deployerPrincipalType: deployerPrincipalType
    kubeletIdentityObjectId: kubeletIdentityObjectId
    keyVaultName: keyVaultName
    acrName: acrName
    commonStorageName: commonStorageName
    partitionStorageNames: partitionStorageNames
    serviceBusNames: serviceBusNames
  }
  dependsOn: [
    keyvaultModule
    acrModule
    storageCommonModule
    partitionModules
  ]
}

// DNS Zone Contributor is assigned at the zone's resource group, which may
// differ from the stack's.
module externalDnsIdentityModule 'modules/external-dns-identity.bicep' = if (!empty(dnsZoneName)) {
  name: 'spi-external-dns-identity'
  params: {
    name: externalDnsIdentityName
    location: location
    oidcIssuerUrl: oidcIssuerUrl
  }
}

module externalDnsRoleModule 'modules/external-dns-role.bicep' = if (!empty(dnsZoneName)) {
  name: 'spi-external-dns-role'
  scope: resourceGroup(dnsZoneResourceGroup)
  params: {
    dnsZoneName: dnsZoneName
    #disable-next-line BCP318
    principalId: externalDnsIdentityModule.outputs.principalId
  }
}

// Declared one by one: BCP178 forbids a for-expression over module outputs.
// Secret values are never deployment outputs.
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource secretTenantId 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'tenant-id'
  parent: keyVault
  properties: { value: tenant().tenantId }
  dependsOn: [ keyvaultModule ]
}

resource secretSubscriptionId 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'subscription-id'
  parent: keyVault
  properties: { value: subscription().subscriptionId }
  dependsOn: [ keyvaultModule ]
}

resource secretIdentityId 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'osdu-identity-id'
  parent: keyVault
  properties: { value: identityModule.outputs.clientId }
  dependsOn: [ keyvaultModule ]
}

resource secretKeyvaultUri 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'keyvault-uri'
  parent: keyVault
  properties: { value: keyvaultModule.outputs.uri }
}

resource secretSystemStorage 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'system-storage'
  parent: keyVault
  properties: { value: commonStorageName }
  dependsOn: [ keyvaultModule ]
}

resource secretAppDevSpUsername 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'app-dev-sp-username'
  parent: keyVault
  properties: { value: identityModule.outputs.clientId }
  dependsOn: [ keyvaultModule ]
}

resource secretAppDevSpPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'app-dev-sp-password'
  parent: keyVault
  properties: { value: 'DISABLED' }
  dependsOn: [ keyvaultModule ]
}

resource secretAppDevSpTenantId 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'app-dev-sp-tenant-id'
  parent: keyVault
  properties: { value: tenant().tenantId }
  dependsOn: [ keyvaultModule ]
}

resource secretAppDevSpId 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'app-dev-sp-id'
  parent: keyVault
  properties: { value: identityModule.outputs.clientId }
  dependsOn: [ keyvaultModule ]
}

resource secretGraphEndpoint 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'graph-db-endpoint'
  parent: keyVault
  properties: { value: gremlinModule.outputs.documentEndpoint }
  dependsOn: [ keyvaultModule ]
}

resource partitionStorageSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for (p, i) in dataPartitions: {
  name: '${p}-storage'
  parent: keyVault
  properties: {
    value: partitionStorageNames[i]
  }
  dependsOn: [
    keyvaultModule
  ]
}]

resource partitionCosmosEndpointSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for (p, i) in dataPartitions: {
  name: '${p}-cosmos-endpoint'
  parent: keyVault
  properties: {
    value: partitionModules[i].outputs.cosmosEndpoint
  }
  dependsOn: [
    keyvaultModule
  ]
}]

resource partitionServiceBusSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for (p, i) in dataPartitions: {
  name: '${p}-sb-namespace'
  parent: keyVault
  properties: {
    value: serviceBusNames[i]
  }
  dependsOn: [
    keyvaultModule
  ]
}]

// The bundled Java agent returns HTTP 500 without request telemetry, so the
// CLI disables the agent when telemetry is off.
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (enableApplicationInsights) {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (enableApplicationInsights) {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// Per-partition arrays stay aligned with partitionNames; Key Vault values are
// never outputs.
@description('Tenant ID used by the deployed workload identities.')
output tenantId string = tenant().tenantId

@description('Subscription ID containing the deployed PaaS resources.')
output subscriptionId string = subscription().subscriptionId

@description('Resource group containing the deployed PaaS resources.')
output resourceGroupName string = resourceGroup().name

@description('Client ID used by pods to request tokens for the OSDU workload identity.')
output identityClientId string = identityModule.outputs.clientId

@description('Principal ID used for OSDU workload identity role assignments.')
output identityPrincipalId string = identityModule.outputs.principalId

@description('Azure resource ID of the OSDU workload identity.')
output identityResourceId string = identityModule.outputs.resourceId

@description('Vault URI used by OSDU services to retrieve configuration secrets.')
output keyvaultUri string = keyvaultModule.outputs.uri

@description('Azure resource ID of the Key Vault.')
output keyvaultId string = keyvaultModule.outputs.resourceId

@description('Azure resource ID of the container registry.')
output acrId string = acrModule.outputs.resourceId

@description('Registry hostname used to pull custom OSDU images.')
output acrLoginServer string = acrModule.outputs.loginServer

@description('Cosmos DB Gremlin endpoint used by Entitlements.')
output graphEndpoint string = gremlinModule.outputs.documentEndpoint

@description('Azure resource ID of the Cosmos DB Gremlin account.')
output graphAccountId string = gremlinModule.outputs.resourceId

@description('Name of the storage account shared across data partitions.')
output commonStorageName string = commonStorageName

@description('Azure resource ID of the storage account shared across data partitions.')
output commonStorageId string = storageCommonModule.outputs.resourceId

@description('Ordered data partition identifiers used to index all per-partition output arrays.')
output partitionNames array = dataPartitions

@description('Cosmos DB SQL endpoints ordered to match partitionNames.')
output partitionCosmosEndpoints array = [for i in range(0, length(dataPartitions)): partitionModules[i].outputs.cosmosEndpoint]

@description('Cosmos DB SQL account resource IDs ordered to match partitionNames.')
output partitionCosmosAccountIds array = [for i in range(0, length(dataPartitions)): partitionModules[i].outputs.cosmosAccountId]

@description('Service Bus namespace resource IDs ordered to match partitionNames.')
output partitionServiceBusIds array = [for i in range(0, length(dataPartitions)): partitionModules[i].outputs.serviceBusId]

@description('Service Bus namespace names ordered to match partitionNames.')
output partitionServiceBusNames array = serviceBusNames

@description('Storage account resource IDs ordered to match partitionNames.')
output partitionStorageIds array = [for i in range(0, length(dataPartitions)): partitionModules[i].outputs.storageId]

@description('Storage account names ordered to match partitionNames.')
output partitionStorageNamesOut array = partitionStorageNames

@description('Application Insights connection string, or an empty string when telemetry is disabled.')
#disable-next-line BCP318
output appInsightsConnectionString string = enableApplicationInsights ? appInsights.properties.ConnectionString : ''

@description('Application Insights component name, or an empty string when telemetry is disabled.')
output appInsightsName string = enableApplicationInsights ? appInsightsName : ''

@description('Client ID for the ExternalDNS workload identity, or an empty string when DNS mode is disabled.')
output externalDnsClientId string = !empty(dnsZoneName) ? externalDnsIdentityModule.outputs.clientId : ''

@description('Principal ID for the ExternalDNS workload identity, or an empty string when DNS mode is disabled.')
#disable-next-line BCP318
output externalDnsPrincipalId string = !empty(dnsZoneName) ? externalDnsIdentityModule.outputs.principalId : ''
