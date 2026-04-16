# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Herald is a single-file Python tool that fetches GitHub repository activity (commits, PRs, issues, releases) and generates AI-powered summaries via Claude CLI. It supports multiple groups of repositories, each with team-specific context that tunes the AI output. Originally built for the OCC (Open Confidential Computing) Team, now generalized for any team.

## Running the Tool

```bash
# Install dependencies
pip install -r requirements.txt

# Basic run (uses ./herald.config.json)
python herald.py

# Common options
python herald.py --days 30          # Override time window
python herald.py --repos owner/repo # Override repositories (ignores groups)
python herald.py --output file.md   # Save to file
python herald.py --force            # Bypass cache
python herald.py --teams            # Post to Microsoft Teams
python herald.py --config path.json # Custom config file
python herald.py --group occ-team   # Run specific group only
python herald.py --list-groups      # Show configured groups
```

There is no test suite, linter, or build system.

## Architecture

The entire application is `herald.py`, structured into three classes plus a CLI entry point.

### Class hierarchy

- **`ActivitySource`** (ABC) — abstract base with shared caching helpers (`get_cache_path`, `is_cache_valid`, `load_cache`, `save_cache`) and abstract methods `fetch_activity(since)` and `validate()`.
- **`GitHubSource(ActivitySource)`** — GitHub REST API v3 fetcher. Holds `repositories`, `max_commits`, `activity_types`. Methods: `github_request()`, `fetch_commits()`, `fetch_pulls()`, `fetch_issues()`, `fetch_releases()`, `fetch_activity()`, `validate()`.
- **`SOURCE_REGISTRY`** — dict mapping source type strings (`"github"`) to source classes. Extend by adding entries here.
- **`AIBackend`** (ABC) — abstract base for AI summarization backends with `summarize(prompt, label)` and `validate()` methods.
- **`ClaudeCLIBackend(AIBackend)`** — default backend using Claude CLI subprocess. Configurable `timeout`. Saves debug prompts on failure.
- **`AI_BACKEND_REGISTRY`** — dict mapping backend type strings (`"claude-cli"`) to backend classes. Extend by adding entries here.
- **`Herald`** — main orchestrator. Handles config loading, group resolution, AI backend invocation, report saving, and Teams posting.
- **`main()`** — CLI argument parsing and orchestration.

### Data flow

1. Config loaded from `herald.config.json` (CWD > home dir > legacy `occ-digest.config.json` fallback), overridden by CLI args
2. Config resolved into groups; each group has one or more sources
3. Each source fetches activity via its API, cached in `.cache/` with 1-hour TTL
4. A detailed raw report is saved to `reports/<group-name>/herald-detailed-{TIMESTAMP}.md`
5. Activity data + team context formatted into a structured prompt sent to the configured AI backend (default: Claude CLI)
6. AI response is stripped of conversational preamble/postamble via `strip_conversational_output()`
7. Final digest output to stdout/file; optionally posted to Teams via Power Automate webhook

### Key methods on `Herald`

- `load_config()` — JSON config with CLI arg overrides; falls back to legacy config with warning
- `resolve_groups()` — parses groups array or wraps flat config as single "default" group
- `list_groups()` — prints configured groups
- `generate_group_digest(group)` — processes one group end-to-end
- `generate_digest(group_names, repositories)` — iterates groups
- `format_prompt(activity_list, team_context)` — dynamic priority injection from `team_context["priorities"][0]`; includes output format constraint
- `strip_conversational_output(text)` — strips preamble before first `**TL;DR:**` line or `###` heading and trailing conversational lines
- `generate_summary()` — orchestrates prompt + AI backend call with caching and output stripping
- `save_detailed_report(group_name, activity_list)` — writes to `reports/<group>/`
- `post_to_teams(digest, webhook_url)` / `markdown_to_adaptive_card_blocks()` — Teams Adaptive Card integration

### Resilience patterns

- 403 rate limit → falls back to stale cache, shows reset time
- AI backend failure → saves debug prompt to `.cache/`, falls back to formatted raw data
- Cache uses per-repo, per-activity-type, per-day files with 1-hour TTL; `--force` bypasses

## Configuration

Config is split into two layers to keep secrets and team-specific data out of version control:

- **`herald.config.json`** (committed) — defaults + group stubs with `config_file` references
- **`groups/*.json`** (gitignored, except `groups/example.json`) — per-group configs with sources, team context, and webhook URLs

### herald.config.json (structural only)

- `defaults.time_window_days` — lookback period (default 14)
- `defaults.max_commits` — per-repo limit (default 20)
- `defaults.activity_types` — subset of `["commits", "pulls", "issues", "releases"]`
- `defaults.ai_backend` — AI backend config; `type` (default `"claude-cli"`) + backend-specific options
- `groups[].name` — group identifier
- `groups[].config_file` — path to external group config (resolved relative to config file directory)

### Group config files (groups/*.json)

- `sources[].type` — source type (`"github"`)
- `sources[].repositories` — list of `owner/repo` strings
- `sources[].filters.exclude_authors` — exact-match author/login names to exclude
- `sources[].filters.exclude_titles` — regex patterns to exclude by title/subject
- `sources[].filters.exclude_labels` — exact-match label names to exclude (PRs/issues)
- `team_context.name`, `.focus_areas`, `.priorities` — injected into Claude prompts
- `teams_webhook_url` — Power Automate webhook (optional)

Groups can also be defined inline in `herald.config.json` (see `herald.config.example.json`). Flat legacy configs with top-level `repositories` are auto-wrapped as a single group.

### Environment variables

- `HERALD_CONFIG` — config file path (overridden by `--config` CLI arg)
- `HERALD_DAYS` — override `defaults.time_window_days` (integer)
- `HERALD_MAX_COMMITS` — override `defaults.max_commits` (integer)
- `HERALD_TEAMS_WEBHOOK` — set webhook URL for groups that lack one
- `GITHUB_TOKEN` — GitHub API token for higher rate limits (5000 vs 60 req/hour)

## Dependencies

- **Python**: `requests`, `python-dateutil` (see `requirements.txt`)
- **System**: Claude CLI (default AI backend; must be installed and authenticated)
