{{/*
Copyright 2026, Microsoft

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
*/}}

{{/* Chart name */}}
{{- define "osdu-spi-init.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Common labels */}}
{{- define "osdu-spi-init.labels" -}}
app.kubernetes.io/name: {{ include "osdu-spi-init.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: osdu
{{- end }}

{{/* Shared pod-spec fragment used by every Job. The Workload Identity webhook
     injects AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_FEDERATED_TOKEN_FILE
     automatically when the pod carries azure.workload.identity/use: "true".
     Auth uses urllib + the v1.0 token endpoint, so no MSAL pip-install step.
     The partition-records volume follows its ConfigMap, which only the core
     release renders: legal-init never mounts it, and kubelet fails a pod on
     any declared volume whose ConfigMap is missing. */}}
{{- define "osdu-spi-init.podSpec" -}}
serviceAccountName: {{ .Values.serviceAccountName }}
restartPolicy: Never
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
securityContext:
  runAsNonRoot: true
  seccompProfile:
    type: RuntimeDefault
volumes:
  - name: scripts
    configMap:
      name: osdu-spi-init-scripts
      defaultMode: 0755
{{- if .Values.coreEnabled }}
  - name: partition-records
    configMap:
      name: osdu-spi-init-partition-records
{{- end }}
{{- end }}

{{/* The partition record every partition-init Job POSTs. Identical for every
     partition: the sensitive values are Key Vault secret suffixes the service
     prefixes with the partition id. partition-init hashes this body into its
     Job name, so a changed record reruns the Job on an existing environment. */}}
{{- define "osdu-spi-init.partitionRecord" -}}
{
  "properties": {
    "compliance-ruleset": {
      "sensitive": false,
      "value": "shared"
    },
    "cosmos-connection": {
      "sensitive": true,
      "value": "cosmos-connection"
    },
    "cosmos-endpoint": {
      "sensitive": true,
      "value": "cosmos-endpoint"
    },
    "cosmos-primary-key": {
      "sensitive": true,
      "value": "cosmos-primary-key"
    },
    "sb-connection": {
      "sensitive": true,
      "value": "sb-connection"
    },
    "sb-namespace": {
      "sensitive": true,
      "value": "sb-namespace"
    },
    "storage-account-key": {
      "sensitive": true,
      "value": "storage-account-key"
    },
    "storage-account-name": {
      "sensitive": true,
      "value": "storage"
    },
    "storage-account-blob-endpoint": {
      "sensitive": true,
      "value": "storage-account-blob-endpoint"
    },
    "elastic-endpoint": {
      "sensitive": true,
      "value": "elastic-endpoint"
    },
    "elastic-username": {
      "sensitive": true,
      "value": "elastic-username"
    },
    "elastic-password": {
      "sensitive": true,
      "value": "elastic-password"
    },
    "elastic-ssl-enabled": {
      "sensitive": false,
      "value": "true"
    },
{{- with .Values.tenantServiceAccount }}
    "app-dev-sp-username": {
      "sensitive": false,
      "value": {{ . | quote }}
    },
    "serviceAccount": {
      "sensitive": false,
      "value": {{ . | quote }}
    },
{{- end }}
    "indexer-decimation-enabled": {
      "sensitive": false,
      "value": "true"
    }
  }
}
{{- end }}
