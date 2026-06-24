---
name: herald-config
description: Interactively build or extend Herald configuration through conversational Q&A.
allowed-tools: Read, Write, Bash(mkdir *), Bash(ls *), Glob, Grep, AskUserQuestion
user-invocable: true
---

# /herald-config -- Interactive Herald Configuration Generator

Walks the user through building a valid Herald configuration via `AskUserQuestion`. Handles both fresh setup (no existing config) and adding a new team to an existing `config/herald.json`. Writes all config files with proper structure and validates input along the way.

## Project Directory

The herald project lives at `/home/ubuntu/git/amd-aliem/herald`. All paths below are relative to this directory unless noted otherwise.

## Config Schema Reference

### `config/herald.json` (primary config)
```json
{
  "defaults": {
    "time_window_days": 14,
    "max_commits": 20,
    "activity_types": ["commits", "pulls", "issues", "releases"],
  },
  "teams": [
    { "name": "team-name", "config_file": "teams/team-name.json" }
  ]
}
```

### `config/teams/<name>.json` (per-team config)
```json
{
  "sources": [{
    "type": "github",
    "repositories": ["owner/repo"],
    "filters": {
      "exclude_authors": ["dependabot[bot]"],
      "exclude_titles": ["^build\\(deps\\):"],
      "exclude_labels": ["wontfix"]
    },
    "diff_keywords": [],
    "max_diff_size": 50000,
    "fetch_comments": false,
    "max_comments_per_item": 5,
    "deep_analysis": {
      "enabled": false,
      "clone_dir": ".cache/repos",
      "context_files": ["CLAUDE.md", "README.md", "README.rst", "ARCHITECTURE.md", "CONTRIBUTING.md", "docs/architecture.md"],
      "max_file_size": 50000
    }
  }],
  "display_name": "My Team",
  "focus_areas": ["area1", "area2"],
  "priorities": ["top priority", "secondary priority"],
  "sub_teams": []
}
```

### `config/secrets/<name>.json` (webhook URLs)
```json
{ "teams_webhook_url": "https://..." }
```

## Procedure

### Phase 1: Detect existing config

1. Read `config/herald.json` in the project directory.
   - If it exists and has a non-empty `teams` array, note the existing team names. The flow will be "add team to existing config".
   - If it exists but `teams` is empty, the flow is "add first team".
   - If it does not exist, the flow is "fresh setup" — you will create `config/herald.json` with defaults plus the new team.
2. Inform the user what was detected (existing config with N teams, empty config, or no config found).

### Phase 2: Core team info

3. **Team name**: Ask via `AskUserQuestion` for the team name (slug form).
   - Validate: lowercase, no spaces, alphanumeric plus hyphens only (`^[a-z0-9][a-z0-9-]*$`).
   - Reject if it collides with an existing team name.
   - If invalid, explain the constraint and ask again.

4. **Display name**: Ask for a human-readable display name.
   - Suggest a default derived from the team name (e.g., `my-team` → `My Team`).

5. **GitHub repositories**: Ask the user to provide one or more GitHub repositories.
   - Format: `owner/repo`, comma- or space-separated, or one per line.
   - Validate each entry matches `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$`.
   - If any are invalid, show which ones failed and ask again.
   - At least one repository is required.

### Phase 3: Team context

6. **Focus areas**: Ask what technical areas or domains the team cares about.
   - Accept a comma-separated list.
   - Provide examples: "confidential computing, kernel security, firmware, driver development".
   - Optional — if skipped, use an empty list.

7. **Priorities**: Ask what the team's current priorities are (these are injected into AI prompts).
   - Accept a comma-separated list, ordered by importance.
   - Provide examples: "Security hardening for Q3 release, Performance optimization, API stability".
   - Optional — if skipped, use an empty list.

### Phase 4: Source options

8. **Filters — exclude authors**: Ask if they want to exclude any commit/PR authors.
   - Default suggestion: `dependabot[bot]` (pre-selected).
   - Accept comma-separated additions or let them accept the default.

9. **Filters — exclude titles**: Ask if they want regex patterns to exclude PRs/commits by title.
   - Provide examples: `^chore:`, `^docs:`, `^build\(deps\):`.
   - Optional — if skipped, use an empty list.

