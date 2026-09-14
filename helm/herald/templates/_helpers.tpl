{{/*
Expand the name of the chart.
*/}}
{{- define "herald.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name.
*/}}
{{- define "herald.fullname" -}}
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
Chart name and version label.
*/}}
{{- define "herald.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "herald.labels" -}}
helm.sh/chart: {{ include "herald.chart" . }}
{{ include "herald.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "herald.selectorLabels" -}}
app.kubernetes.io/name: {{ include "herald.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The Secret name that holds API keys and webhooks (created here or referenced).
*/}}
{{- define "herald.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- printf "%s-secrets" (include "herald.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Team names whose "<team>.json" webhook file should be mounted from the Secret.
When the chart generates the Secret, this is the keys of secrets.teamWebhooks.
With an existingSecret, the chart cannot introspect it, so the caller lists the
webhook filenames to mount via secrets.webhookTeams. Emits a JSON list of names.
*/}}
{{- define "herald.webhookTeams" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.webhookTeams | default list | toJson -}}
{{- else -}}
{{- keys (.Values.secrets.teamWebhooks | default dict) | toJson -}}
{{- end -}}
{{- end }}
