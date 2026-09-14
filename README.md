# Herald

Herald fetches GitHub repository activity and produces team-focused digests. The
data pipeline lives in Python. There are **two ways to run the AI step**:

1. **Headless (`herald.py digest`)** — calls the Anthropic Messages API directly.
   No Node or Claude Code required. This is what the container and CI use.
2. **Interactive (Claude Code skills)** under `.claude/skills/` — for local,
   exploratory runs.

## Quick Start (headless)

```bash
# 1. Install
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_...  # optional but recommended (5000 vs 60 req/hour)

# 2. Point Herald at the Anthropic API
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL=claude-opus-4-8
# Optional: route through a proxy or corporate LLM gateway instead
# export ANTHROPIC_BASE_URL=https://your-gateway.example.com/anthropic
# export ANTHROPIC_CUSTOM_HEADERS="X-Auth-Header: <value>"

# 3. Configure a team (see Configuration below) then generate a digest
python herald.py digest --team my-team
python herald.py digest --team my-team --days 30 --post   # deliver to Teams
```

## Quick Start (Claude Code)

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_...

/herald-config               # inside Claude Code — interactive team setup
/herald-digest my-team       # full run: fetch, rate PRs, analyze, save report
```

## CLI Reference

Herald uses subcommands. Run `python herald.py <command> --help` for full details.

### fetch

Fetch activity from configured sources and write structured JSON.

```bash
# Fetch one team to stdout
python herald.py fetch --team my-team

# Fetch with options
python herald.py fetch --team my-team --days 7 --force -o .cache/activity.json

# Fetch ad-hoc repos (ignores team config)
python herald.py fetch --repos owner/repo1,owner/repo2

# Fetch multiple teams (writes to .cache/activity-<team>.json each)
python herald.py fetch -t team-a -t team-b
```

### digest

Full headless pipeline: fetch activity, rate PRs for relevance, and generate a
markdown digest by calling the Anthropic Messages API directly (no Claude Code).
Requires an LLM endpoint configured via the `ANTHROPIC_*` environment variables
(see [LLM Endpoint](#llm-endpoint-for-digest)).

```bash
# Digest one team → reports/<team>/herald-digest-<date>.md
python herald.py digest --team my-team

# Widen the window, bypass cache, write to a specific file
python herald.py digest -t my-team --days 30 --force -o digest.md

# Generate and post to Teams (webhook resolved from secrets)
python herald.py digest -t my-team --post

# All configured teams (omit --team)
python herald.py digest
```

### list-teams

```bash
python herald.py list-teams
```

### validate

Run pre-flight checks on config, secrets, and environment.

```bash
python herald.py validate
```

### post

Post a digest to Microsoft Teams via Power Automate webhook. Reads markdown from stdin.

```bash
python herald.py post --team my-team < reports/my-team/herald-digest-2026-06-24.md
python herald.py post --webhook-url "$URL" < digest.md
```

## LLM Endpoint (for `digest`)

The `digest` command talks to any Anthropic-compatible `/v1/messages` endpoint —
`api.anthropic.com` by default, or a proxy/corporate gateway. Settings come from
the environment (override) or a `defaults.ai_backend` block in config, with
environment variables taking precedence so the same image runs unchanged across
environments.

| Variable | Config key | Effect |
|----------|------------|--------|
| `ANTHROPIC_API_KEY` | `ai_backend.api_key` | API key (required) |
| `ANTHROPIC_MODEL` | `ai_backend.model` | Model id, e.g. `claude-opus-4-8` |
| `ANTHROPIC_BASE_URL` | `ai_backend.base_url` | Endpoint base (default `https://api.anthropic.com`) |
| `ANTHROPIC_CUSTOM_HEADERS` | `ai_backend.headers` | Extra headers as `Key: Value` lines (one per line), for gateways that need custom auth |

Behind a corporate LLM gateway? See your internal setup notes for the base URL,
model id, and any custom auth header.

The `digest` command ports the `herald-rate-pr` and `herald-analyze` skill
prompts into Python, so headless and interactive runs produce comparable output.

## Publishing the image

