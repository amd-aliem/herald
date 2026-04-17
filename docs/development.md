# Development Guide

## Adding a New Source

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

## Adding a Custom AI Backend

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

## Custom Team Context

The `team_context` block is injected directly into the AI prompt. The first entry in `priorities` is flagged as **CRITICAL PRIORITY** and those items appear first in the summary. Use this to tune the AI output for your team's needs.

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
