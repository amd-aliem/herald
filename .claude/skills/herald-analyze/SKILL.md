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
3. **Triage repos** — Sort repos by combined relevance: sum of `_herald_relevance.score` across PRs, then by total item count. Drop repos with zero meaningful activity (only bot commits, no PRs/issues/releases). Use `meta.stats` for quick counts.
4. For each kept repository, summarize commits, PRs, issues, and releases. Commits that duplicate PR merge/head SHAs are already removed by `herald.py fetch`.
   - **PRs with `_herald_relevance`**: include `[RELEVANCE: N]` and weave the reason into the summary sentence. Lead with score 4-5 items.
   - **PRs with `_herald_diff`**: reference key changed files or subsystems; don't paste raw diff.
   - **`_herald_comments`**: surface review blockers or contentious discussion in the summary sentence. Don't list comments individually.
   - **`_herald_repo_context`**: use project docs (README, ARCHITECTURE) to explain *why* a change matters in context.
   - **Standalone commits** (after dedup): group minor commits into a single "N other commits" line. Only give individual bullets to notable standalone commits.
5. Cap at ~8 bullets per repo. Combine related issues/PRs where possible.
6. Repos with only 1-2 minor items: fold into a "**Minor activity**" section at the end instead of giving them a full heading.

## Output format

```
**TL;DR:** <2-3 sentences leading with items matching priorities[0], then key themes>

### owner/repo
- [HIGH PRIORITY] **Title** [STATUS] ([#N](url)) PR by @author — What changed and why it matters
- **Title** [STATUS] ([#N](url)) PR by @author — What changed and why it matters
- **Title** [STATUS] ([#N](url)) Issue — What the issue reports
- **Title** [STATUS] ([#N](url), +N more) Summary — Grouped description
- **Title** [RELEASED] ([vX.Y.Z](url)) Release
- **Title** Commit (abc1234) — What the commit does
- N other commits including X, Y, and Z.

### owner/repo
...

### Minor activity
- **owner/repo**: brief note

### Recommended Actions
- **Action title** — Recommendation with relevant links
```

## Format rules

- `[STATUS]` values (square brackets required): `[MERGED]`, `[OPEN]`, `[CLOSED]`, `[RELEASED]`
- **Item type** label is required after the link/status: `PR`, `Issue`, `Summary`, `Release`, `Commit`, or `Commits`
  - PRs: `[STATUS] ([#N](url)) PR by @author`
  - Issues: `[STATUS] ([#N](url)) Issue`
  - Grouped items: `[STATUS] ([#N](url), +N more) Summary`
  - Releases: `[RELEASED] ([tag](url)) Release`
  - Commits: `Commit (sha)` or `Commits (sha, sha)`
- `by @author` is required for PRs; omit for issues, summaries, releases, and commits
- Start directly with `**TL;DR:**` — no preamble, heading, or sign-off
- `###` headings for each active repo and Recommended Actions
- Items matching `team_context.priorities[0]` go first in their repo section, tagged `[HIGH PRIORITY]`
- Em dash (` — `) separating type/attribution from context sentence is required
- 1-3 recommended actions; fewer is fine. Actions should be specific ("review PR #X for security implications") not generic
