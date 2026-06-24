---
name: herald-digest
description: Generate a full Herald digest for a team using fetch and analysis skills.
allowed-tools: Bash(python herald.py *), Bash(mkdir *), Read, Write, Skill
user-invocable: true
---

# /herald-digest — Full Herald Run

Orchestrate a complete Herald cycle for one team.

## Arguments

`<team-name>` optional flags: `--days N`, `--force`, `--output FILE`

Do **not** post to Teams unless the user explicitly requests delivery.

## Procedure

1. Detect herald repo root (directory containing `herald.py`).
2. **Fetch activity**:
   ```bash
   python herald.py fetch --team <team> [--days N] [--force] -o .cache/activity-<team>.json
   ```
   If fetch exits non-zero, show the stderr output and stop.
   If the activity array is empty, tell the user "No activity found for `<team>` in the last N days" and stop.
   The envelope includes `meta.stats` with `repos`, `pulls`, `pulls_with_diffs`, `commits`, `issues`, `releases` counts.
   Print: "Fetched activity for <team> — N repos, N PRs, N commits"

3. **Rate PRs** — Read `.cache/activity-<team>.json`. For each PR in `activity[].pulls`:
   - Skip if `_herald_diff` is absent (no diff data to rate).
   - Skip if `_herald_relevance` already exists (already rated).
   - Build input: `{ "pr": <pr object>, "repo": "<owner/repo>", "team_context": <meta.team_context> }`
   - Invoke `/herald-rate-pr` with that input.
   - Parse the response: extract the integer after `RELEVANCE:` and the text after `REASON:`.
   - Write back: `pr._herald_relevance = { "score": N, "reason": "..." }`
   - If a single rating fails, log a warning and continue with remaining PRs.
   After the loop, write the updated JSON back to `.cache/activity-<team>.json`.
   Print: "Rated N PRs (skipped M already-rated)"

4. **Generate digest** — Invoke `/herald-analyze` with the activity JSON path.

5. **Save outputs**:
   ```bash
   mkdir -p reports/<team>
   ```
   - Write digest markdown to `reports/<team>/herald-digest-YYYY-MM-DD.md` (or `--output` path).
   - Timestamp format: `YYYY-MM-DD` (date only, e.g. `2026-06-24`).
   Print: "Digest saved to <path>"

6. **Summary** — Print final status: team name, repo count, PR count rated, digest file path.

## Notes

- Forward `--days` and `--force` to the fetch command; they are not consumed by this skill.
- Without `--force`, fetch uses cached data (1-hour TTL). This is fine for re-running analysis.
- Never post to Teams/webhooks unless the user explicitly asks.
