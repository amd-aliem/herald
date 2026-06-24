---
name: herald-analyze
description: Analyze Herald activity JSON and produce a digest markdown report.
allowed-tools: Read
user-invocable: false
---

# /herald-analyze — Digest Generation

Read a Herald activity JSON envelope (from `herald fetch`) and produce the final digest markdown.

## Input

A JSON file with `{ "meta": { team, team_context, time_window_days, since, ... }, "activity": [...] }`.

## Procedure

1. Read the activity JSON file path provided by the caller.
2. Extract `meta.team_context` (name, focus_areas, priorities) and `activity` array.
3. For each repository in `activity`, summarize commits, PRs, issues, and releases.
   - Deduplicate commits whose SHAs appear in PR merge commits or head SHAs.
   - For PRs with `_herald_relevance`, include score and reason.
   - For PRs with `_herald_diff`, include diff or file summary per `_herald_diff_type`.
   - For items with `_herald_comments`, include recent comments.
   - For `_herald_repo_context`, use project docs to enrich analysis.
4. Write the digest using **exactly** this output format:

```
**TL;DR:** <2-3 sentence paragraph summarizing key themes across all repos>

### owner/repo
- **Title** [STATUS] ([#N](url)) by @author — One sentence on what changed and why it matters

(repeat per active repository; skip repos with negligible activity)

### Recommended Actions
- **Action title** — One sentence with recommendation and links
```

## Format rules

- STATUS: MERGED, OPEN, CLOSED, or RELEASED
- Start directly with `**TL;DR:**` — no preamble or sign-off
- Use `###` headings for each active repo and Recommended Actions
- If `team_context.priorities[0]` exists, put related items first and tag with `[HIGH PRIORITY]` before the bold title
- Em dash (` — `) between attribution and context sentence is required on activity bullets
- Up to 3 recommended actions; fewer is fine

## Output

Return only the digest markdown. Write to the path specified by the caller if provided.
