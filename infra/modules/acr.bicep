// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//

@description('Globally unique registry name of 5-50 alphanumeric characters.')
param name string

@description('Azure region where the registry is deployed.')
param location string

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: name
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    // Registry admin credentials are disabled; image pulls use RBAC identities.
    adminUserEnabled: false
  }
}

@description('Azure resource ID of the container registry.')
output resourceId string = acr.id

@description('Registry hostname used by container image references.')
output loginServer string = acr.properties.loginServer