The `.github/workflows/publish-image.yml` workflow builds the image and pushes it
to the GitHub Container Registry on version tags — no extra secrets needed (it
uses the built-in `GITHUB_TOKEN`):

```bash
git tag v0.1.0
git push origin v0.1.0
# -> ghcr.io/<owner>/herald:0.1.0, :0.1, :sha-<commit>, :latest
```

Make the package **public** (repo → Packages → package settings) so clusters can
pull without an image pull secret. To publish elsewhere, build and push manually:

```bash
docker build -t <registry>/herald:<tag> .
docker push <registry>/herald:<tag>
```

## Container & Kubernetes

Herald ships a `python:3.12-slim` image and a Helm chart that runs `herald.py
digest` on a schedule. `values.yaml` defaults `image.repository` to the published
GHCR image; override it for a private registry or mirror.

For a full walkthrough — secret management, one-off test runs, and common
gotchas (storage, image pulls, endpoint DNS) — see
[docs/deploying-on-kubernetes.md](docs/deploying-on-kubernetes.md).

```bash
docker build -t herald:poc .    # or pull the published ghcr.io image

helm install herald ./helm/herald \
  --set secrets.anthropicApiKey=$ANTHROPIC_API_KEY \
  --set secrets.githubToken=$GITHUB_TOKEN \
  --set-string secrets.teamWebhooks.my-team=$WEBHOOK_URL
# Behind a gateway, also: --set secrets.anthropicCustomHeaders="X-Auth-Header: <value>"
```

Team config lives in the chart's **ConfigMap** (`values.config`); API keys and
per-team webhooks live in the **Secret**. The endpoint base URL and model are set
under `endpoint:` in `values.yaml`. The CronJob's `cronjob.args` selects which
team(s) to digest.

### Managing the Secret out of band

To keep credentials out of Helm values (recommended), create the Secret yourself
and point the chart at it with `secrets.existingSecret`. The Secret holds the API
credentials as keys and each team's webhook as a `<team>.json` file:

```bash
kubectl create secret generic herald-secrets -n herald \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --from-literal=GITHUB_TOKEN="$(gh auth token)" \
  --from-file=my-team.json=config/secrets/my-team.json
# Behind a gateway, also:
#   --from-literal=ANTHROPIC_CUSTOM_HEADERS="X-Auth-Header: <value>"

helm install herald ./helm/herald \
  --set secrets.existingSecret=herald-secrets \
  --set 'secrets.webhookTeams={my-team}'   # webhook files to mount for --post
```

Because Helm can't read an existing Secret's keys, `secrets.webhookTeams` lists
which `<team>.json` webhook files it contains so they get mounted at
`/app/config/secrets/`. Omit it if you don't post to Teams.

## Skills (Claude Code)

Skills live in `.claude/skills/` and are available automatically when working in this repo.

| Skill | Invocation | Purpose |
|-------|------------|---------|
| herald-digest | `/herald-digest <team>` | Full run: fetch, rate PRs, analyze, save report |
| herald-analyze | (called by digest) | Produce digest markdown from activity JSON |
| herald-rate-pr | (called by digest) | Rate one PR's relevance to a team (1-5) |
| herald-config | `/herald-config` | Interactive team configuration builder |
| herald-post | `/herald-post <file>` | Post digest to Teams (requires explicit request) |

## Configuration

Herald uses a layered config structure:

```
config/
  herald.json              main config (defaults + team list)
  herald.example.json      example showing both patterns
  teams/
    team.example.json      per-team template
    my-team.json           your team config (gitignored)
  secrets/
    secrets.example.json   webhook URL template
    my-team.json           your webhook URL (gitignored)
```

### Main config: `config/herald.json`

```json
{
  "defaults": {
    "time_window_days": 14,
    "max_commits": 20,
    "activity_types": ["commits", "pulls", "issues", "releases"]
  },
  "teams": [
    { "name": "my-team", "config_file": "teams/my-team.json" }
  ]
}
```

Teams can reference external files (`config_file`) or be defined inline with `sources` directly in the main config. See `config/herald.example.json` for both patterns.

### Team config: `config/teams/<name>.json`

