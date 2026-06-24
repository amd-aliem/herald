# Herald Simplification: Skills-Based Architecture Plan

## Status

**Draft for review** | June 2026

## Problem Statement

Herald is a 4,848-line single-file Python application. Over time, it has accumulated complexity in three areas that are increasingly hard to maintain:

1. **Prompts embedded in code** (~200 lines of string concatenation in `format_prompt()` and `_build_rating_prompt()`) -- these are hard to iterate on, hard to read, and tightly coupled to the data-fetching pipeline.
2. **A full TUI** (~1,700 lines, 35% of the codebase) -- a Textual config editor that duplicates what the `/herald-config` skill already does better conversationally.
3. **Monolithic orchestration** -- fetching, filtering, enriching, prompting, post-processing, and delivery are all wired together in one class with 40+ methods.

The goal is to simplify Herald's code footprint while making the AI-facing parts (prompts, output formatting, delivery) easier to evolve independently of the data pipeline.

## Proposed Architecture

### Core Idea

Split Herald into two layers:

```
┌─────────────────────────────────────────────────┐
│  Skills Layer (SKILL.md files)                  │
│  - herald-digest:  orchestrate a full run       │
│  - herald-analyze: digest prompt + output rules │
│  - herald-rate-pr: PR relevance rating prompt   │
│  - herald-config:  (already exists)             │
│  - herald-post:    Teams delivery               │
├─────────────────────────────────────────────────┤
│  Data Layer (Python CLI)                        │
│  herald.py  -- fetch, cache, filter, output     │
│              (no AI prompts, no TUI)            │
└─────────────────────────────────────────────────┘
```

**Skills own all AI interaction.** Python owns all data fetching and caching. The boundary is a JSON file on disk (or stdout) -- the Python CLI fetches and writes structured activity data; skills read it and invoke Claude with the right prompts.

### What Changes

| Area | Current | Proposed |
|------|---------|----------|
| Digest prompt | `format_prompt()` -- 160 lines of string concat in Python | `herald-analyze` skill -- prompt lives in markdown, reads activity JSON |
| PR rating prompt | `_build_rating_prompt()` -- 30 lines in Python | `herald-rate-pr` skill -- reads per-PR JSON, writes rating |
| Output stripping | `strip_conversational_output()` | Eliminated -- skill prompt controls output format directly |
| TUI config editor | `ConfigApp` + `ConfigManager` -- 1,700+ lines | Removed -- `/herald-config` skill already handles this |
| Rich CLI fallback | `ConfigManager` -- 500 lines | Removed (same reason) |
| Teams posting | `post_to_teams()` + `markdown_to_adaptive_card_blocks()` -- 250 lines | `herald-post` skill or keep as thin Python (see trade-offs) |
| AI backend abstraction | `AIBackend` / `ClaudeCLIBackend` / registry -- 120 lines | Removed -- skills invoke Claude natively |
| Web config UI | `herald_web.py` (separate file) | Removed -- replaced by skill |
| Legacy migration | `_migrate_config_layout()` | Keep for one more release, then remove |

### What Stays in Python

`herald.py` becomes a focused data CLI (~1,000-1,500 lines estimated):

- **GitHub API client**: `GitHubSource` class (fetch, paginate, cache, rate-limit handling)
- **Activity enrichment**: diff fetching, comment fetching, deep analysis (repo cloning)
- **Filtering**: exclude authors/titles/labels
- **Config loading**: JSON config with team resolution, sub-teams, secrets
- **Caching**: file-based cache with TTL and pruning
- **CLI interface**: `argparse` entry point with `--fetch-only` as the primary mode
- **Structured output**: write activity data as JSON to stdout or a file

The Python CLI gains one new primary command:

```bash
# Fetch activity and write structured JSON
python herald.py fetch --team occ-team --days 14 --output .cache/activity-occ-team.json

# Or to stdout
python herald.py fetch --team occ-team | jq .
```

### New Skills

#### `/herald-digest` (orchestrator)

Top-level skill that runs a full Herald cycle:

1. Calls `python herald.py fetch --team <team> --output <tmpfile>`
2. Reads the JSON output
3. Invokes `/herald-analyze` with the activity data + team context
4. Optionally invokes `/herald-rate-pr` for PRs with diffs
5. Optionally invokes `/herald-post` to deliver to Teams
6. Saves the detailed report

```yaml
---
name: herald-digest
description: Generate a Herald digest for a team
allowed-tools: Bash(python herald.py *), Read, Write, Skill
user-invocable: true
args: <team-name> [--days N] [--post]
---
```

