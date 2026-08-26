// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// AKS-native Flux extension and cluster-scoped GitOps configuration. This
// deploys after CLI bootstrap so the osdu-flux namespace and inputs exist
// before reconciliation starts.

targetScope = 'resourceGroup'

@description('Name of the AKS cluster where Flux is installed as a cluster-scoped extension.')
param clusterName string

@description('HTTPS URL of the Git repository that Flux reconciles.')
param repoUrl string

@description('Git branch that Flux reconciles.')
param repoBranch string = 'main'

@description('Allowed profile directory under software/stacks/osdu/profiles.')
@allowed([
  'bare'
  'minimal'
  'core'
])
param profile string = 'core'

@description('Allowed ingress directory under software/stacks/osdu/ingress.')
@allowed([
  'azure'
  'dns'
  'ip'
])
param ingressMode string = 'azure'

@description('Resource name for the cluster Flux configuration.')
param configurationName string = 'osdu-spi-stack-system'

// The bare profile has no ingress substrate and always selects its empty tree;
// ingressMode is unused. Minimal omits the OSDU HTTPRoute Kustomization because
// that dependsOn spi-osdu-services and would otherwise stall on DependencyNotReady.
var ingressPath = profile == 'bare'
  ? './software/stacks/osdu/ingress/bare'
  : profile == 'minimal'
    ? './software/stacks/osdu/ingress/${ingressMode}-minimal'
    : './software/stacks/osdu/ingress/${ingressMode}'

resource aks 'Microsoft.ContainerService/managedClusters@2024-10-01' existing = {
  name: clusterName
}

resource fluxExtension 'Microsoft.KubernetesConfiguration/extensions@2024-11-01' = {
  name: 'flux'
  scope: aks
  properties: {
    extensionType: 'microsoft.flux'
    autoUpgradeMinorVersion: true
    releaseTrain: 'Stable'
    scope: {
      cluster: {
        releaseNamespace: 'flux-system'
      }
    }
    // Multi-tenancy enforcement injects flux-applier impersonation, which AKS
    // Automatic admission policy rejects with `dry-run failed (Forbidden)`.
    // Disabling it lets controllers apply as their exempt flux-system identities.
    configurationSettings: {
      'multiTenancy.enforce': 'false'
    }
  }
}

resource gitopsConfig 'Microsoft.KubernetesConfiguration/fluxConfigurations@2024-11-01' = {
  name: configurationName
  scope: aks
  properties: {
    scope: 'cluster'
    // AKS Automatic denies deployer writes to flux-system. The CLI seeds
    // SPI-owned inputs into osdu-flux, so reconciliation uses that namespace.
    namespace: 'osdu-flux'
    sourceKind: 'GitRepository'
    gitRepository: {
      url: repoUrl
      repositoryRef: {
        branch: repoBranch
      }
      syncIntervalInSeconds: 600
      timeoutInSeconds: 600
    }
    kustomizations: {
      stack: {
        path: './software/stacks/osdu/profiles/${profile}'
        prune: true
        syncIntervalInSeconds: 600
        timeoutInSeconds: 1800
      }
      ingress: {
        path: ingressPath
        prune: true
        syncIntervalInSeconds: 600
        timeoutInSeconds: 1800
      }
    }
  }
  dependsOn: [
    fluxExtension
  ]
}

@description('Resource name of the deployed Flux configuration.')
output configurationName string = gitopsConfig.name

@description('Resource name of the deployed Flux extension.')
output extensionName string = fluxExtension.name
