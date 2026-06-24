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
2. Consider PR title, description, state, and diff data (full diff, file summary, or truncated).
3. Rate relevance 1-5:
   - 1 = unrelated
   - 2 = tangential
   - 3 = somewhat relevant
   - 4 = relevant
   - 5 = critical

## Output

Respond with **exactly**:

```
RELEVANCE: <1-5>
REASON: <one sentence>
```

No other text. The caller attaches this as `pr._herald_relevance = { "score": N, "reason": "..." }`.
