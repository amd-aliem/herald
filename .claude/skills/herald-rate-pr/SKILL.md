---
name: herald-rate-pr
description: Rate a pull request's relevance to a team on a 1-5 scale.
allowed-tools: Read
user-invocable: false
---

# /herald-rate-pr — PR Relevance Rating

Rate how relevant a single PR is to a team's focus areas and priorities.

## Input

JSON with:
- `pr` — PR object (title, body, user, state, `_herald_diff`, `_herald_diff_type`, etc.)
- `repo` — repository name (`owner/repo`)
- `team_context` — `{ name, focus_areas, priorities }`

## Procedure

1. Read the input JSON.
2. Evaluate relevance using these signals in priority order:
   a. **Priorities match** — Does the PR touch `team_context.priorities`? A match with `priorities[0]` pushes toward 5.
   b. **Focus areas match** — Check title, body, and diff file paths against each `focus_areas` entry.
   c. **Diff content** — If `_herald_diff` is present, scan changed file paths and code for team-relevant subsystems.
   d. **Title/body keywords** — Look for terms that align with the team's domain.
3. When `_herald_diff` is absent or `_herald_diff_type` is `"truncated"`, rate based on title/body/file summary only. Do not penalize for missing diff data.
4. Rate from the team's perspective, not general importance. A critical kernel fix is a 2 for a networking team if it doesn't touch networking.

## Scale

- **5 = critical** — Directly impacts the team's top priority; requires attention
- **4 = relevant** — Clearly within focus areas; team should be aware
- **3 = somewhat relevant** — Touches adjacent areas; useful context
- **2 = tangential** — Loosely related; skim-worthy at best
- **1 = unrelated** — No connection to team focus

## Output

Respond with **exactly**:

```
RELEVANCE: <1-5>
REASON: <one sentence>
```

No other text. The caller attaches this as `pr._herald_relevance = { "score": N, "reason": "..." }`.