```json
{
  "sources": [{
    "type": "github",
    "repositories": ["owner/repo-1", "owner/repo-2"],
    "filters": {
      "exclude_authors": ["dependabot[bot]"],
      "exclude_titles": ["^build\\(deps\\):"],
      "exclude_labels": ["wontfix"]
    },
    "diff_keywords": ["security", "breaking", "critical"],
    "fetch_comments": true,
    "deep_analysis": { "enabled": true, "clone_dir": ".cache/repos" }
  }],
  "display_name": "My Team",
  "focus_areas": ["Area your team cares about"],
  "priorities": ["Top priority (flagged in digests)", "Secondary priority"],
  "sub_teams": []
}
```

### Secrets: `config/secrets/<name>.json`

```json
{ "teams_webhook_url": "https://your-power-automate-webhook-url" }
```

### Key fields

| Field | Default | Description |
|-------|---------|-------------|
| `time_window_days` | 14 | How far back to fetch |
| `max_commits` | 20 | Per repo per request |
| `activity_types` | all four | Subset of commits, pulls, issues, releases |
| `exclude_authors` | [] | Skip activity from these users |
| `exclude_titles` | [] | Regex patterns to skip by title |
| `diff_keywords` | [] | Fetch diffs for PRs matching these keywords |
| `fetch_comments` | false | Include recent PR/issue comments |
| `deep_analysis.enabled` | false | Clone repos and read context files |
| `display_name` | required | Team name shown in digests |
| `focus_areas` | [] | Team's technical interests (fed to AI) |
| `priorities` | [] | Ordered; first entry is flagged as high priority |
| `sub_teams` | [] | Merge sources from other configured teams |

## Environment Variables

| Variable | Effect |
|----------|--------|
| `GITHUB_TOKEN` | GitHub API auth (5000 req/hour vs 60) |
| `HERALD_CONFIG` | Override config file path |
| `HERALD_DAYS` | Override `time_window_days` |
| `HERALD_MAX_COMMITS` | Override `max_commits` |
| `HERALD_TEAMS_WEBHOOK` | Fallback webhook URL for all teams |
| `ANTHROPIC_*` | LLM endpoint for `digest` — see [LLM Endpoint](#llm-endpoint-for-digest) |

## Activity JSON Schema

`herald fetch` outputs a structured envelope consumed by skills:

```json
{
  "meta": {
    "team": "my-team",
    "fetched_at": "2026-06-24T12:00:00+00:00",
    "time_window_days": 14,
    "since": "2026-06-10T12:00:00+00:00",
    "team_context": { "name": "...", "focus_areas": [...], "priorities": [...] },
    "stats": { "repos": 3, "commits": 42, "pulls": 12, "pulls_with_diffs": 4, "issues": 7, "releases": 1 }
  },
  "activity": [
    {
      "repository": "owner/repo",
      "source_type": "github",
      "commits": [...],
      "pulls": [...],
      "issues": [...],
      "releases": [...]
    }
  ]
}
```

Commits that duplicate PR merge/head SHAs are automatically removed during fetch. See `schema/activity.json` for the full JSON Schema.

## Extending Herald

### Adding a source type

Subclass `ActivitySource` and register it:

```python
class GitLabSource(ActivitySource):
    source_type = "gitlab"

    def validate(self) -> bool:
        return True

    def fetch_activity(self, since: datetime) -> List[Dict[str, Any]]:
        return [{"repository": "...", "commits": [], "pulls": [], "issues": [], "releases": []}]

SOURCE_REGISTRY["gitlab"] = GitLabSource
```

Then use `"type": "gitlab"` in your team config sources.

## Troubleshooting

### GitHub rate limit exceeded

```bash
export GITHUB_TOKEN=ghp_your_token
```

This gives 5,000 requests/hour vs 60 unauthenticated. `python herald.py validate` will show your current status.

### No activity found

- Verify repository names are `owner/repo` format
- Increase the window: `--days 30`
- Check that repos are public or your token has access

### Cache

Cache lives in `.cache/` with a 1-hour TTL. Files older than 7 days are pruned automatically.

```bash
# Bypass cache for one run
python herald.py fetch --team my-team --force

# Clear cache entirely
rm -rf .cache/
```

## License

MIT License
