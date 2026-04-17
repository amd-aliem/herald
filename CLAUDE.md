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
python herald.py config             # Launch interactive TUI config editor
```

There is no test suite, linter, or build system.

## Architecture

The entire application is `herald.py`, structured into core classes, a TUI, and a CLI entry point.

### Class hierarchy

- **`ActivitySource`** (ABC) — abstract base with shared caching helpers (`get_cache_path`, `is_cache_valid`, `load_cache`, `save_cache`) and abstract methods `fetch_activity(since)` and `validate()`.
- **`GitHubSource(ActivitySource)`** — GitHub REST API v3 fetcher. Holds `repositories`, `max_commits`, `activity_types`. Methods: `github_request()`, `fetch_commits()`, `fetch_pulls()`, `fetch_issues()`, `fetch_releases()`, `fetch_activity()`, `validate()`.
- **`SOURCE_REGISTRY`** — dict mapping source type strings (`"github"`) to source classes. Extend by adding entries here.
- **`AIBackend`** (ABC) — abstract base for AI summarization backends with `summarize(prompt, label)` and `validate()` methods.
- **`ClaudeCLIBackend(AIBackend)`** — default backend using Claude CLI subprocess. Configurable `timeout`. Saves debug prompts on failure.
- **`AI_BACKEND_REGISTRY`** — dict mapping backend type strings (`"claude-cli"`) to backend classes. Extend by adding entries here.
- **`Herald`** — main orchestrator. Handles config loading, group resolution, AI backend invocation, report saving, and Teams posting.
- **`ConfigManager`** — Rich-based CLI config editor (fallback when Textual is not installed). Uses `Prompt`, `IntPrompt`, `Confirm` from Rich.
- **`ConfigApp(App)`** — Textual TUI config editor (see TUI section below). Guarded by `if HAS_TEXTUAL:`.
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
- `_load_group_secrets()` — loads `secrets/<group-name>.json` and merges recognized secret fields into group dict
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

### TUI (`python herald.py config`)

The `config` subcommand launches a Textual TUI for interactive configuration management. Falls back to a Rich-based CLI (`ConfigManager`) when Textual is not installed. All TUI code lives inside an `if HAS_TEXTUAL:` guard.

#### TUI class hierarchy

- **`VimDataTable(DataTable)`** / **`VimListView(ListView)`** — widgets with hjkl vim bindings mapped to arrow keys. Used throughout all screens.
- **`ConfirmModal(ModalScreen[bool])`** — yes/no confirmation dialog. Accepts `yes_label` and `yes_variant` to customize the confirm button (defaults to destructive "Yes, delete" styling).
- **`InputModal(ModalScreen[str])`** — generic single-field text input modal. Returns the input string on submit, empty string on cancel. Enter key submits.
- **`SelectModal(ModalScreen[str])`** — generic option picker using VimListView. Returns the selected value string, empty string on cancel (`b` or `Escape`).
- **`MainMenuScreen`** — top-level navigation: Defaults, Groups, Secrets, Validate, Exit.
- **`DefaultsScreen`** — view/edit defaults via `InputModal` (integers, activity types) and `SelectModal` (AI backend type).
- **`GroupsScreen`** — list groups, create (`c`) via `InputModal` + `SelectModal`, delete (`d`) via chained `ConfirmModal`s.
- **`GroupDetailScreen`** — per-group menu: pushes to `EditSourcesScreen`, `EditTeamContextScreen`, or `JsonViewScreen`.
- **`EditSourcesScreen`** — manage repos (`a` add, `d` delete, `f` filters) with `InputModal`/`ConfirmModal`.
- **`EditFiltersScreen`** — edit exclude_authors/titles/labels via `InputModal`. Validates regex for exclude_titles. `x` clears all.
- **`EditTeamContextScreen`** — edit team name, focus_areas, priorities via `InputModal`.
- **`JsonViewScreen`** — read-only syntax-highlighted JSON view of group config.
- **`SecretsScreen`** — manage webhook URLs via `SelectModal` (update/remove) and `InputModal`.
- **`ValidateScreen`** — runs `Herald.validate()` and displays colorized results.
- **`ConfigApp(App)`** — main app class with file I/O helpers, CSS, and config state.

#### TUI patterns

- **No `app.suspend()`** — all user input happens through native Textual modals (`InputModal`, `SelectModal`, `ConfirmModal`). The TUI never drops to the raw terminal.
- **Callback chaining** — modals are async; results arrive via callbacks passed to `push_screen(modal, callback)`. Multi-step flows chain callbacks (e.g., create group: `InputModal` → `SelectModal`; delete group: `ConfirmModal` → `ConfirmModal`).
- **Re-fetch before mutation** — callbacks that modify data re-fetch the current state (e.g., `self.app._load_secrets(group_name)`) rather than closing over stale references captured before the modal was shown.
- **Save + notify + refresh** — after every mutation, screens call the appropriate save method (`_save_main_config`, `_save_group_config`, `_save_secrets`), show a notification, and refresh their table.
- **`_get_group_data()` helper** — `EditSourcesScreen`, `EditFiltersScreen`, and `EditTeamContextScreen` each have this method returning `(group_entry, full_config, is_external)` for consistent config access.

## Configuration

Config is split into three layers to keep secrets and team-specific data out of version control:

- **`herald.config.json`** (committed) — defaults + group stubs with `config_file` references
- **`groups/*.json`** (gitignored, except `groups/example.json`) — per-group configs with sources and team context
- **`secrets/*.json`** (gitignored, except `secrets/example.json`) — per-group secrets (webhook URLs), auto-discovered by group name

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

Groups can also be defined inline in `herald.config.json` (see `herald.config.example.json`). Flat legacy configs with top-level `repositories` are auto-wrapped as a single group.

### Secrets files (secrets/*.json)

- Auto-discovered by group name: `secrets/<group-name>.json`
- `teams_webhook_url` — Power Automate webhook URL
- Precedence: `HERALD_TEAMS_WEBHOOK` env var > `secrets/<group>.json` > inline group config

### Environment variables

- `HERALD_CONFIG` — config file path (overridden by `--config` CLI arg)
- `HERALD_DAYS` — override `defaults.time_window_days` (integer)
- `HERALD_MAX_COMMITS` — override `defaults.max_commits` (integer)
- `HERALD_TEAMS_WEBHOOK` — set webhook URL for groups that lack one
- `GITHUB_TOKEN` — GitHub API token for higher rate limits (5000 vs 60 req/hour)

## Dependencies

- **Python**: `requests`, `python-dateutil` (see `requirements.txt`)
- **Python (optional)**: `textual` for TUI config editor, `rich` for CLI config editor fallback
- **System**: Claude CLI (default AI backend; must be installed and authenticated)
