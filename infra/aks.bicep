// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// AKS Automatic cluster with managed Istio on a pre-created VNet. main.bicep
// binds the workload identity to this template's OIDC issuer output; the CLI
// enables Istio CNI chaining afterwards because the resource provider rejects
// proxyRedirectionMechanism at creation.

targetScope = 'resourceGroup'

@description('Resource name for the AKS Automatic cluster.')
param clusterName string

@description('Azure region where the cluster and its network resources are deployed.')
param location string = resourceGroup().location

@description('Kubernetes version accepted by AKS; must be 1.36 or later because required operators use mutating webhooks.')
param kubernetesVersion string = '1.36'

@description('VM SKU for the system pool; its cache must accommodate the ephemeral OS disk.')
param systemPoolVmSize string = 'Standard_D4lds_v5'

@description('Availability zones for the system pool; the CLI supplies the subscription-resolved usable set, and the default applies to direct template deploys and to CLI runs where the SKU catalogue read fails.')
param availabilityZones array = [
  '1'
  '2'
  '3'
]

// The managed-VNet path cannot satisfy the "Subnets should be private" policy;
// vnet.bicep carries the reason.
module vnetModule 'modules/vnet.bicep' = {
  name: 'spi-aks-vnet'
  params: {
    vnetName: '${clusterName}-vnet'
    natGatewayName: '${clusterName}-natgw'
    publicIpName: '${clusterName}-natgw-pip'
    location: location
  }
}

// Automatic with a BYO VNet rejects a system-assigned identity
// (OnlySupportedOnUserAssignedMSICluster). This control-plane identity needs
// Network Contributor on the whole VNet for NICs, NAT association and the API
// server subnet delegation; OSDU workloads never use it.
var clusterIdentityName = '${clusterName}-ctl-id'
var networkContributorRoleId = '4d97b98b-1d4f-4787-a291-c67834d212e7'

resource clusterIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: clusterIdentityName
  location: location
}

resource aksVnet 'Microsoft.Network/virtualNetworks@2024-01-01' existing = {
  name: '${clusterName}-vnet'
}

resource clusterIdentityNetworkContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aksVnet
  name: guid(aksVnet.id, clusterIdentity.id, networkContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributorRoleId)
    principalId: clusterIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
  dependsOn: [
    vnetModule
  ]
}

// Automatic SKU validation requires the user-assigned identity, an ephemeral OS
// disk on the explicit system pool, the webAppRouting and KeyvaultSecretsProvider
// add-ons, and hostedSystemProfile on the BYO VNet so the service-created hosted
// pool does not fall back to a managed VNet. The BYO VNet also switches
// outboundType to the NAT gateway vnet.bicep creates.
resource aksCluster 'Microsoft.ContainerService/managedClusters@2026-03-01' = {
  name: clusterName
  location: location
  sku: {
    name: 'Automatic'
    tier: 'Standard'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${clusterIdentity.id}': {}
    }
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: clusterName

    // Immutable after creation.
    nodeResourceGroup: '${clusterName}-nodes'

    enableRBAC: true
    disableLocalAccounts: true
    supportPlan: 'KubernetesOfficial'

    // Automatic requires public API server for Karpenter.
    publicNetworkAccess: 'Enabled'

    oidcIssuerProfile: {
      enabled: true
    }

    hostedSystemProfile: {
      enabled: true
      nodeSubnetID: vnetModule.outputs.subnetId
      systemNodeSubnetID: vnetModule.outputs.systemNodeSubnetId
    }
    nodeProvisioningProfile: {
      mode: 'Auto'
      defaultNodePools: 'Auto'
    }

    networkProfile: {
      outboundType: 'userAssignedNATGateway'
      networkPlugin: 'azure'
      serviceCidr: '192.168.0.0/16'
      dnsServiceIP: '192.168.0.10'
      loadBalancerSku: 'standard'
    }

    // API server VNet integration is always on with a BYO VNet and needs its
    // own delegated subnet.
    apiServerAccessProfile: {
      subnetId: vnetModule.outputs.apiServerSubnetId
    }

    ingressProfile: {
      webAppRouting: {
        enabled: true
      }
    }
    addonProfiles: {
      azureKeyvaultSecretsProvider: {
        enabled: true
        config: {
          enableSecretRotation: 'true'
        }
      }
    }

    // Explicit drivers prevent PVCs from remaining in ExternalProvisioning.
    storageProfile: {
      diskCSIDriver: {
        enabled: true
      }
      fileCSIDriver: {
        enabled: true
      }
      blobCSIDriver: {
        enabled: true
      }
      snapshotController: {
        enabled: true
      }
    }

    agentPoolProfiles: [
      {
        name: 'systempool'
        count: 1
        mode: 'System'
        vmSize: systemPoolVmSize
        osDiskType: 'Ephemeral'
        osType: 'Linux'
        availabilityZones: availabilityZones
        vnetSubnetID: vnetModule.outputs.subnetId
      }
    ]

    // A pinned revision keeps AKS from upgrading the mesh independently of the
    // Kubernetes version.
    serviceMeshProfile: {
      mode: 'Istio'
      istio: {
        revisions: [
          'asm-1-30'
        ]
        components: {
          ingressGateways: [
            {
              enabled: true
              mode: 'External'
            }
          ]
        }
      }
    }
  }
  dependsOn: [
    clusterIdentityNetworkContributor
  ]
}

@description('Resource name of the deployed AKS cluster.')
output clusterName string = clusterName

@description('Azure resource ID of the deployed AKS cluster.')
output clusterResourceId string = aksCluster.id

@description('OIDC issuer URL required when creating workload identity federated credentials.')
output oidcIssuerUrl string = aksCluster.properties.?oidcIssuerProfile.?issuerURL ?? ''

@description('Principal ID of the control-plane identity used to reconcile network resources.')
output clusterPrincipalId string = clusterIdentity.properties.principalId
