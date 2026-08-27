// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// AKS Automatic cluster with managed Istio. The BYO VNet requires a dedicated
// control-plane identity; the pod workload identity is created separately by
// main.bicep from this template's OIDC issuer output.
//
// The CLI enables Istio CNI chaining after deployment because the resource
// provider rejects proxyRedirectionMechanism during cluster creation.

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

// The "Subnets should be private" Azure Policy requires
// ``defaultOutboundAccess: false``, which the managed-VNet path does not set.
// A pre-created VNet is therefore required in subscriptions enforcing the policy.

module vnetModule 'modules/vnet.bicep' = {
  name: 'spi-aks-vnet'
  params: {
    vnetName: '${clusterName}-vnet'
    natGatewayName: '${clusterName}-natgw'
    publicIpName: '${clusterName}-natgw-pip'
    location: location
  }
}

// AKS Automatic + BYO VNet rejects SAMI with
// ``OnlySupportedOnUserAssignedMSICluster``. This identity is used only by the
// control plane to reconcile the pre-existing VNet, not by OSDU workloads.
//
// The UAMI needs ``Network Contributor`` on the VNet so the cluster
// can manage NICs, NAT association, and API server subnet delegation. VNet
// scope covers every subnet AKS Automatic reconciles.

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

// Automatic SKU validation requires:
//   - UAMI (user-assigned managed identity) when using BYO VNet.
//     Managed-VNet Automatic clusters require SAMI; BYO-VNet requires
//     UAMI; these are mutually exclusive.
//   - Ephemeral OS disks on the explicit system pool
//   - webApplicationRouting and KeyvaultSecretsProvider add-ons enabled
//   - hostedSystemProfile wired to the BYO VNet so AKS Automatic's
//     service-created "hostedpool" does not fall back to a managed VNet
//
// With BYO VNet, outboundType switches from managedNATGateway to
// userAssignedNATGateway (the NAT we pre-created in vnet.bicep).

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

    // Pin the node resource group name because it is immutable after creation.
    nodeResourceGroup: '${clusterName}-nodes'

    enableRBAC: true
    disableLocalAccounts: true
    supportPlan: 'KubernetesOfficial'

    // Automatic requires public API server for Karpenter.
    publicNetworkAccess: 'Enabled'

    // main.bicep consumes the issuer URL to create federated credentials.
    oidcIssuerProfile: {
      enabled: true
    }

    // Keep AKS Automatic's service-created hosted pools on the BYO VNet.
    hostedSystemProfile: {
      enabled: true
      nodeSubnetID: vnetModule.outputs.subnetId
      systemNodeSubnetID: vnetModule.outputs.systemNodeSubnetId
    }
    nodeProvisioningProfile: {
      mode: 'Auto'
      defaultNodePools: 'Auto'
    }

    // The BYO subnets use the NAT gateway created by vnet.bicep.
    networkProfile: {
      outboundType: 'userAssignedNATGateway'
      networkPlugin: 'azure'
      serviceCidr: '192.168.0.0/16'
      dnsServiceIP: '192.168.0.10'
      loadBalancerSku: 'standard'
    }

    // API server VNet integration is always-on for AKS Automatic with
    // BYO VNet and requires a dedicated delegated subnet distinct from
    // the node subnets (see vnet.bicep).
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

    // Automatic requires the explicit system pool to use an ephemeral OS disk.
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

    // Pin the Istio revision so AKS does not upgrade the mesh independently
    // of the Kubernetes version. ADR-002 records the compatibility requirement.
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
