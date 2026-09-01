// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// VNet, NAT gateway and private subnets for AKS Automatic. The "Subnets should
// be private" policy (7bca8353-aa3b-429b-904a-9229c4385837) rejects any subnet
// without defaultOutboundAccess: false, which the managed-VNet path never sets,
// so the network is created here and handed to the cluster. Egress still flows
// through the NAT gateway; the flag only disables implicit outbound SNAT.

targetScope = 'resourceGroup'

@description('Resource name for the AKS virtual network.')
param vnetName string

@description('Name of the subnet used by AKS user nodes.')
param subnetName string = 'aks-subnet'

@description('Name of the delegated subnet used for AKS API server VNet integration.')
param apiServerSubnetName string = 'apiserver-subnet'

@description('Name of the subnet used by AKS Automatic managed system nodes.')
param systemNodeSubnetName string = 'systemnode-subnet'

@description('Resource name for the NAT gateway attached to the node subnets.')
param natGatewayName string

@description('Resource name for the public IP assigned to the NAT gateway.')
param publicIpName string

@description('Azure region where the network resources are deployed.')
param location string = resourceGroup().location

@description('CIDR address space containing all AKS subnets.')
param vnetAddressPrefix string = '10.240.0.0/16'

@description('CIDR prefix for the user node subnet; must be within vnetAddressPrefix.')
param subnetAddressPrefix string = '10.240.0.0/17'

@description('API server subnet address prefix. Must be a distinct /28 or larger delegated to Microsoft.ContainerService/managedClusters.')
param apiServerSubnetAddressPrefix string = '10.240.128.0/28'

@description('Managed system node subnet address prefix. Must be distinct from the user node and API server subnets.')
param systemNodeSubnetAddressPrefix string = '10.240.128.64/26'

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-01-01' = {
  name: publicIpName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2024-01-01' = {
  name: natGatewayName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 4
    publicIpAddresses: [
      {
        id: publicIp.id
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        // The cluster's userAssignedNATGateway outbound mode needs the gateway attached.
        name: subnetName
        properties: {
          addressPrefix: subnetAddressPrefix
          defaultOutboundAccess: false
          natGateway: {
            id: natGateway.id
          }
        }
      }
      {
        // Without a system node subnet the hosted pool rejects the user-assigned NAT gateway.
        name: systemNodeSubnetName
        properties: {
          addressPrefix: systemNodeSubnetAddressPrefix
          defaultOutboundAccess: false
          natGateway: {
            id: natGateway.id
          }
        }
      }
      {
        // API server VNet integration needs its own delegated subnet.
        name: apiServerSubnetName
        properties: {
          addressPrefix: apiServerSubnetAddressPrefix
          defaultOutboundAccess: false
          delegations: [
            {
              name: 'aks-apiserver-delegation'
              properties: {
                serviceName: 'Microsoft.ContainerService/managedClusters'
              }
            }
          ]
        }
      }
    ]
  }
}

@description('Azure resource ID of the AKS virtual network.')
output vnetId string = vnet.id

@description('Resource name of the AKS virtual network.')
output vnetName string = vnet.name

@description('Azure resource ID of the user node subnet.')
output subnetId string = '${vnet.id}/subnets/${subnetName}'

@description('Resource name of the user node subnet.')
output subnetName string = subnetName

@description('Azure resource ID of the managed system node subnet.')
output systemNodeSubnetId string = '${vnet.id}/subnets/${systemNodeSubnetName}'

@description('Resource name of the managed system node subnet.')
output systemNodeSubnetName string = systemNodeSubnetName

@description('Azure resource ID of the API server subnet.')
output apiServerSubnetId string = '${vnet.id}/subnets/${apiServerSubnetName}'

@description('Resource name of the API server subnet.')
output apiServerSubnetName string = apiServerSubnetName

@description('Azure resource ID of the NAT gateway used for cluster egress.')
output natGatewayId string = natGateway.id

@description('Azure resource ID of the public IP assigned to the NAT gateway.')
output publicIpId string = publicIp.id
