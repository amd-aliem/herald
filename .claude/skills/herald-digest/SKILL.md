---
name: herald-digest
description: Generate a full Herald digest for a team using fetch and analysis skills.
allowed-tools: Bash(python herald.py *), Read, Write, Skill
user-invocable: true
---

# /herald-digest — Full Herald Run

Orchestrate a complete Herald cycle for one team.

## Arguments

`<team-name>` optional flags: `--days N`, `--force`, `--output FILE`

Do **not** pass `--post` or post to Teams unless the user explicitly requests delivery.

## Procedure

1. Detect herald repo root (directory containing `herald.py`).
2. Run fetch:
   ```bash
   python herald.py fetch --team <team> [--days N] [--force] -o .cache/activity-<team>.json
   ```
3. Read `.cache/activity-<team>.json`.
4. For each PR in `activity[].pulls` that has `_herald_diff` and lacks `_herald_relevance`:
   - Invoke `/herald-rate-pr` with `{ pr, repo, team_context }` from the envelope
   - Write `score` and `reason` back into the PR as `_herald_relevance`
5. Update the activity JSON file with ratings (optional but recommended).
6. Invoke `/herald-analyze` with the activity JSON path.
7. Save outputs:
   - Digest markdown to `reports/<team>/herald-digest-<timestamp>.md` (or `--output` path)
   - Copy activity JSON alongside as `reports/<team>/herald-activity-<timestamp>.json` if useful
8. Print a short summary: team name, repo count, digest path.

## Notes

- Use cached fetch data when re-running analysis without `--force`.
- If fetch returns empty activity, report clearly and stop.
- Never post to Teams/webhooks unless the user explicitly asks.
