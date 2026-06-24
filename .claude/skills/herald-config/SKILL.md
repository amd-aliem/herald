---
name: herald-config
description: Interactively build or extend Herald configuration through conversational Q&A.
allowed-tools: Read, Write, Bash(mkdir *), Bash(ls *), Glob, Grep, AskUserQuestion
user-invocable: true
---

# /herald-config — Interactive Herald Configuration Generator

Walks the user through building a valid Herald configuration via `AskUserQuestion`. Handles fresh setup and adding teams to an existing `config/herald.json`.

## Project Directory

Detect the herald repo by walking up from the cwd until you find `herald.py` and `config/herald.example.json`. All paths below are relative to that root.

## Config Schema

See `config/herald.example.json`, `config/teams/team.example.json`, and `config/secrets/secrets.example.json` for the canonical schema.

Key points:
- Main config: `config/herald.json` with `defaults` and `teams[]`
- Per-team config: `config/teams/<name>.json` with `sources`, `display_name`, `focus_areas`, `priorities`
- Secrets: `config/secrets/<name>.json` with `teams_webhook_url`

## Procedure

1. Detect existing config (`config/herald.json`).
2. Gather team name (slug), display name, repositories, focus areas, priorities.
3. Optionally gather filters, diff_keywords, fetch_comments, deep_analysis, webhook URL.
4. Write `config/teams/<name>.json`, optional `config/secrets/<name>.json`, update `config/herald.json`.
5. Suggest: `python herald.py validate` and `/herald-digest <team-name>`.

## Validation

- Team name: `^[a-z0-9][a-z0-9-]*$`
- Repository: `owner/repo` format, at least one required
- Never overwrite existing team files without confirmation