#### `/herald-analyze` (digest generation)

Contains the full analysis prompt that currently lives in `format_prompt()`. The prompt text lives directly in the SKILL.md file, making it trivially editable. The skill:

1. Reads the activity JSON file
2. Reads team context from config
3. Constructs the prompt (template lives in the skill file itself)
4. Claude processes it inline (no subprocess needed -- the skill IS the Claude interaction)
5. Outputs the formatted digest

```yaml
---
name: herald-analyze
description: Analyze Herald activity data and produce a digest
allowed-tools: Read, Bash(python herald.py *)
user-invocable: false
---
```

The key insight: since skills run inside Claude, the "prompt" is just the skill's procedure text plus the data it reads. No string concatenation. No subprocess. No prompt engineering in Python.

#### `/herald-rate-pr` (PR relevance rating)

Replaces `_build_rating_prompt()` and `_rate_single_pr()`. The skill:

1. Receives a PR data blob (JSON) and team context
2. Rates it 1-5 with a reason
3. Returns structured output

```yaml
---
name: herald-rate-pr
description: Rate a PR's relevance to a team
allowed-tools: Read
user-invocable: false
---
```

#### `/herald-post` (Teams delivery)

Handles markdown-to-Adaptive Card conversion and webhook posting. Two options here (see trade-offs):

**Option A**: Keep as Python -- the Adaptive Card format is fiddly and benefits from programmatic generation.

**Option B**: Move to a skill -- Claude can generate Adaptive Card JSON directly from the digest markdown, and `curl` handles the POST.

```yaml
---
name: herald-post
description: Post a Herald digest to Microsoft Teams
allowed-tools: Bash(curl *), Read
user-invocable: true
args: <digest-file> [--team TEAM]
---
```

### Data Flow (New)

```
User runs:  /herald-digest occ-team --days 14

  ┌──────────────────────┐
  │  /herald-digest      │  (skill - orchestrator)
  └──────┬───────────────┘
         │
         ▼
  python herald.py fetch --team occ-team --days 14
         │
         ▼
  ┌──────────────────────┐
  │  Activity JSON       │  written to .cache/ or stdout
  │  (structured data)   │
  └──────┬───────────────┘
         │
    ┌────┴──────────────┐
    │                   │
    ▼                   ▼
  /herald-rate-pr    /herald-analyze
  (per-PR, if diffs) (digest prompt)
    │                   │
    └────┬──────────────┘
         │
         ▼
  ┌──────────────────────┐
  │  Final Digest (md)   │
  └──────┬───────────────┘
         │
         ▼ (optional)
  /herald-post → Teams webhook
```

### Python CLI Surface After Simplification

```
herald.py fetch   [--team T] [--days N] [--repos r1,r2] [--output FILE] [--force]
                  Fetch activity → JSON

herald.py list-teams
                  Print configured teams

herald.py validate
                  Check config and prerequisites
```

Everything else (`--teams`, `--prompt-only`, `--dry-run`, `config` subcommand, `serve` subcommand) is removed. Those capabilities are handled by skills or are no longer needed.

## Migration Path

### Phase 1: Add `fetch` command and JSON output

- Add `fetch` subcommand to `herald.py` that writes structured activity JSON
- Define the JSON schema (basically what `fetch_activity()` already returns, serialized)
- Keep all existing functionality working -- this is purely additive

### Phase 2: Create skills

- Write `herald-analyze` skill with the prompt from `format_prompt()`
- Write `herald-rate-pr` skill with the prompt from `_build_rating_prompt()`
- Write `herald-digest` orchestrator skill
- Decide on `herald-post` approach (Option A or B)
- Install skills via symlinks to `~/.claude/skills/`
- Test end-to-end: `/herald-digest occ-team` produces equivalent output

### Phase 3: Remove dead code from Python

- Remove `format_prompt()`, `_build_rating_prompt()`, `_rate_single_pr()`, `rate_pr_diffs()`
- Remove `AIBackend`, `ClaudeCLIBackend`, `AI_BACKEND_REGISTRY`
- Remove `strip_conversational_output()`
- Remove `generate_summary()`, `generate_digest()`, `generate_team_digest()`
- Remove entire TUI (`ConfigApp`, `ConfigManager`, `ConfigStore`, all Textual widgets)
- Remove `_capture_validation_output()`, `_colorize_validation_line()`
- Remove `herald_web.py` and Flask dependency
- Remove `textual`, `rich` from dependencies

### Phase 4: Clean up

