// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// AKS Automatic cluster + managed Istio.
//
// Scope: only the AKS cluster. The cluster uses a user-assigned managed
// identity because Automatic with a BYO VNet requires one. Workload identity
// for pods is a SEPARATE user-assigned identity
// created in infra/main.bicep after this template outputs the OIDC
// issuer URL for federated credentials.
//
// One post-deploy imperative step remains: use `az aks mesh
// enable-istio-cni` to flip managed Istio to CNIChaining. The resource
// provider rejects proxyRedirectionMechanism at create time even though
// newer schemas expose it.

targetScope = 'resourceGroup'

// ──────────────────────────────────────────────
// Parameters
// ──────────────────────────────────────────────

@description('AKS cluster name.')
param clusterName string

@description('Azure region.')
param location string = resourceGroup().location

@description('Kubernetes version for the cluster. Must be >= 1.36: earlier AKS Automatic versions block all MutatingWebhookConfigurations (cert-manager, CNPG) at the authorization layer.')
param kubernetesVersion string = '1.36'

@description('VM size for the system pool. D4lds_v5 has a 150 GiB cache that fits the 128 GiB default ephemeral OS disk.')
param systemPoolVmSize string = 'Standard_D4lds_v5'

@description('Availability zones for the system pool. The Automatic SKU validates this against its recommended values, so all three zones are required unless a region genuinely offers fewer.')
param availabilityZones array = [
  '1'
  '2'
  '3'
]

// ──────────────────────────────────────────────
// Private network (BYO VNet)
// ──────────────────────────────────────────────
//
// AKS Automatic's managed-VNet path is blocked on Microsoft corporate
// tenants by the "Subnets should be private" Azure Policy, which
// requires ``defaultOutboundAccess: false`` on every subnet. The
// managed VNet does not set that flag, so we pre-create a VNet and
// subnet here and pass the subnet ID into the cluster.

module vnetModule 'modules/vnet.bicep' = {
  name: 'spi-aks-vnet'
  params: {
    vnetName: '${clusterName}-vnet'
    natGatewayName: '${clusterName}-natgw'
    publicIpName: '${clusterName}-natgw-pip'
    location: location
  }
}

// ──────────────────────────────────────────────
// Cluster control-plane identity (UAMI)
// ──────────────────────────────────────────────
//
// AKS Automatic + BYO VNet rejects SAMI with
// ``OnlySupportedOnUserAssignedMSICluster``. This is a DIFFERENT
// identity from the OSDU workload identity in infra/main.bicep; this
// one is consumed only by the cluster control plane to reconcile
// network resources in the pre-existing VNet.
//
// The UAMI needs ``Network Contributor`` on the VNet so the cluster
// can attach NICs, manage the NAT gateway association, and reconcile
// the API server subnet delegation. We scope to the VNet rather than
// each subnet individually: AKS Automatic uses a node subnet AND a
// delegated API server subnet (VNet integration), and VNet-level
// scoping keeps the assignment set minimal.

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

// ──────────────────────────────────────────────
// AKS Automatic cluster
// ──────────────────────────────────────────────
//
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

    // Shorter node RG name than the AKS default
    // (``${clusterName}_aks_${clusterName}_nodes``). Immutable after
    // cluster creation, so existing environments keep their old names;
    // new environments get the clean ``{clusterName}-nodes`` form.
    nodeResourceGroup: '${clusterName}-nodes'

    enableRBAC: true
    disableLocalAccounts: true
    supportPlan: 'KubernetesOfficial'

    // Automatic requires public API server for Karpenter.
    publicNetworkAccess: 'Enabled'

    // OIDC issuer URL is output and consumed by infra/main.bicep to
    // wire federated credentials to the workload-identity SAs.
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

    // BYO VNet: outbound goes through the user-assigned NAT Gateway
    // attached to the subnets in vnet.bicep.
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

    // Explicitly enable the CSI disk/file/blob drivers and snapshot
    // controller. This matches the sister Terraform repo
    // (../osdu-spi-infra/main/infra/aks.tf:82-87) and avoids PVCs
    // getting stuck in ExternalProvisioning on fresh clusters.
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

    // System pool. Automatic uses managed hosted pools for its platform
    // components and Karpenter for user workloads; this explicit pool
    // preserves the previous deployment shape for system add-ons.
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

    // Managed Istio with External ingress gateway. CNI chaining is
    // applied imperatively post-deploy (see top-of-file note).
    //
    // revisions is pinned to prevent AKS from silently upgrading the
    // mesh under us. `asm-1-30` is the newest revision compatible with
    // Kubernetes 1.36 (asm-1-28 tops out at 1.35 per
    // `az aks mesh get-revisions`).
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

// ──────────────────────────────────────────────
// Outputs (consumed by downstream Bicep + CLI imperative steps)
// ──────────────────────────────────────────────

output clusterName string = clusterName
output clusterResourceId string = aksCluster.id
output oidcIssuerUrl string = aksCluster.properties.?oidcIssuerProfile.?issuerURL ?? ''
output clusterPrincipalId string = clusterIdentity.properties.principalId
output kubeletIdentityObjectId string = aksCluster.properties.?identityProfile.?kubeletidentity.?objectId ?? ''
output istioRevision string = 'asm-1-30'
