// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Reservoir DDMS persistence for one OSDU data partition. The service reaches
// the public endpoint only through the AKS NAT gateway and authenticates as the
// dedicated Reservoir workload identity. Password authentication stays disabled.

@description('Globally unique PostgreSQL Flexible Server name.')
param name string

@description('OSDU data partition that owns this server.')
param partition string

@description('Azure region where the server is deployed.')
param location string

@description('Name of the Reservoir workload identity configured as the Entra administrator.')
param identityName string

@description('Principal ID of the Reservoir workload identity.')
param identityPrincipalId string

@description('Public IP of the AKS NAT gateway allowed through the PostgreSQL firewall.')
param aksNatPublicIpAddress string

@description('Existing Key Vault that receives the passwordless libpq connection string.')
param keyVaultName string

@description('PostgreSQL major version supported by Reservoir DDMS.')
param postgresVersion string = '16'

@description('Development/test compute SKU for Reservoir DDMS.')
param skuName string = 'Standard_B2ms'

var databaseName = 'postkv'
var serverFqdn = '${name}.postgres.database.azure.com'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: name
  location: location
  sku: {
    name: skuName
    tier: 'Burstable'
  }
  properties: {
    version: postgresVersion
    createMode: 'Default'
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
    storage: {
      autoGrow: 'Enabled'
      storageSizeGB: 32
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource workloadIdentityAdministrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: server
  name: identityPrincipalId
  properties: {
    principalName: identityName
    principalType: 'ServicePrincipal'
    tenantId: tenant().tenantId
  }
}

resource aksNatFirewallRule 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: server
  name: 'aks-nat-egress'
  properties: {
    startIpAddress: aksNatPublicIpAddress
    endIpAddress: aksNatPublicIpAddress
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource connectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: '${partition}-reservoir-conn'
  properties: {
    value: 'host=${serverFqdn} port=5432 dbname=${databaseName} user=${identityName} sslmode=require'
  }
  dependsOn: [
    database
    workloadIdentityAdministrator
    aksNatFirewallRule
  ]
}

@description('Azure resource ID of the PostgreSQL Flexible Server.')
output resourceId string = server.id

@description('PostgreSQL server FQDN used by Reservoir DDMS.')
output fullyQualifiedDomainName string = serverFqdn

@description('Database name used by Reservoir DDMS.')
output databaseName string = databaseName
