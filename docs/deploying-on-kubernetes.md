# Deploying Herald on Kubernetes

This guide walks through deploying Herald as a scheduled `CronJob` with the Helm
chart in `helm/herald/`, and covers the operational gotchas that aren't obvious
from the chart alone. It assumes you've already published an image (see the
README's "Publishing the image") and configured at least one team.

## Overview

The chart deploys:

- a **CronJob** that runs `herald.py digest` on a schedule,
- a **ConfigMap** holding team configs (`values.config`),
- a **Secret** holding API credentials and per-team webhooks (chart-generated or
  supplied out of band),
- optionally a **PersistentVolumeClaim** for the clone/response cache.

The digest step needs two kinds of network access from inside the cluster:

1. **GitHub** — to fetch activity (public egress, optionally a token).
2. **The LLM endpoint** — an Anthropic-compatible `/v1/messages` API.

Both must be reachable from the pod. The most common deployment problems are
about that second one, or about storage — see [Troubleshooting](#troubleshooting).

## Prerequisites

- A published image your cluster can pull (public registry, or a private one
  with an image pull secret).
- A namespace to deploy into: `kubectl create namespace herald`.
- Your team config(s) in `values.config` (or the chart's default example).
- Credentials: an LLM API key, optionally a GitHub token, and — if posting — a
  webhook URL per team.

## Managing secrets

You can let the chart generate the Secret from values, or manage it yourself.
**Prefer managing it yourself** so credentials never land in a values file or
your shell history.

### Option A — chart-generated (quick, less secure)

```bash
helm install herald ./helm/herald -n herald \
  --set secrets.anthropicApiKey=$ANTHROPIC_API_KEY \
  --set secrets.githubToken=$GITHUB_TOKEN \
  --set-string secrets.teamWebhooks.my-team=$WEBHOOK_URL
```

### Option B — existing Secret (recommended)

Create the Secret with `kubectl` (or sealed-secrets / external-secrets). It holds
the API credentials as keys and each team's webhook as a `<team>.json` file:

```bash
kubectl create secret generic herald-secrets -n herald \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --from-literal=GITHUB_TOKEN="$(gh auth token)" \
  --from-file=my-team.json=config/secrets/my-team.json
# If your endpoint needs a custom auth header, also:
#   --from-literal=ANTHROPIC_CUSTOM_HEADERS="X-Auth-Header: <value>"
```

Then point the chart at it:

```bash
helm install herald ./helm/herald -n herald \
  --set secrets.existingSecret=herald-secrets \
  --set 'secrets.webhookTeams={my-team}'
```

Because Helm can't introspect an existing Secret's keys, `secrets.webhookTeams`
lists which `<team>.json` webhook files the Secret contains so they get mounted
at `/app/config/secrets/`. Omit it if you don't post to Teams.

## Keeping site-specific values out of git

Put your real endpoint, team configs, schedule, and image in an untracked
overlay — `helm/herald/values.local.yaml` is gitignored for this purpose — and
pass it with `-f`:

```bash
helm upgrade --install herald ./helm/herald -n herald \
  -f helm/herald/values.local.yaml \
  --set secrets.existingSecret=herald-secrets \
  --set 'secrets.webhookTeams={my-team}'
```

Keep credentials in the Secret (Option B), not in the overlay. The overlay is for
non-secret but environment-specific settings.

## Verifying a deployment

Trigger a one-off Job instead of waiting for the schedule:

```bash
kubectl create job --from=cronjob/herald herald-manual -n herald
kubectl logs -f job/herald-manual -n herald
```

A healthy run logs each repo fetch, `Rated N PRs`, `Digest saved`, and — with
`--post` — `Posted to Teams successfully`. Clean up test jobs afterward:

```bash
kubectl delete job herald-manual -n herald
```

## Troubleshooting

### Pod stuck `Pending` — no storage

If the pod never schedules and the PVC stays `Pending`:

```
no persistent volumes available for this claim and no storage class is set
```

the cluster has no default `StorageClass` to satisfy the cache PVC. The cache is
just scratch space (shallow clones + short-TTL response cache), so an ephemeral
volume is fine:

```bash
--set cache.persistent=false      # use an emptyDir instead of a PVC
```

Set a real class instead if you want the cache to persist across runs:
`--set cache.storageClass=<class>`.

### `ErrImagePull` / `ImagePullBackOff`

The pod can't pull the image. Check the resolved reference:

```bash
kubectl describe pod -n herald -l app.kubernetes.io/name=herald | grep -i image
```

- Make sure `image.repository`/`image.tag` point at an image that actually
  exists in a registry (a locally-built image that was never pushed won't pull).
- For a private registry, add an image pull secret via `imagePullSecrets`.

### LLM endpoint hostname won't resolve

If GitHub fetches succeed but every rating/generation call fails with a DNS
error like `Failed to resolve '<host>'`, the pod's DNS can't resolve the LLM
endpoint even though the network route to it may be fine. This is common for
**internal gateways on split-horizon DNS**: a workstation resolves the name via
a corporate resolver, but the cluster's DNS doesn't know it.

First confirm it's DNS and not routing. Resolution and reachability are separate:

```bash
# From a pod, test raw reachability to the endpoint's IP (bypassing DNS).
# If the TCP connect succeeds, it's a DNS-only problem and hostAliases will fix it.
kubectl run nettest --rm -it --restart=Never -n herald \
  --image=<your-herald-image> --command -- \
  python -c "import socket; socket.create_connection(('<endpoint-ip>', 443), 5); print('reachable')"
```

If the IP is reachable, pin the hostname in the pod's `/etc/hosts` with
`hostAliases` (chart-supported). In your overlay:

```yaml
hostAliases:
  - ip: "<endpoint-ip>"
    hostnames:
      - "<endpoint-hostname>"
```

Redeploy and verify the entry landed:

```bash
kubectl exec -n herald <pod> -- cat /etc/hosts   # should list the pinned host
```

If the IP is **not** reachable either, it's a routing/firewall issue, not DNS —
`hostAliases` won't help. Run the digest somewhere that can reach the endpoint
(e.g. a host on the right network, or CI), or fix cluster egress.

### Degraded digests are not posted

If some PR ratings fail because the endpoint is intermittently unreachable,
Herald treats the run as degraded: it **saves** the digest for inspection but
**refuses to `--post`** a partially-analyzed digest and exits non-zero. Check the
job logs for `N failed`, resolve the endpoint issue, and re-run. This is
intentional — a scheduled team post should never ship an analysis-free digest.

## Uninstalling

```bash
helm uninstall herald -n herald
# The chart-generated Secret/ConfigMap/PVC go with it. An out-of-band Secret
# (Option B) is not owned by Helm — delete it separately if desired:
kubectl delete secret herald-secrets -n herald
```
