# Herald - Multi-Source Repository Activity Tracker

Herald fetches recent activity from GitHub repositories and generates AI-powered summaries using Claude CLI. Configure groups of repositories with team-specific context so the AI highlights what matters most to your team.

## Quick Start

### Prerequisites

- Python 3.8+
- [Claude CLI](https://github.com/anthropics/claude-code) installed and authenticated
- (Optional) `GITHUB_TOKEN` environment variable for higher API rate limits

### Install

```bash
pip install -r requirements.txt
```

### Configure

Copy the example config and create your group config:

```bash
cp herald.config.example.json herald.config.json
cp groups/example.json groups/my-team.json
# Edit groups/my-team.json with your repositories, team context, and webhook URL
```

### Run

```bash
python herald.py
```

## Usage

```bash
# Use default config (herald.config.json)
python herald.py

# Override time window
python herald.py --days 30

# Override repositories (ignores configured groups)
python herald.py --repos owner/repo1,owner/repo2

# Save output to file
python herald.py --output digest.md

# Force regenerate, ignoring cache
python herald.py --force

# Post to Microsoft Teams
python herald.py --teams

# Run specific group(s) only
python herald.py --group my-team
python herald.py -g team-a -g team-b

# List configured groups
python herald.py --list-groups

# Use a custom config file
python herald.py --config /path/to/config.json
```

## Configuration

Herald splits configuration into two layers:

- **`herald.config.json`** -- structural config (defaults + group stubs). Committed to the repo.
- **`groups/*.json`** -- per-group configs with team context, repositories, and webhook URLs. Gitignored (except `groups/example.json`).

This keeps secrets (webhook URLs) and team-specific data out of version control.

### File structure

```
herald.config.json          <-- committed, structural only
herald.config.example.json  <-- committed, shows full structure
groups/
  example.json              <-- committed, template for new groups
  my-team.json              <-- gitignored, your team's config
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

### Group config file (gitignored)

Each group file contains sources, team context, and optional webhook URL:

```json
{
  "sources": [
    {
      "type": "github",
      "repositories": ["owner/repo-1", "owner/repo-2"]
    }
  ],
  "team_context": {
    "name": "My Team",
    "focus_areas": ["Area your team cares about"],
    "priorities": ["Top priority (shown first in AI summaries)"]
  },
  "teams_webhook_url": "https://..."
}
```

### Creating a new group

```bash
cp groups/example.json groups/my-team.json
# Edit groups/my-team.json with your repositories, team context, and webhook URL
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
| `groups[].team_context.name` | Team display name used in prompts |
| `groups[].team_context.focus_areas` | List of focus areas injected into the AI prompt |
| `groups[].team_context.priorities` | Ordered list; first entry is flagged as critical priority |
| `groups[].teams_webhook_url` | Power Automate webhook URL (optional) |

### Inline groups

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

## Reports

Detailed raw activity reports are saved automatically to:

```
reports/
  <group-name>/
    herald-detailed-20260402_143000.md
```

The `reports/` directory is gitignored.

## Automation

### GitHub Actions

The included workflow (`.github/workflows/herald.yml`) runs weekly on Monday at 9:00 UTC. It can also be triggered manually with custom inputs.

Required secrets:
- `ANTHROPIC_API_KEY` -- for Claude CLI

Optional secrets:
- `GITHUB_TOKEN` -- automatically provided by Actions; gives 5,000 req/hour rate limit

Manual trigger inputs:
- **days**: lookback period (default: 14)
- **group**: specific group name (blank = all)
- **force**: ignore cache (default: false)

### Cron

```bash
# Weekly Monday 9am
0 9 * * 1 cd /path/to/herald && python herald.py --output digest-$(date +\%Y\%m\%d).md
```

## Extending Herald

### Adding a New Source

Create a subclass of `ActivitySource` and register it:

```python
class GitLabSource(ActivitySource):
    source_type = "gitlab"

    def validate(self) -> bool:
        # Validate config
        return True

    def fetch_activity(self, since: datetime) -> List[Dict[str, Any]]:
        # Fetch from GitLab API
        return [{"repository": "...", "commits": [], "pulls": [], "issues": [], "releases": []}]

SOURCE_REGISTRY["gitlab"] = GitLabSource
```

Then use it in config:

```json
{
  "sources": [
    { "type": "gitlab", "repositories": ["group/project"] }
  ]
}
```

### Adding a Custom AI Backend

Herald uses a pluggable AI backend system. The default is `claude-cli`, but you can add custom backends by subclassing `AIBackend`:

```python
class OllamaBackend(AIBackend):
    backend_type = "ollama"

    def validate(self) -> bool:
        # Check if Ollama is running
        return True

    def summarize(self, prompt: str, label: str = "") -> Optional[str]:
        # Call Ollama API and return the response text
        return "..."

AI_BACKEND_REGISTRY["ollama"] = OllamaBackend
```

Then set it in config:

```json
{
  "defaults": {
    "ai_backend": {
      "type": "ollama",
      "model": "llama2",
      "api_url": "http://localhost:11434"
    }
  }
}
```

The backend receives the full `ai_backend` config dict, so you can pass backend-specific options like model name, API URL, timeout, etc. Access them via `self.config.get("model")` in your backend class.

### Custom Team Context

The `team_context` block is injected directly into the AI prompt. The first entry in `priorities` is flagged as **CRITICAL PRIORITY** and those items appear first in the summary. Use this to tune the AI output for your team's needs.

## Architecture

```
herald.py
  ActivitySource (ABC)     -- cache helpers, abstract fetch_activity/validate
  GitHubSource             -- GitHub REST API v3 fetcher
  SOURCE_REGISTRY          -- maps type strings to source classes
  AIBackend (ABC)          -- abstract base for AI summarization backends
  ClaudeCLIBackend         -- Claude CLI subprocess backend (default)
  AI_BACKEND_REGISTRY      -- maps type strings to backend classes
  Herald                   -- orchestrator: config, groups, prompts, AI backend, reports, Teams
  main()                   -- CLI argument parsing
```

**Data flow:**
1. Config loaded from `herald.config.json`, resolved into groups
2. Each group's sources fetch activity from their respective APIs (cached with 1-hour TTL)
3. A detailed raw report is saved to `reports/<group>/`
4. Activity + team context formatted into a prompt sent to the configured AI backend
5. AI response is stripped of conversational preamble/postamble
6. Final digest output to stdout/file; optionally posted to Teams

## Troubleshooting

### Claude CLI not found

```bash
npm install -g @anthropic-ai/claude-code
claude auth login
```

### GitHub rate limit exceeded

Set `GITHUB_TOKEN` for 5,000 requests/hour (vs 60 unauthenticated):

```bash
export GITHUB_TOKEN=your_token
python herald.py
```

### No activity found

- Verify repository names are in `owner/repo` format
- Check that the time window includes recent activity (`--days 30`)
- Ensure repositories are public or your token has access

### Cache

Cache is stored in `.cache/` with a 1-hour TTL. To clear:

```bash
rm -rf .cache/
```

Or use `--force` to bypass cache for a single run.

## License

MIT License
