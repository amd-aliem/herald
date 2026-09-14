# Herald

Herald fetches GitHub repository activity and produces team digests. There are **two ways to run the AI step**:

1. **Interactive (Claude Code skills)** under `.claude/skills/` — for local, exploratory runs.
2. **Headless (`herald.py digest`)** — calls the Anthropic Messages API directly (no Node/Claude Code). This is what the container and CI use.

## Architecture

```
.claude/skills/     orchestration + prompts (herald-digest, herald-analyze, …)
herald.py           fetch, validate, list-teams, post, digest
schema/activity.json  JSON boundary between Python and skills
config/             teams, secrets, defaults
Dockerfile          python:3.12-slim image (git for deep_analysis clones)
helm/herald/        Chart: CronJob + ConfigMap (config) + Secret (webapi keys)
```

The `digest` command ports the `herald-rate-pr` and `herald-analyze` skill
prompts into Python (`RATE_PR_SYSTEM_PROMPT`, `ANALYZE_SYSTEM_PROMPT`) and calls
`AnthropicClient` — keep those prompts in sync with the skill markdown.

## Python CLI

```bash
pip install -r requirements.txt

python herald.py fetch --team <name> [--days N] [--output FILE] [--force]
python herald.py digest --team <name> [--days N] [--output FILE] [--force] [--post]
python herald.py list-teams
python herald.py validate
python herald.py post --team <name> < digest.md   # Teams webhook from secrets
```

## LLM endpoint (for `digest`)

Configurable via env (override) or `defaults.ai_backend` in config:

- `ANTHROPIC_BASE_URL` — endpoint base; defaults to `https://api.anthropic.com` (set to a proxy/gateway URL if needed)
- `ANTHROPIC_API_KEY` — API key
- `ANTHROPIC_MODEL` — e.g. `claude-opus-4-8`
- `ANTHROPIC_CUSTOM_HEADERS` — extra headers as `Key: Value` lines, for gateways that need custom auth

## Container & Kubernetes

```bash
docker build -t herald:poc .
helm install herald ./helm/herald \
  --set secrets.anthropicApiKey=$ANTHROPIC_API_KEY \
  --set secrets.githubToken=$GITHUB_TOKEN \
  --set-string secrets.teamWebhooks.my-team=$WEBHOOK_URL
# Behind a gateway, also: --set secrets.anthropicCustomHeaders="X-Auth-Header: <value>"
```

Team config jsons live in the **ConfigMap** (`values.config`); webapi keys and
per-team webhooks live in the **Secret**. The CronJob runs `herald.py digest`.

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
