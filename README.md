# Herald

Herald fetches GitHub repository activity and produces team-focused digests. The data pipeline lives in Python; AI analysis lives in Claude Code skills.

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_...  # optional but recommended (5000 vs 60 req/hour)

# 2. Configure (interactive)
/herald-config               # inside Claude Code

# 3. Generate a digest
/herald-digest my-team
```

Or manually:

```bash
python herald.py fetch --team my-team -o activity.json
# then use /herald-analyze with the JSON, or pipe through your own tooling
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