- Remove legacy config migration code (after sufficient adoption time)
- Remove backward-compat CLI aliases (`--group`, `--list-groups`)
- Update CLAUDE.md to reflect new architecture
- Update config examples

## Trade-offs

### Gains

| Benefit | Detail |
|---------|--------|
| **~3,000 fewer lines of Python** | TUI (1,700), AI plumbing (350), prompt construction (200), web UI (700+) |
| **Prompts are editable markdown** | Anyone can tweak digest style by editing a SKILL.md file -- no Python knowledge needed |
| **No AI subprocess management** | Skills run inside Claude natively; no timeout/retry/debug-prompt logic |
| **Simpler dependencies** | Drop `textual`, `rich`, `flask`; keep only `requests`, `python-dateutil` |
| **Better separation of concerns** | Python = data; Skills = AI interaction; Config = `/herald-config` skill |
| **Easier testing** | Data pipeline (Python) can be tested with fixture JSON; skills can be tested by invoking them |
| **Deployment simplification** | `pip install requests python-dateutil` + symlink skills. No TUI deps, no web server |

### Costs

| Cost | Detail |
|------|--------|
| **Requires Claude Code** | Skills only work in Claude Code sessions. Can't run Herald from a bare cron job without Claude Code being the orchestrator. |
| **Sequential PR rating** | Current code rates PRs concurrently via `ThreadPoolExecutor`. Skills would rate them sequentially (one Claude turn per PR). Mitigations: batch them in a single prompt, or accept the latency. |
| **Two-step invocation** | Users type `/herald-digest team` instead of `python herald.py --team team`. This is actually simpler UX but is a change. |
| **Adaptive Card complexity** | If moved to a skill, the markdown→Adaptive Card conversion may be less reliable than the current programmatic approach. Recommendation: keep as Python utility function callable from the skill. |
| **Skill maintenance** | A new artifact type to maintain (SKILL.md files). However, these are simpler than the Python code they replace. |

### Open Questions

1. **PR rating concurrency**: Should we batch all PRs into a single rating prompt, or accept sequential rating? A single prompt would be simpler but may hit context limits with many diffs.

2. **Adaptive Card generation**: Keep as Python helper (`python herald.py format-card < digest.md`) or have Claude generate it inline? Python is more reliable for the structured JSON format.

3. **Cron/automation**: For unattended runs (cron, CI), do we need a wrapper script that launches Claude Code with the `/herald-digest` skill? Or do we keep a minimal `herald.py run` command that calls Claude CLI as a subprocess (essentially reverting to the current model for automated runs)?

4. **Skill installation**: Should Herald skills live in the herald repo and be symlinked by an install script (like chive), or should they be distributed differently?

## Estimated Scope

| Component | Effort |
|-----------|--------|
| JSON output format + `fetch` command | Small |
| `herald-analyze` skill | Medium (prompt extraction + testing) |
| `herald-rate-pr` skill | Small |
| `herald-digest` orchestrator skill | Medium |
| `herald-post` decision + implementation | Small-Medium |
| Python code removal | Small (mostly deletion) |
| CLAUDE.md update | Small |
| Testing + validation | Medium |

## Appendix: Current Code Breakdown

```
herald.py (4,848 lines)
├── Imports + constants + helpers          60 lines
├── ActivitySource (ABC)                   50 lines
├── GitHubSource                          600 lines  ← stays
│   ├── API client + caching              200
│   ├── Diff fetching                     100
│   ├── Comment fetching                  100
│   ├── Deep analysis                     100
│   └── Filtering                         100
├── AIBackend + ClaudeCLIBackend          120 lines  ← removed
├── Herald (orchestrator)                1,400 lines ← most removed
│   ├── Config loading + teams            400        ← stays (in fetch command)
│   ├── Prompt construction               200        ← moved to skill
│   ├── PR relevance rating               120        ← moved to skill
│   ├── Summary generation + caching      100        ← removed
│   ├── Report saving + raw formatting    100        ← stays (simplified)
│   ├── Teams posting + Adaptive Cards    250        ← moved or kept
│   └── Digest orchestration              230        ← moved to skill
├── Validation helpers                     50 lines  ← stays
├── ConfigStore                           100 lines  ← removed
├── ConfigManager (Rich CLI)              500 lines  ← removed
├── ConfigApp (Textual TUI)             1,700 lines  ← removed
├── Config migration                      100 lines  ← removed (after grace period)
└── main()                                200 lines  ← simplified
```

**Estimated final size**: ~1,000-1,500 lines of Python (data pipeline + CLI) + 4-5 SKILL.md files (~200-400 lines total).
