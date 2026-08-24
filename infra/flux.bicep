// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// GitOps activation: AKS-native Flux extension plus the cluster-scoped
// GitRepository + Kustomization that Flux will reconcile against.
//
// Deployed twice by the CLI on AKS Automatic:
//   1. Extension-only so Azure creates the protected flux-system namespace.
//   2. Full GitOps activation after the CLI writes the ConfigMaps/secrets
//      consumed by postBuild substitutions and HelmRelease valuesFrom.

targetScope = 'resourceGroup'

@description('AKS cluster name; Flux is installed as a cluster-scoped extension.')
param clusterName string

@description('Git repository URL (GitHub-style HTTPS).')
param repoUrl string

@description('Branch Flux should track.')
param repoBranch string = 'main'

@description('Profile path segment under software/stacks/osdu/profiles (e.g., "core").')
@allowed([
  'bare'
  'minimal'
  'core'
])
param profile string = 'core'

@description('Ingress mode path segment under software/stacks/osdu/ingress (azure, dns, or ip).')
@allowed([
  'azure'
  'dns'
  'ip'
])
param ingressMode string = 'azure'

@description('Name of the fluxConfigurations resource on the cluster.')
param configurationName string = 'osdu-spi-stack-system'

@description('Create the fluxConfigurations GitOps resource. Set false to install only the Flux extension and namespace.')
param activateGitOps bool = true

@description('Namespace for SPI-owned GitRepository, Kustomizations, HelmReleases, and bootstrap ConfigMaps.')
param gitopsNamespace string = 'osdu-flux'

@description('Optional local Kubernetes Secret name for private Git repository auth.')
param gitRepositoryLocalAuthRef string = ''

// The bare profile has no ingress substrate and always selects its empty tree;
// ingressMode is unused. Minimal omits the OSDU HTTPRoute Kustomization because
// that dependsOn spi-osdu-services and would otherwise stall on DependencyNotReady.
var ingressPath = profile == 'bare'
  ? './software/stacks/osdu/ingress/bare'
  : profile == 'minimal'
    ? './software/stacks/osdu/ingress/${ingressMode}-minimal'
    : './software/stacks/osdu/ingress/${ingressMode}'

var gitRepositoryBase = {
  url: repoUrl
  repositoryRef: {
    branch: repoBranch
  }
  syncIntervalInSeconds: 600
  timeoutInSeconds: 600
}

var gitRepositoryAuth = !empty(gitRepositoryLocalAuthRef) ? {
  localAuthRef: gitRepositoryLocalAuthRef
} : {}

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
    // Disable Flux multi-tenancy enforcement. With it enabled (the default
    // since extension v1.9), the Azure Flux agent injects
    // `serviceAccountName: flux-applier` into managed Kustomizations and runs
    // the OSS controllers with `--default-service-account=flux-applier`. On
    // AKS Automatic that path is now blocked: the platform's
    // protect-system-namespaces ValidatingAdmissionPolicy denies impersonating
    // the flux-applier service account, so every reconcile fails with
    // `dry-run failed (Forbidden): failed to get server groups`. With
    // enforcement off the controllers apply as their own exempt flux-system
    // identities, which can write to protected namespaces and create the
    // admission webhooks cert-manager and CloudNativePG require.
    configurationSettings: {
      'multiTenancy.enforce': 'false'
    }
    scope: {
      cluster: {
        releaseNamespace: 'flux-system'
      }
    }
  }
}

resource gitopsConfig 'Microsoft.KubernetesConfiguration/fluxConfigurations@2024-11-01' = if (activateGitOps) {
  name: configurationName
  scope: aks
  properties: {
    scope: 'cluster'
    namespace: gitopsNamespace
    sourceKind: 'GitRepository'
    gitRepository: union(gitRepositoryBase, gitRepositoryAuth)
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

output configurationName string = activateGitOps ? gitopsConfig.name : ''
output extensionName string = fluxExtension.name
