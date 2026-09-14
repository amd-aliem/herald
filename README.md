# Herald

Herald fetches GitHub repository activity and produces team-focused digests. Run
it headless with `herald.py digest` (calls the Anthropic Messages API directly —
no Node or Claude Code needed); in Claude Code, `/herald-config` can build your
team config interactively.

## Quick Start

Clone to a digest on disk. Posting to Teams is optional — see step 5.

```bash
pip install -r requirements.txt

# LLM + GitHub creds. Gateway instead of api.anthropic.com? Also set
# ANTHROPIC_BASE_URL / ANTHROPIC_CUSTOM_HEADERS (see LLM Endpoint).
export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_TOKEN=$(gh auth token)

# Config: copy the examples (or run /herald-config in Claude Code). Edit the team
# file, and list the team in config/herald.json's teams[].
cp config/herald.example.json config/herald.json
cp config/teams/team.example.json config/teams/my-team.json

python herald.py validate                  # pre-flight: config, creds, endpoint
python herald.py digest --team my-team      # -> reports/my-team/herald-digest-<date>.md
python herald.py digest --team my-team --post   # optional: deliver to Teams
```

## Quick Start (Kubernetes)

Run it as a scheduled `CronJob` via the Helm chart (needs a working team config
and webhook from above). Full walkthrough:
[docs/deploying-on-kubernetes.md](docs/deploying-on-kubernetes.md).

```bash
# Image: use the public GHCR one, or build your own (see the deploy doc).
kubectl create namespace herald

# Credentials as a Secret you manage. Gateway? add ANTHROPIC_CUSTOM_HEADERS.
kubectl create secret generic herald-secrets -n herald \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --from-literal=GITHUB_TOKEN="$(gh auth token)" \
  --from-file=my-team.json=config/secrets/my-team.json

# Install with your team(s)/endpoint in a gitignored overlay.
helm install herald ./helm/herald -n herald \
  -f helm/herald/values.local.yaml \
  --set secrets.existingSecret=herald-secrets \
  --set 'secrets.webhookTeams={my-team}'

# Test now instead of waiting for the schedule (default: Mondays 09:00 UTC).
kubectl create job --from=cronjob/herald herald-manual -n herald
kubectl logs -f job/herald-manual -n herald   # expect "Posted to Teams successfully"
```

## Commands

Run `python herald.py <command> --help` for options. Common flags: `--team/-t`
(repeatable; omit for all), `--days N`, `--force`, `-o FILE`, `--post`.

| Command | Purpose |
|---------|---------|
| `fetch` | Fetch activity from configured sources to JSON |
| `digest` | Full pipeline: fetch → rate PRs → generate markdown (add `--post` for Teams) |
| `list-teams` | List configured teams |
| `validate` | Pre-flight checks: config, creds, LLM endpoint, `GITHUB_TOKEN` |
| `post` | Post a digest (markdown on stdin) to a Teams webhook |

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

Running in a cluster is covered in
[docs/deploying-on-kubernetes.md](docs/deploying-on-kubernetes.md) (image
publishing, secrets, scheduling, troubleshooting).

## Skills (Claude Code)

Optional. If you use Claude Code, these skills live in `.claude/skills/` and are
available automatically in this repo — handy for interactive setup and
exploration. The headless `herald.py digest` command is the primary way to run
Herald (and the only one used by the container and CI).

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
  herald.json              main config (defaults + team list) — you create this
  herald.example.json      copy me to herald.json
  teams/
    team.example.json      copy me to teams/<name>.json
    my-team.json           your team config (gitignored)
  secrets/
    secrets.example.json   copy me to secrets/<name>.json (only needed to post)
    my-team.json           your webhook URL (gitignored)
```

Copy the `*.example.json` files and edit them — `herald.example.json` shows both
the `config_file` reference and inline-team patterns; `teams/team.example.json`
is the per-team template; `secrets/<name>.json` holds `{ "teams_webhook_url": ... }`.

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

`herald fetch` outputs a structured `{ meta, activity[] }` envelope consumed by
the digest step. See `schema/activity.json` for the full schema. (Commits that
duplicate PR merge/head SHAs are removed during fetch.)

To add a source type, subclass `ActivitySource`, register it in
`SOURCE_REGISTRY`, and use its `type` in a team's `sources`.

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
