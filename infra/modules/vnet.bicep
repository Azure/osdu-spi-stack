// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// Private network for AKS Automatic: VNet + NAT gateway + private subnets.
//
// Exists specifically to satisfy the "Subnets should be private" Azure
// Policy (definition 7bca8353-aa3b-429b-904a-9229c4385837) that ships
// enabled on Microsoft corporate tenants. The policy rejects any subnet
// where ``defaultOutboundAccess`` is not explicitly ``false``. AKS's own
// managed-VNet path does not set this property, so the VNet must be
// pre-created and passed in via ``vnetSubnetID``/``hostedSystemProfile``
// on the managed cluster.
//
// Outbound connectivity is provided by a user-assigned NAT Gateway with
// a Standard SKU public IP; the AKS cluster sets
// ``outboundType: 'userAssignedNATGateway'`` to attach to it.
//
// This module is invoked from ``infra/aks.bicep`` so the VNet and the
// cluster are provisioned in the same deployment and the subnet ID can
// be passed through a module output rather than threaded through the
// CLI.

targetScope = 'resourceGroup'

@description('VNet name.')
param vnetName string

@description('Subnet name for AKS user nodes and pods.')
param subnetName string = 'aks-subnet'

@description('Subnet name for the AKS Automatic API server (VNet integration).')
param apiServerSubnetName string = 'apiserver-subnet'

@description('Subnet name for AKS Automatic managed system nodes.')
param systemNodeSubnetName string = 'systemnode-subnet'

@description('NAT Gateway name (user-assigned, attached to the subnet).')
param natGatewayName string

@description('Public IP name for the NAT Gateway (Standard SKU).')
param publicIpName string

@description('Azure region.')
param location string = resourceGroup().location

@description('VNet address space.')
param vnetAddressPrefix string = '10.240.0.0/16'

@description('User node subnet address prefix (must be within vnetAddressPrefix).')
param subnetAddressPrefix string = '10.240.0.0/17'

@description('API server subnet address prefix. Must be a distinct /28 or larger delegated to Microsoft.ContainerService/managedClusters.')
param apiServerSubnetAddressPrefix string = '10.240.128.0/28'

@description('Managed system node subnet address prefix. Must be distinct from the user node and API server subnets.')
param systemNodeSubnetAddressPrefix string = '10.240.128.64/26'

// ──────────────────────────────────────────────
// Public IP + NAT Gateway (outbound for the private subnet)
// ──────────────────────────────────────────────

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

// ──────────────────────────────────────────────
// VNet + private subnets
// ──────────────────────────────────────────────
//
// The subnet explicitly sets ``defaultOutboundAccess: false`` to satisfy
// Azure Policy "Subnets should be private". Outbound egress still flows
// through the attached NAT Gateway; the flag only disables Azure's
// implicit default-outbound-SNAT (which is being retired in September
// 2025 anyway, so this also future-proofs the deployment).

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
        // Dedicated user node subnet for AKS Automatic node
        // autoprovisioning. The NAT gateway is attached because the
        // cluster uses ``outboundType: 'userAssignedNATGateway'``.
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
        // Dedicated managed system node subnet required by AKS Automatic
        // custom networking. Without this, the service-created
        // "hostedpool" stays on the managed VNet path and rejects the
        // user-assigned NAT gateway.
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
        // Dedicated API server subnet required by AKS Automatic
        // (API Server VNet Integration). Must be delegated to
        // Microsoft.ContainerService/managedClusters. Must also be
        // private (``defaultOutboundAccess: false``) to satisfy the
        // same Azure Policy that drove us to BYO VNet in the first
        // place.
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

// ──────────────────────────────────────────────
// Outputs
// ──────────────────────────────────────────────

output vnetId string = vnet.id
output vnetName string = vnet.name
output subnetId string = '${vnet.id}/subnets/${subnetName}'
output subnetName string = subnetName
output systemNodeSubnetId string = '${vnet.id}/subnets/${systemNodeSubnetName}'
output systemNodeSubnetName string = systemNodeSubnetName
output apiServerSubnetId string = '${vnet.id}/subnets/${apiServerSubnetName}'
output apiServerSubnetName string = apiServerSubnetName
output natGatewayId string = natGateway.id
output publicIpId string = publicIp.id