10. **Filters — exclude labels**: Ask if they want to exclude issues/PRs with specific labels.
    - Provide examples: `wontfix`, `duplicate`, `invalid`.
    - Optional — if skipped, use an empty list.

11. **Diff keywords**: Ask if they want Herald to fetch PR diffs for PRs matching specific keywords.
    - Explain: "When set, Herald fetches the actual code diff for merged PRs whose title or body contains any of these keywords, enabling deeper AI analysis."
    - Provide examples: `security`, `breaking`, `critical`.
    - Optional — if skipped, use an empty list.

12. **Comment fetching**: Ask whether to include recent PR/issue comments in the digest.
    - Default: No.
    - If yes, ask for max comments per item (default: 5).

13. **Deep analysis**: Ask whether to enable deep analysis mode.
    - Explain: "Deep analysis clones tracked repos and reads context files (CLAUDE.md, README, etc.) to give the AI richer understanding of each project."
    - Default: No.

### Phase 5: Delivery and defaults

14. **Webhook URL**: Ask if they have a Microsoft Teams / Power Automate webhook URL for posting digests.
    - Optional — if skipped, no secrets file is created.
    - If provided, it will be saved to `config/secrets/<team-name>.json`.

15. **Defaults customization**: Ask if they want to customize global defaults or use the standard ones.
    - Show the defaults: `time_window_days: 14`, `max_commits: 20`, `activity_types: [commits, pulls, issues, releases]`.
    - If they want to customize, ask for each value individually.
    - Only modify defaults if this is a fresh setup (no existing `config/herald.json`). For existing configs, skip this step to avoid clobbering.

### Phase 6: Write config files

16. **Create directories** if needed:
    ```
    mkdir -p config/teams config/secrets
    ```

17. **Write `config/teams/<team-name>.json`**:
    - Build the team config object from gathered answers.
    - Use `Write` to save the file.

18. **Write `config/secrets/<team-name>.json`** (only if webhook URL was provided):
    - Write `{ "teams_webhook_url": "<url>" }`.

19. **Update `config/herald.json`**:
    - If fresh setup: create the full file with defaults + the new team entry.
    - If existing config: read the current file, append `{ "name": "<team-name>", "config_file": "teams/<team-name>.json" }` to the `teams` array, and write back.
    - Preserve existing formatting by reading, parsing, modifying, and re-serializing with 2-space indent.

### Phase 7: Confirm

20. Report what was written:
    - List the files created/modified.
    - Show a brief summary of the team configuration.
    - Suggest next steps: `python herald.py validate` to verify, then `/herald-digest <team-name>` to generate a digest.

## Validation Rules

- **Team name**: Must match `^[a-z0-9][a-z0-9-]*$`. No spaces, no uppercase, no leading hyphens.
- **Repository format**: Must match `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$`. Must contain exactly one `/`.
- **Unique team name**: Must not collide with any existing team in `config/herald.json`.
- **At least one repo**: The repositories list cannot be empty.

## Sensible Defaults

When the user skips optional fields, use these defaults:

| Field | Default |
|---|---|
| `filters.exclude_authors` | `["dependabot[bot]"]` |
| `filters.exclude_titles` | `[]` |
| `filters.exclude_labels` | `[]` |
| `diff_keywords` | `[]` |
| `max_diff_size` | `50000` |
| `fetch_comments` | `false` |
| `max_comments_per_item` | `5` |
| `deep_analysis.enabled` | `false` |
| `deep_analysis.clone_dir` | `".cache/repos"` |
| `deep_analysis.context_files` | `["CLAUDE.md", "README.md", "README.rst", "ARCHITECTURE.md", "CONTRIBUTING.md", "docs/architecture.md"]` |
| `deep_analysis.max_file_size` | `50000` |
| `time_window_days` | `14` |
| `max_commits` | `20` |
| `activity_types` | `["commits", "pulls", "issues", "releases"]` |

## Important Notes

- Always work in the herald project directory: `/home/ubuntu/git/amd-aliem/herald`.
- Never overwrite existing team config files without asking. If `config/teams/<name>.json` already exists, warn the user and ask for confirmation before overwriting.
- When updating `config/herald.json`, preserve all existing teams and defaults — only append the new team entry.
- Write JSON with 2-space indentation and a trailing newline.
- The `config/secrets/` directory is gitignored — safe to write secrets there.
