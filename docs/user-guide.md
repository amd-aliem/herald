# User Guide

## Configuration

Herald splits configuration into three layers to keep secrets and team-specific data out of version control:

- **`herald.config.json`** -- structural config (defaults + group stubs). Committed to the repo.
- **`groups/*.json`** -- per-group configs with team context and repositories. Gitignored (except `groups/example.json`).
- **`secrets/*.json`** -- per-group secrets (webhook URLs). Gitignored (except `secrets/example.json`). Auto-discovered by group name.

### File Structure

```
herald.config.json          <-- committed, structural only
herald.config.example.json  <-- committed, shows full structure
groups/
  example.json              <-- committed, template for new groups
  my-team.json              <-- gitignored, your team's config
secrets/
  example.json              <-- committed, template for secrets
  my-team.json              <-- gitignored, your team's secrets
```

### herald.config.json (committed)

Contains defaults and group stubs that reference external config files:

```json
{
  "defaults": {
    "time_window_days": 14,
    "max_commits": 20,
    "activity_types": ["commits", "pulls", "issues", "releases"]
  },
  "groups": [
    { "name": "my-team", "config_file": "groups/my-team.json" }
  ]
}
```

### Group Config File (gitignored)

Each group file contains sources and team context:

```json
{
  "sources": [
    {
      "type": "github",
      "repositories": ["owner/repo-1", "owner/repo-2"],
      "filters": {
        "exclude_authors": ["dependabot[bot]"],
        "exclude_titles": ["^build\\(deps\\):"],
        "exclude_labels": ["wontfix"]
      }
    }
  ],
  "team_context": {
    "name": "My Team",
    "focus_areas": ["Area your team cares about"],
    "priorities": ["Top priority (shown first in AI summaries)"]
  }
}
```

### Secrets File (gitignored)

Secrets are stored separately in `secrets/<group-name>.json`. Herald auto-discovers them by matching the filename to the group name:

```json
{
  "teams_webhook_url": "https://..."
}
```

### Creating a New Group

```bash
cp groups/example.json groups/my-team.json
# Edit groups/my-team.json with your repositories and team context
cp secrets/example.json secrets/my-team.json
# Edit secrets/my-team.json with your webhook URL
```

Then add a stub to `herald.config.json`:

```json
{ "name": "my-team", "config_file": "groups/my-team.json" }
```

### Fields

| Field | Description |
|---|---|
| `defaults.time_window_days` | Lookback period in days (default: 14) |
| `defaults.max_commits` | Max commits fetched per repository (default: 20) |
| `defaults.activity_types` | Subset of `["commits", "pulls", "issues", "releases"]` |
| `defaults.ai_backend.type` | AI backend type (default: `"claude-cli"`) |
| `defaults.ai_backend.timeout` | Backend timeout in seconds (default: 600) |
| `groups[].name` | Identifier for the group (used in CLI and reports) |
| `groups[].config_file` | Path to external group config (relative to config file) |
| `groups[].sources[].type` | Source type (`"github"`) |
| `groups[].sources[].repositories` | List of `owner/repo` strings |
| `groups[].sources[].filters.exclude_authors` | Author usernames to exclude (exact match) |
| `groups[].sources[].filters.exclude_titles` | Regex patterns to exclude by title (case-insensitive) |
| `groups[].sources[].filters.exclude_labels` | Labels to exclude (exact match, PRs/issues only) |
| `groups[].team_context.name` | Team display name used in prompts |
| `groups[].team_context.focus_areas` | List of focus areas injected into the AI prompt |
| `groups[].team_context.priorities` | Ordered list; first entry is flagged as critical priority |
| `secrets/<group>.teams_webhook_url` | Power Automate webhook URL (stored in `secrets/<group-name>.json`) |

### Inline Groups

Groups can also be defined inline in `herald.config.json` (useful for non-sensitive configs). See `herald.config.example.json` for an example showing both patterns.

### Flat Config (Backward Compatible)

Herald also accepts the legacy flat format with `repositories` at the top level. It will be wrapped as a single group named `"default"`:

```json
{
  "repositories": ["owner/repo"],
  "time_window_days": 14,
  "max_commits": 20,
  "activity_types": ["commits", "pulls", "issues", "releases"],
  "team_context": { "name": "My Team", "priorities": ["..."] }
}
```

### Environment Variables

Herald settings can be overridden via environment variables, useful for CI/cron deployments:

| Variable | Overrides | Example |
|---|---|---|
| `HERALD_CONFIG` | Config file path | `HERALD_CONFIG=/etc/herald/config.json` |
| `HERALD_DAYS` | `defaults.time_window_days` | `HERALD_DAYS=30` |
| `HERALD_MAX_COMMITS` | `defaults.max_commits` | `HERALD_MAX_COMMITS=50` |
| `HERALD_TEAMS_WEBHOOK` | `teams_webhook_url` (all groups) | `HERALD_TEAMS_WEBHOOK=https://...` |
| `GITHUB_TOKEN` | GitHub API authentication | `GITHUB_TOKEN=ghp_...` |

Webhook URL precedence (highest to lowest): `HERALD_TEAMS_WEBHOOK` env var > `secrets/<group-name>.json` > inline group config.

### Reports

Detailed raw activity reports are saved automatically to:

```
reports/
  <group-name>/
    herald-detailed-20260402_143000.md
```

The `reports/` directory is gitignored.

## Troubleshooting

### Claude CLI Not Found

```bash
npm install -g @anthropic-ai/claude-code
claude auth login
```

### GitHub Rate Limit Exceeded

Set `GITHUB_TOKEN` for 5,000 requests/hour (vs 60 unauthenticated):

```bash
export GITHUB_TOKEN=your_token
python herald.py
```

Each repository consumes 4-8 API calls (one per activity type, plus pagination). With 10 repos, unauthenticated usage (~60 req/hour) exhausts the quota rapidly. Use a token for any non-trivial setup.

### No Activity Found

- Verify repository names are in `owner/repo` format
- Check that the time window includes recent activity (`--days 30`)
- Ensure repositories are public or your token has access

### Cache

Cache is stored in `.cache/` with a 1-hour TTL. Files older than 7 days are pruned automatically on each run. To clear manually:

```bash
rm -rf .cache/
```

Or use `--force` to bypass cache for a single run.
