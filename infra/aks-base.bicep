// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// AKS Standard (Base SKU) cluster with Node Autoprovisioning + managed Istio.
//
// NOTE: This was AKS Automatic, but ~2026-06-02 AKS Automatic made managed
// system node pools mandatory, which non-bypassably BLOCKS creating/modifying
// MutatingWebhookConfigurations (even for cluster-admin) via the
// "AKS-managed security control changes" guardrail. cert-manager and
// CloudNativePG both require MWCs, so the full SPI Stack cannot reconcile on
// Automatic. We therefore run the Base SKU with Node Autoprovisioning (NAP /
// Karpenter) enabled -- this preserves the software layer's Karpenter
// NodePools and per-service nodeSelectors while dropping the Automatic-only
// guardrails.
//
// Scope: only the AKS cluster. The cluster uses a user-assigned managed
// identity (BYO VNet requires UAMI). Workload identity for pods is a SEPARATE
// user-assigned identity created in infra/main.bicep after this template
// outputs the OIDC issuer URL for federated credentials.
//
// One post-deploy imperative step remains: `az aks mesh enable-istio-cni`
// to flip managed Istio to CNIChaining. The resource provider rejects
// proxyRedirectionMechanism at create time even though newer schemas
// expose it.

targetScope = 'resourceGroup'

// ──────────────────────────────────────────────
// Parameters
// ──────────────────────────────────────────────

@description('AKS cluster name.')
param clusterName string

@description('Azure region.')
param location string = resourceGroup().location

@description('Kubernetes version for the cluster.')
param kubernetesVersion string = '1.34'

@description('VM size for the system pool. D4lds_v5 has a 150 GiB cache that fits the 128 GiB default ephemeral OS disk.')
param systemPoolVmSize string = 'Standard_D4lds_v5'

@description('Availability zones for the system node pool. Empty selects a region-aware default. eastus2 excludes zone 2, which lacks zonal capacity for the system VM SKU; other regions use 1, 2, 3.')
param availabilityZones array = []

// Region-aware zone default. eastus2 does not offer zone 2 for the Dlds_v5
// system SKU, so a hardcoded ['1','2','3'] fails there with a SKU/zone
// mismatch; callers can still override via the availabilityZones parameter.
var zonesByRegion = {
  eastus2: ['1', '3']
}
var systemPoolZones = !empty(availabilityZones) ? availabilityZones : (contains(zonesByRegion, location) ? zonesByRegion[location] : ['1', '2', '3'])

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
// AKS Standard (Base SKU) cluster + Node Autoprovisioning
// ──────────────────────────────────────────────
//
// Key config vs a vanilla Base cluster (these are what AKS Automatic used
// to preconfigure and must now be explicit):
//   - aadProfile: Azure RBAC for Kubernetes (the CLI grants the deployer
//     cluster-admin and authenticates with kubelogin azurecli mode).
//   - securityProfile.workloadIdentity + oidcIssuerProfile: federated
//     workload identity for OSDU pods (wired in infra/main.bicep).
//   - nodeProvisioningProfile (NAP / Karpenter): REQUIRED -- the software
//     layer defines platform/osdu Karpenter NodePools and every OSDU
//     service nodeSelects them. NAP requires Azure CNI overlay + Cilium.
//   - UAMI (BYO VNet requires user-assigned identity).
//   - Ephemeral OS disks + webAppRouting + KeyvaultSecretsProvider add-ons.
//
// With BYO VNet, outbound egress flows through the user-assigned NAT
// Gateway pre-created in vnet.bicep (outboundType: userAssignedNATGateway).

resource aksCluster 'Microsoft.ContainerService/managedClusters@2026-03-01' = {
  name: clusterName
  location: location
  sku: {
    name: 'Base'
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

    // Public API server (Karpenter/NAP and the CLI talk to it publicly).
    publicNetworkAccess: 'Enabled'

    // Azure RBAC for Kubernetes authorization. AKS Automatic preconfigured
    // this; Base must set it explicitly. The CLI assigns the deployer the
    // "Azure Kubernetes Service RBAC Cluster Admin" role and authenticates
    // with kubelogin (azurecli mode) -- see src/spi/azure_infra.py.
    aadProfile: {
      managed: true
      enableAzureRBAC: true
    }

    // OIDC issuer URL is output and consumed by infra/main.bicep to wire
    // federated credentials to the workload-identity SAs. workloadIdentity
    // is preconfigured on Automatic but must be explicit on Base.
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }

    // Node Autoprovisioning (Karpenter). REQUIRED: the software layer
    // (software/components/nodepools) defines platform/osdu Karpenter
    // NodePools and every OSDU service nodeSelects them. ``defaultNodePools:
    // 'Auto'`` provisions an untainted default pool that hosts infra
    // (cert-manager, CNPG, flux) which carries no nodeSelector. NAP is GA
    // and requires Azure CNI overlay + Cilium dataplane (set below).
    nodeProvisioningProfile: {
      mode: 'Auto'
      defaultNodePools: 'Auto'
    }

    // Azure CNI Overlay powered by Cilium (required by NAP). Outbound goes
    // through the user-assigned NAT Gateway attached to the subnets in
    // vnet.bicep. CIDRs match the prior deployment shape.
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkDataplane: 'cilium'
      networkPolicy: 'cilium'
      outboundType: 'userAssignedNATGateway'
      podCidr: '10.244.0.0/16'
      serviceCidr: '192.168.0.0/16'
      dnsServiceIP: '192.168.0.10'
      loadBalancerSku: 'standard'
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

    // Bootstrap system pool for kube-system + the NAP/Karpenter controller.
    // NAP provisions all other capacity (default/platform/osdu NodePools).
    agentPoolProfiles: [
      {
        name: 'systempool'
        count: 2
        mode: 'System'
        vmSize: systemPoolVmSize
        osDiskType: 'Ephemeral'
        osType: 'Linux'
        availabilityZones: systemPoolZones
        vnetSubnetID: vnetModule.outputs.subnetId
      }
    ]

    // Managed Istio with External ingress gateway. CNI chaining is
    // applied imperatively post-deploy (see top-of-file note).
    //
    // revisions is pinned to prevent AKS from silently upgrading the
    // mesh under us. `asm-1-28` matches the sister Terraform repo
    // (../osdu-spi-infra/main/infra/aks.tf); validated with 1.34 on
    // KubernetesOfficial and LTS.
    serviceMeshProfile: {
      mode: 'Istio'
      istio: {
        revisions: [
          'asm-1-28'
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

@description('Object ID of the AKS-managed kubelet (node) identity. Consumed by main.bicep/rbac.bicep to grant AcrPull so nodes can pull images from the SPI ACR (e.g. custom OSDU service images). Empty if not yet populated by the RP.')
output kubeletIdentityObjectId string = aksCluster.properties.?identityProfile.?kubeletidentity.?objectId ?? ''
output istioRevision string = 'asm-1-28'
