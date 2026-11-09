{{/*
Expand the name of the chart.
*/}}
{{- define "workspace.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "workspace.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "workspace.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "workspace.labels" -}}
helm.sh/chart: {{ include "workspace.chart" . }}
{{ include "workspace.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: che.eclipse.org
{{- end }}

{{/*
Selector labels
*/}}
{{- define "workspace.selectorLabels" -}}
app.kubernetes.io/name: {{ include "workspace.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "workspace.config" -}}
{{- dict "username" .Values.global.tenant.username "group" .Values.global.tenant.group "workspaceNamespace" (printf "%s-%s" .Values.global.tenant.namespacePrefix .Values.global.tenant.username) "controlNamespace" .Release.Namespace "domain" .Values.global.localClusterDomain "workspace" .Values.devworkspace.name "playground" .Values.playground.name "playgroundEnabled" .Values.playground.enabled "model" .Values.playground.model "rocketchatEnabled" .Values.rocketchat.enabled "rocketchatUrl" .Values.rocketchat.url "rocketchatNamespace" .Values.rocketchat.namespace "rocketchatDeployment" .Values.rocketchat.deployment "rocketchatContainer" .Values.rocketchat.container "subscription" .Values.provisioner.subscription "renewBefore" .Values.provisioner.renewBeforeSeconds "timeout" .Values.provisioner.readinessTimeoutSeconds "startupTimeout" .Values.provisioner.startupReadinessTimeoutSeconds "startupDeadline" .Values.provisioner.startupDeadlineSeconds | toJson -}}
{{- end }}

{{- define "workspace.provisionerPod" -}}
{{- $startup := .startup -}}
{{- with .root -}}
serviceAccountName: tenant-provisioner
restartPolicy: Never
containers:
  - name: provision
    image: {{ .Values.provisioner.image }}
    command: [python3, /config/provision.py]
    {{- if $startup }}
    args: [--startup]
    {{- end }}
    resources:
      requests:
        cpu: 100m
        memory: 128Mi
      limits:
        cpu: "1"
        memory: 512Mi
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: [ALL]
      runAsNonRoot: true
      seccompProfile:
        type: RuntimeDefault
    volumeMounts:
      - name: config
        mountPath: /config
        readOnly: true
volumes:
  - name: config
    configMap:
      name: tenant-provisioner
{{- end }}
{{- end }}
