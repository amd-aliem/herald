# Deploying Herald on Kubernetes

This guide walks through deploying Herald as a scheduled `CronJob` with the Helm
chart in `helm/herald/`, and covers the operational gotchas that aren't obvious
from the chart alone. It assumes you've already published an image (see
[Building & publishing the image](#building--publishing-the-image) below) and
configured at least one team.

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

## Building & publishing the image

The `.github/workflows/publish-image.yml` workflow builds the image and pushes it
to the GitHub Container Registry on version tags — no extra secrets needed (it
uses the built-in `GITHUB_TOKEN`):

```bash
git tag v0.1.0
git push origin v0.1.0
# -> ghcr.io/<owner>/herald:0.1.0, :0.1, :sha-<commit>, :latest
```

Make the package **public** (repo → Packages → package settings) so clusters can
pull without an image pull secret. `values.yaml` defaults `image.repository` to
this published image. To publish elsewhere, build and push manually:

```bash
docker build -t <registry>/herald:<tag> .
docker push <registry>/herald:<tag>
```

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

## Scheduling

The CronJob's `cronjob.schedule` is a standard 5-field cron expression:

```
┌─ minute (0-59)
│ ┌─ hour (0-23)
│ │ ┌─ day of month (1-31)
│ │ │ ┌─ month (1-12)
│ │ │ │ ┌─ day of week (0-6, Sun=0)
0 9 * * 1     # every Monday at 09:00
```

| When | Cron |
|------|------|
| Monday 09:00 | `0 9 * * 1` |
| Weekdays 08:30 | `30 8 * * 1-5` |
| Daily 07:00 | `0 7 * * *` |
| Mondays & Thursdays 09:00 | `0 9 * * 1,4` |
| Every 6 hours | `0 */6 * * *` |

### Timezone

Kubernetes CronJobs run in **UTC** unless you set a timezone. Use
`cronjob.timeZone` with an IANA zone name so the schedule reflects local
wall-clock time — a named zone also tracks daylight-saving transitions
automatically:

```yaml
cronjob:
  schedule: "0 8 * * 1"      # 08:00...
  timeZone: "America/Chicago"  # ...Central time, DST-aware
```

Changing the schedule is a `helm upgrade` — Kubernetes patches the live CronJob
in place; existing/running Jobs are unaffected.

## Multiple teams

Herald resolves the Teams webhook **per team** at post time, from
`config/secrets/<team>.json` (mounted from the Secret). `--team` is repeatable,
and omitting it digests every configured team. So a single command already fans
out to each team's own channel:

```bash
herald.py digest --team team-a --team team-b --post   # each posts to its own webhook
```

There are two ways to shape this on the cluster.

### Model A — one CronJob, all teams, one schedule

Point `cronjob.args` at several teams (or omit `--team` for all) and mount every
webhook. The Secret holds all the webhook files; `webhookTeams` lists them:

```bash
helm upgrade --install herald ./helm/herald -n herald \
  -f helm/herald/values.local.yaml \
  --set secrets.existingSecret=herald-secrets \
  --set 'secrets.webhookTeams={team-a,team-b,team-c}'
# with cronjob.args: [digest, --team, team-a, --team, team-b, --team, team-c, --post]
```

Simplest, but all teams share one schedule and run sequentially in one pod; one
team's failure affects the batch.

### Model B — one release per team (independent schedules)

Install the chart once per team as separate releases, each with its own
`fullnameOverride`, schedule/timezone, team args, and mounted webhook. **All
releases share one Secret** — it contains the API credentials (common to every
team) plus every team's `<team>.json` webhook; each release mounts only its own
via `webhookTeams`:

```bash
# Shared secret: API creds + all webhook files, created once.
kubectl create secret generic herald-secrets -n herald \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --from-literal=GITHUB_TOKEN="$(gh auth token)" \
  --from-file=team-a.json=config/secrets/team-a.json \
  --from-file=team-b.json=config/secrets/team-b.json \
  --from-file=team-c.json=config/secrets/team-c.json

# One release per team, each mounting only its own webhook.
helm upgrade --install herald-a ./helm/herald -n herald \
  -f helm/herald/values.team-a.yaml \
  --set secrets.existingSecret=herald-secrets \
  --set 'secrets.webhookTeams={team-a}'
# ...repeat for herald-b, herald-c
```

Each overlay sets `fullnameOverride` (so the CronJob/ConfigMap names don't
collide), its `cronjob.args`/`schedule`/`timeZone`, and its team in
`config.teams`. This gives independent schedules, isolated failures, and
per-team job history at the cost of managing several releases.

Choose Model A when every team wants the same slot; Model B when they need
different times or failure isolation.

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
