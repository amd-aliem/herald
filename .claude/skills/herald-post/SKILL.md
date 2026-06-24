---
name: herald-post
description: Post a Herald digest markdown file to Microsoft Teams.
allowed-tools: Bash(python herald.py *), Read, AskUserQuestion
user-invocable: true
---

# /herald-post — Teams Delivery

Post a digest markdown file to Microsoft Teams via Power Automate webhook.

## Arguments

`<digest-file>` and either `--team TEAM` or `--webhook-url URL`.

Only run when the user explicitly requests Teams delivery.

## Procedure

1. **Validate digest** — Read the file. Verify it exists and is non-empty. The Python `post` command also warns if the content doesn't start with `**TL;DR:**`.

2. **Resolve webhook** (first match wins):
   - `--webhook-url URL` — use directly
   - `--team <name>` — read `config/secrets/<name>.json` for `teams_webhook_url`
   - `HERALD_TEAMS_WEBHOOK` env var
   - If none found, tell the user and stop.

3. **Confirm** — Use AskUserQuestion: "Post digest `<filename>` to `<team>` Teams channel?" with options Yes / No. Stop if declined.

4. **Post**:
   ```bash
   python herald.py post --team <team> < <digest-file>
   ```
   or with explicit URL:
   ```bash
   python herald.py post --webhook-url "$URL" < <digest-file>
   ```

5. **Report result** — If exit code 0, confirm success. Otherwise, show the error output from stderr.

## Safety

- Never post without explicit user request AND confirmation.
- Never log or echo the full webhook URL.
