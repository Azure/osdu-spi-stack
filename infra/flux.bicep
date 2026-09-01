// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// AKS Flux extension and cluster-scoped GitOps configuration, deployed after
// the CLI bootstrap so the osdu-flux namespace and its inputs already exist.

targetScope = 'resourceGroup'

@description('Name of the AKS cluster where Flux is installed as a cluster-scoped extension.')
param clusterName string

@description('HTTPS URL of the Git repository that Flux reconciles.')
param repoUrl string

@description('Git branch that Flux reconciles.')
param repoBranch string = 'main'

@description('Git tag that Flux reconciles. When set, this takes the place of repoBranch.')
param repoTag string = ''

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

// bare has no ingress substrate, so ingressMode is unused; the minimal trees
// omit the OSDU HTTPRoutes, whose dependsOn spi-osdu-services would never resolve.
var ingressPath = profile == 'bare'
  ? './software/stacks/osdu/ingress/bare'
  : profile == 'minimal'
    ? './software/stacks/osdu/ingress/${ingressMode}-minimal'
    : './software/stacks/osdu/ingress/${ingressMode}'

var repositoryRef = empty(repoTag)
  ? {
      branch: repoBranch
    }
  : {
      tag: repoTag
    }

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
    // Multi-tenancy enforcement applies as an impersonated flux-applier, which
    // the AKS Automatic admission policy rejects; off, the controllers apply as
    // their exempt flux-system identities.
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
    // The deployer cannot write to flux-system on AKS Automatic; the CLI seeds
    // its inputs into osdu-flux instead.
    namespace: 'osdu-flux'
    sourceKind: 'GitRepository'
    gitRepository: {
      url: repoUrl
      repositoryRef: repositoryRef
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
