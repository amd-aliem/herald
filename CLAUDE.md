# Herald

Herald fetches GitHub repository activity and produces team digests. The **data pipeline** lives in Python; **AI interaction** lives in Claude Code skills under `.claude/skills/`.

## Architecture

```
.claude/skills/     orchestration + prompts (herald-digest, herald-analyze, …)
herald.py           fetch, validate, list-teams, post
schema/activity.json  JSON boundary between Python and skills
config/             teams, secrets, defaults
```

## Python CLI

```bash
pip install -r requirements.txt

python herald.py fetch --team <name> [--days N] [--output FILE] [--force]
python herald.py list-teams
python herald.py validate
python herald.py post --team <name> < digest.md   # Teams webhook from secrets
```

## Skills (Claude Code)

Skills ship in `.claude/skills/` and are discovered automatically when working in this repo.

| Skill | Purpose |
|-------|---------|
| `/herald-digest` | Full run: fetch → rate PRs → analyze → save report |
| `/herald-analyze` | Produce digest markdown from activity JSON |
| `/herald-rate-pr` | Rate one PR's relevance (1–5) |
| `/herald-config` | Interactive team configuration |
| `/herald-post` | Post digest to Teams (explicit user request only) |

## Configuration

- `config/herald.json` — defaults + team stubs
- `config/teams/*.json` — per-team sources and context
- `config/secrets/*.json` — webhook URLs (gitignored)

See `config/herald.example.json` for structure.

## Environment

- `GITHUB_TOKEN` — higher GitHub API rate limits
- `HERALD_CONFIG`, `HERALD_DAYS`, `HERALD_MAX_COMMITS`, `HERALD_TEAMS_WEBHOOK`
