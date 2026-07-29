// Copyright 2026, Microsoft
// Licensed under the Apache License, Version 2.0.
//
// GitOps activation: AKS-native Flux extension plus the cluster-scoped
// GitRepository + Kustomization that Flux will reconcile against.
//
// Deployed AFTER ``infra/aks.bicep`` and ``infra/main.bicep`` and AFTER
// the CLI's imperative K8s bootstrap phase. The Bicep declaration replaces
// the previous ``az k8s-configuration flux create`` call; re-deploys drift-
// reconcile the Kustomization config (sync interval, repo branch, profile
// path) without the CLI having to branch on create-vs-update.

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
    // Disable Flux multi-tenancy enforcement. With it on (default since
    // extension v1.9) the agent injects `serviceAccountName: flux-applier`
    // and the controllers impersonate that SA; AKS Automatic's
    // protect-system-namespaces ValidatingAdmissionPolicy denies the
    // impersonation, failing every reconcile with `dry-run failed
    // (Forbidden)`. With enforcement off the controllers apply as their own
    // exempt flux-system identities.
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
    // SPI-owned GitOps objects (GitRepository, Kustomizations) live in
    // osdu-flux, NOT the extension's flux-system: AKS Automatic's
    // protect-system-namespace-objects policy denies deployer writes to
    // flux-system, so the CLI seeds ConfigMaps/secrets into osdu-flux and
    // the Flux config must reconcile from the same namespace.
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

output configurationName string = gitopsConfig.name
output extensionName string = fluxExtension.name
