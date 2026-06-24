---
name: herald-post
description: Post a Herald digest markdown file to Microsoft Teams.
allowed-tools: Bash(python herald.py *), Read
user-invocable: true
---

# /herald-post — Teams Delivery

Post a digest markdown file to Microsoft Teams via Power Automate webhook.

## Arguments

`<digest-file>` and either `--team TEAM` (loads webhook from `config/secrets/<team>.json`) or `--webhook-url URL`.

Only run when the user explicitly requests Teams delivery.

## Procedure

1. Verify the digest file exists and is non-empty.
2. Resolve webhook URL:
   - `--team <name>` → read `config/secrets/<name>.json` for `teams_webhook_url`
   - or use `HERALD_TEAMS_WEBHOOK` env var
   - or `--webhook-url` argument
3. Post via the Python helper (Adaptive Card conversion stays in Python):
   ```bash
   python herald.py post --team <team> < digest.md
   ```
   or
   ```bash
   python herald.py post --webhook-url "$URL" < digest.md
   ```
4. Report success or HTTP error to the user.

## Safety

Do not post unless explicitly requested. Never log or echo the full webhook URL.
