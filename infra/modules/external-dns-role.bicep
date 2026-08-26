// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Assigns DNS Zone Contributor at zone scope. The caller must deploy this
// module at the zone's resource group in the current subscription.

targetScope = 'resourceGroup'

@description('Existing Azure DNS zone to be managed by ExternalDNS.')
param dnsZoneName string

@description('Principal ID of the ExternalDNS managed identity receiving zone access.')
param principalId string

// Azure built-in DNS Zone Contributor role.
var dnsZoneContributorRoleId = 'befefa01-2a29-4197-83a8-272ff33ce314'

resource zone 'Microsoft.Network/dnsZones@2018-05-01' existing = {
  name: dnsZoneName
}

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: zone
  name: guid(zone.id, principalId, dnsZoneContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dnsZoneContributorRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
