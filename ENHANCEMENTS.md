# Herald Enhancement Opportunities

## Guiding Principles

These enhancements are organized around two goals stated by the project owner:

1. **Maximize important information per unit of reading time** -- every feature should increase signal density or reduce noise
2. **Very configurable, but easy to start** -- zero-config defaults with progressive disclosure of power-user options

---

## 1. Output Quality: Higher Signal, Less Reading

### 1.1 Configurable Output Profiles

Currently the prompt hardcodes "3-7 bullet points" and a fixed output structure. Allow groups to define an `output_profile` that controls summary depth and format.

- `"executive"` -- 3-5 bullets, no links, one-line recommended actions. Designed for leadership or cross-team channels.
- `"engineering"` (default) -- current format with links, descriptions, and full recommended actions.
- `"detailed"` -- expanded analysis with per-PR commentary, risk assessments, and contributor trend notes.

This is a config-only change: each profile maps to a different prompt template. Teams choose their profile in the group JSON; no CLI flags needed for normal use.

### 1.2 Diff-Against-Last-Digest (Delta Mode)

The biggest reading-time problem with recurring digests is re-reading about things you already saw. Herald should track what was reported in previous runs and instruct the AI to:

- Mark items that appeared in a prior digest as `[CONTINUING]`
- Highlight genuinely new items as `[NEW]`
- Call out items that changed state since last run (e.g., PR went from open to merged) as `[UPDATED]`

Implementation: persist a lightweight manifest (PR/issue numbers, commit SHAs, states) alongside each summary cache file. Feed the prior manifest into the prompt so the AI can diff.

### 1.3 Severity / Relevance Scoring

Add a numeric or tiered relevance score (`HIGH` / `MEDIUM` / `LOW`) to each bullet, driven by configurable scoring rules in `team_context`:

```json
"scoring_rules": {
  "high": ["security", "breaking change", "SEV-SNP", "attestation"],
  "low": ["dependabot", "typo", "ci fix", "chore"]
}
```

The AI applies these rules during summarization. Readers can then skim `HIGH` items in under 30 seconds and skip `LOW` entirely.

### 1.4 TL;DR Header

Prepend a single-sentence TL;DR line above the `### Summary` section. This gives readers who receive the Teams card a one-glance answer to "do I need to read this right now?"

Example: *"2 high-priority SEV-SNP attestation changes merged; 1 breaking API change in KBS config. No security patches."*

### 1.5 Configurable Output Sections

Allow groups to enable/disable output sections via config:

```json
"output_sections": ["tldr", "summary", "recommended_actions", "contributor_stats", "raw_counts"]
```

Teams that only want the summary can drop recommended actions. Teams that want contributor tracking can add it. The prompt is assembled dynamically from the enabled sections.

---

## 2. Additional Activity Sources (Portability)

### 2.1 GitLab Source

Implement `GitLabSource(ActivitySource)` using the GitLab REST API. Many organizations run internal GitLab instances or track GitLab-hosted open-source projects (e.g., Mesa, kernel subsystems on gitlab.freedesktop.org). The `SOURCE_REGISTRY` pattern already supports this -- add `"gitlab": GitLabSource`.

Config addition:
```json
{
  "type": "gitlab",
  "base_url": "https://gitlab.com",
  "repositories": ["group/project"],
  "private_token_env": "GITLAB_TOKEN"
}
```

### 2.2 Gitea / Forgejo Source

Gitea and Forgejo are increasingly common for self-hosted projects and some open-source ecosystems. Their API is largely GitHub-compatible, making implementation straightforward.

### 2.3 Mailing List / Discourse Source

Many upstream projects (especially Linux kernel subsystems, OpenSSL, etc.) make decisions on mailing lists, not GitHub. A source that fetches from public mailing list archives (Lore, Pipermail) or Discourse forums and feeds the content into the AI summarizer would capture activity that GitHub-only monitoring misses entirely.

### 2.4 RSS / Atom Feed Source

A generic feed source would let users monitor project blogs, release announcement pages, security advisory feeds (e.g., GitHub Security Advisories, CVE feeds), and changelog pages without writing a custom source class.

---

## 3. Additional AI Backends (Portability)

### 3.1 Anthropic API Backend

Replace subprocess calls with direct API usage via the `anthropic` Python SDK. Benefits:

- No dependency on Claude CLI being installed
- Model selection in config (`claude-sonnet-4-20250514`, `claude-opus-4-20250514`, etc.)
- Explicit token usage tracking and cost reporting per run
- Streaming support for real-time progress feedback

### 3.2 OpenAI-Compatible Backend

Support any OpenAI-compatible API endpoint (OpenAI, Azure OpenAI, Ollama, vLLM, etc.). This is the single biggest portability unlock -- organizations that cannot use Claude can still run Herald.

```json
"ai_backend": {
  "type": "openai",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "api_key_env": "OPENAI_API_KEY"
}
```

### 3.3 Local / Ollama Backend

For air-gapped or cost-sensitive environments, support local LLM inference via Ollama. Output quality will vary with model size, but the option removes external API dependencies entirely.

---

## 4. Delivery Channels (Growing Audience)

### 4.1 Slack Integration

Slack is more prevalent than Teams in many organizations and across most open-source communities. Add a `slack_webhook_url` option per group, with markdown-to-Slack-blocks conversion analogous to the existing Adaptive Card builder.

### 4.2 Email Digest

Generate and send an HTML email digest. Useful for stakeholders who do not use Teams/Slack or for distribution lists that cross tool boundaries.

```json
"delivery": {
  "email": {
    "smtp_server": "smtp.example.com",
    "recipients": ["team@example.com"],
    "subject_template": "Herald: {group_name} - {date}"
  }
}
```

### 4.3 Discord Webhook

Discord is widely used in open-source communities. A Discord webhook delivery target would let open-source project maintainers use Herald to keep their contributor communities informed.

### 4.4 Static Site / HTML Report

Generate a self-contained HTML page per digest run and optionally publish to GitHub Pages, S3, or a local directory. This creates a browsable archive of digests and makes Herald useful as a project status dashboard for public-facing repos.

### 4.5 GitHub Discussions / Issues

Post digests as GitHub Discussion posts or Issue comments. This keeps the intelligence within the platform where the team is already working.

### 4.6 Generalized Webhook

Support a generic `POST` webhook with configurable payload templates. This covers any integration not explicitly supported (PagerDuty, Jira, custom dashboards, etc.) without Herald needing to know the target system's format.

---

## 5. Ease of Getting Started

### 5.1 Interactive Setup Wizard (`herald init`)

Add a `--init` CLI command that walks the user through setup interactively:

1. Ask for GitHub repos to watch (autocomplete with `gh` CLI if available)
2. Ask for team name and focus areas
3. Ask which delivery channels to configure
4. Generate the `herald.config.json` and `groups/*.json` files
5. Validate GitHub token, AI backend availability, and webhook connectivity
6. Run a test digest on a short time window to confirm everything works

This eliminates the need to read documentation or copy example files to get started.

### 5.2 `herald validate`

A `--validate` flag that checks the full config without running a digest:

- Config file syntax and schema validation
- GitHub token presence and rate limit status
- Repository accessibility (do they exist? are they public?)
- AI backend availability (is Claude CLI installed? is the API key set?)
- Webhook URL connectivity test (POST a test message)
- Report which groups are fully configured vs. which have issues

### 5.3 Minimal Single-Command Run

Support running with zero config files:

```bash
herald --repos owner/repo1,owner/repo2
```

This already partially works but requires Claude CLI. Add sensible messaging when Claude CLI is missing, pointing the user to setup options.

### 5.4 PyPI / pipx Installation

Publish Herald as a Python package so users can install with `pip install herald-digest` or `pipx install herald-digest`. Consolidate dependencies into `pyproject.toml`, add a console script entry point, and handle config file discovery from `~/.config/herald/` in addition to CWD.

---

## 6. Scheduling and Automation

### 6.1 Built-in Cron Expression Support

Add a `schedule` field to group configs:

```json
"schedule": "0 8 * * MON-FRI"
```

Herald could run in daemon mode (`herald --daemon`) using a lightweight scheduler (e.g., `schedule` library or just `time.sleep` loop with cron parsing), or generate crontab / systemd timer entries with `herald schedule install`.

### 6.2 GitHub Actions Workflow Template

Provide a ready-to-use `.github/workflows/herald.yml` that runs Herald on a schedule and posts to configured channels. Users copy it into their repo and set secrets -- no server needed.

### 6.3 Container Image

Publish a Docker image so Herald can run in any container orchestrator (Kubernetes CronJob, ECS Scheduled Task, etc.) without Python environment setup.

---

## 7. Intelligence Depth

### 7.1 Cross-Group Correlation

When multiple groups are configured, run a second AI pass that looks for cross-cutting themes:

- A dependency update in one repo that affects another repo's functionality
- The same contributor making related changes across repositories
- Coordinated release activity suggesting an upstream milestone

This produces a "Cross-Group Intelligence" section that no single-group digest would surface.

### 7.2 Trend Analysis Over Time

Store structured digest metadata (item counts, severity distribution, top contributors, key topics) in a local SQLite database. After N runs, Herald can include trend lines:

- "PR volume is up 40% over the last 4 weeks"
- "Security-related activity increased -- 3 CVE-related PRs this period vs. 0 in the prior 3"
- "New contributor X has submitted 5 PRs in the last 2 weeks -- potential upstream partner"

### 7.3 Contributor Watch Lists

Allow groups to define contributors of interest:

```json
"watch_contributors": {
  "competitors": ["user1", "user2"],
  "partners": ["user3"],
  "team": ["our-dev1", "our-dev2"]
}
```

Activity from watched contributors gets highlighted differently in the digest, and contributor-specific summaries can be generated.

### 7.4 PR Diff Fetching

Currently Herald only sees PR titles, descriptions, and metadata. Optionally fetch the actual diff (or a truncated version) for PRs that match high-priority keywords, giving the AI much richer context for assessing impact.

### 7.5 Issue Comment Threads

Fetch recent comments on active issues and PRs, not just the top-level metadata. Decisions and design discussions happen in comments -- Herald currently misses all of this.

---

## 8. Filtering and Noise Reduction

### 8.1 Exclude Patterns

Allow groups to define exclusion rules that filter activity before it reaches the AI:

```json
"exclude": {
  "authors": ["dependabot[bot]", "renovate[bot]"],
  "title_patterns": ["^chore:", "^ci:"],
  "labels": ["wontfix", "duplicate"]
}
```

This reduces prompt size (saving cost) and prevents the AI from wasting summary space on noise.

### 8.2 Include-Only Patterns

The inverse -- only summarize activity matching specific criteria. Useful for teams that only care about, say, PRs touching certain file paths or carrying certain labels.

### 8.3 Smart Deduplication

Commits that are part of a merged PR should be grouped under that PR rather than listed separately. Currently both the commit and the PR appear in the prompt, inflating it with redundant information.

---

## 9. Developer Experience

### 9.1 Structured Logging

Replace `print()` calls with Python's `logging` module. Add `--verbose` / `--quiet` flags. Log to file by default in addition to stderr. This makes debugging production runs (especially scheduled ones) much easier.

### 9.2 Dry Run Mode

`herald --dry-run` fetches activity and builds the prompt but does not call the AI backend or post to any channels. Prints the prompt to stdout so users can inspect what the AI would see.

### 9.3 Plugin Architecture

Formalize the source and backend registries into a plugin system. Allow users to drop a Python file into a `plugins/` directory (or install a pip package) that registers new source types or AI backends without modifying `herald.py`.

### 9.4 Config Schema with JSON Schema

Publish a JSON Schema file for `herald.config.json` and group config files. IDEs with JSON Schema support will provide autocomplete, validation, and inline documentation as users edit their configs.

---

## 10. Claude Code Skills: Output Quality Feedback Loop

Herald's output quality depends on the AI prompt, but tuning that prompt today requires editing JSON config and Python code. These Claude Code skills create a conversational feedback loop: users save examples of good and bad output, annotate what worked or didn't, and Herald uses that accumulated context to steer future summaries toward what the team actually wants.

All examples and style context are stored in `examples/` (per-group subdirectories) and `style_context.json` files alongside group configs. The prompt assembly in `format_prompt()` reads these at runtime and injects them as few-shot guidance.

### 10.1 `/herald-save-good` -- Save a Positive Example

Saves a Herald output (or a section of one) as a positive example for a group. The user runs this after reading a digest and thinking "this is exactly what I wanted."

**Workflow:**
1. User runs `/herald-save-good` inside the Herald project
2. Skill prompts: which group? (auto-detected from recent runs, or manual entry)
3. Skill prompts: paste the output (or provide a path to a report file)
4. Skill prompts: what made this good? (free-text annotation -- e.g., "the TL;DR was sharp," "it correctly identified the SEV-SNP relevance of a generic PR," "the recommended actions were specific enough to act on immediately")
5. Saves to `examples/<group>/good/<timestamp>.md` with the annotation as YAML front matter
6. Triggers a rebuild of the group's style context (see 10.5)

**Stored format:**
```markdown
---
group: occ-team
rating: good
date: 2026-04-16
tags: [relevance-detection, actionable-recommendations]
annotation: >
  Correctly identified that PR #1190 (generic config refactor) had direct
  implications for SEV-SNP attestation policy. Recommended actions were
  specific -- pointed to exact PRs to review and why.
---

### Summary

- **[HIGH PRIORITY] Unified Configuration Overhaul** [MERGED] ...
...
```

### 10.2 `/herald-save-bad` -- Save a Negative Example

Saves a Herald output as a negative example with specific critique. This is how users tell Herald "stop doing this."

**Workflow:**
1. User runs `/herald-save-bad`
2. Skill prompts: which group?
3. Skill prompts: paste the output (or path)
4. Skill prompts: what was wrong? (structured choices + free text)
   - Too verbose / not concise enough
   - Missed something important (what?)
   - Included irrelevant noise (which items?)
   - Wrong priority ordering
   - Recommendations were too vague / generic
   - Tone or formatting issues
   - Other (free text)
5. Saves to `examples/<group>/bad/<timestamp>.md` with the critique as front matter
6. Triggers a rebuild of the group's style context

### 10.3 `/herald-annotate` -- Annotate Specific Bullets

More surgical than save-good/save-bad. The user highlights individual bullets or sections within a digest and marks each one.

**Workflow:**
1. User runs `/herald-annotate`
2. Skill reads the most recent digest output (from cache or a specified file)
3. Skill presents each bullet and recommended action one at a time
4. For each item, user selects: `keep` (good as-is), `improve` (with note), or `drop` (should not have been included)
5. Saves the annotated digest to `examples/<group>/annotated/<timestamp>.md`
6. Triggers a rebuild of the group's style context

This gives the finest-grained feedback. Over time, patterns emerge: "the team always drops Dependabot bumps," "the team always marks SEV-SNP items as keep," "recommendations about 'monitoring' are always marked improve with notes like 'be more specific.'"

### 10.4 `/herald-review-examples` -- Review and Curate Saved Examples

Manage the accumulated example library.

**Workflow:**
1. User runs `/herald-review-examples`
2. Skill lists all saved examples for a group (or all groups), showing date, rating, and annotation summary
3. User can:
   - **View** any example in full
   - **Delete** outdated examples (e.g., from before a priority change)
   - **Re-tag** examples with updated annotations
   - **Promote** a specific example as the "gold standard" reference -- this one gets injected first in the prompt's few-shot section and carries the most weight

### 10.5 `/herald-build-context` -- Generate Style Context from Examples

Synthesizes all saved examples into a structured style guide that gets injected into the Herald prompt.

**Workflow:**
1. User runs `/herald-build-context` (also triggered automatically by save-good/save-bad/annotate)
2. Skill reads all examples for the specified group
3. Uses Claude to analyze the examples and extract patterns:
   - What the team considers high-value vs. noise
   - Preferred bullet length and detail level
   - How specific recommendations should be
   - Topics that should always be elevated or suppressed
   - Formatting preferences (links inline vs. at end, status tags, etc.)
4. Generates `examples/<group>/style_context.json`:

```json
{
  "group": "occ-team",
  "generated": "2026-04-16T14:30:00Z",
  "based_on": 12,
  "style_guidance": {
    "do": [
      "Identify non-obvious AMD/SEV-SNP relevance in generic PRs",
      "Make recommended actions specific -- name exact PRs and explain why to review",
      "Flag competitive dynamics (other hardware vendors adding support)",
      "Connect dependency updates to their impact on attestation paths"
    ],
    "dont": [
      "Include Dependabot/Renovate bumps unless they affect cryptographic or attestation libraries",
      "Use vague recommendations like 'monitor this area' -- say what to do and where",
      "Repeat PR titles verbatim as bullet titles -- synthesize the meaning",
      "Include CI/chore commits in the summary"
    ],
    "tone": "Technical and direct. No hedging. Assume the reader is a senior engineer.",
    "length": "Bullets should be 2-3 sentences max. Recommendations should be 1-2 sentences with a link."
  },
  "gold_standard_example": "examples/occ-team/good/20260410_143022.md",
  "suppressed_topics": ["dependabot", "ci fixes", "typo corrections"],
  "elevated_topics": ["SEV-SNP", "attestation", "key management", "breaking changes"]
}
```

5. This file is read by `format_prompt()` and injected into the prompt as a `STYLE CONTEXT` block between the team context and the activity data. The gold standard example is included as a few-shot reference.

### 10.6 `/herald-compare` -- Compare Output Against Saved Examples

Evaluates a new digest against the team's established preferences before posting.

**Workflow:**
1. User runs `/herald-compare` after generating a digest (or with `--dry-run` output)
2. Skill loads the group's style context and saved examples
3. Uses Claude to score the new output against the team's preferences:
   - Does it match the "do" guidance?
   - Does it violate any "dont" rules?
   - How does it compare to the gold standard example?
   - Are suppressed topics leaking through? Are elevated topics present?
4. Outputs a short report with a quality score and specific suggestions
5. User can then choose to: post as-is, regenerate with `--force`, or save-bad and regenerate

This creates a pre-publication quality gate -- especially useful when changing AI backends, models, or prompt parameters and wanting to verify output quality hasn't regressed.

### 10.7 `/herald-reset-context` -- Reset a Group's Style Context

Clears all examples and generated context for a group. Useful when team priorities change significantly and the accumulated preferences no longer apply.

**Workflow:**
1. User runs `/herald-reset-context`
2. Skill prompts for confirmation and which group
3. Archives existing examples to `examples/<group>/archive/<date>/`
4. Clears `style_context.json`
5. Next Herald run uses the base prompt with no style context

### How These Skills Connect to Herald's Prompt

The integration point is `format_prompt()` in `herald.py`. When a `style_context.json` exists for a group, Herald injects it into the prompt:

```
TEAM CONTEXT:
...existing team context...

STYLE CONTEXT (learned from team feedback):
DO:
- Identify non-obvious AMD/SEV-SNP relevance in generic PRs
- Make recommended actions specific...
DO NOT:
- Include Dependabot/Renovate bumps unless...
TONE: Technical and direct...

REFERENCE EXAMPLE (this is what a good digest looks like for this team):
<contents of gold standard example>

REPOSITORY ACTIVITY:
...
```

This means the feedback loop is purely additive -- it enriches the prompt without changing Herald's code. Users who never use the skills get the same behavior as today. Users who invest in curating examples get progressively better output tuned to their team's specific needs.

---

## 11. Portability and Packaging

### 11.1 Single Binary Distribution

Use PyInstaller or Nuitka to produce standalone binaries for Linux, macOS, and Windows. Users download one file and run it -- no Python installation required.

### 11.2 Homebrew Formula

`brew install herald` for macOS and Linux users. Pairs with the PyPI package or standalone binary.

### 11.3 Platform-Agnostic Config Discovery

Standardize config file search to follow XDG on Linux, `~/Library/Application Support/` on macOS, and `%APPDATA%` on Windows, in addition to the current CWD and home directory search.

### 11.4 Environment Variable Configuration

Support full configuration via environment variables for container and CI/CD environments where config files are inconvenient:

```bash
HERALD_REPOS="owner/repo1,owner/repo2"
HERALD_DAYS=7
HERALD_AI_BACKEND="anthropic-api"
HERALD_ANTHROPIC_API_KEY="sk-..."
HERALD_SLACK_WEBHOOK="https://hooks.slack.com/..."
```

---

## Enhancement Priority Matrix

| Enhancement | Signal Density | Ease of Start | Portability | Audience Growth | Complexity |
|---|---|---|---|---|---|
| Output profiles (1.1) | High | -- | -- | Medium | Low |
| Delta mode (1.2) | High | -- | -- | -- | Medium |
| TL;DR header (1.4) | High | -- | -- | -- | Low |
| Severity scoring (1.3) | High | -- | -- | -- | Low |
| Interactive init (5.1) | -- | High | -- | High | Medium |
| Config validation (5.2) | -- | High | -- | -- | Low |
| GitLab source (2.1) | -- | -- | High | High | Medium |
| Anthropic API backend (3.1) | -- | Medium | High | Medium | Low |
| OpenAI backend (3.2) | -- | -- | High | High | Medium |
| Slack delivery (4.1) | -- | -- | -- | High | Low |
| Email delivery (4.2) | -- | -- | -- | High | Medium |
| Exclude patterns (8.1) | High | -- | -- | -- | Low |
| Smart dedup (8.3) | High | -- | -- | -- | Medium |
| GH Actions template (6.2) | -- | High | -- | High | Low |
| PyPI package (5.4) | -- | High | High | High | Medium |
| PR diff fetching (7.4) | High | -- | -- | -- | Medium |
| Issue comments (7.5) | High | -- | -- | -- | Medium |
| Cross-group correlation (7.1) | High | -- | -- | -- | Medium |
| Trend analysis (7.2) | Medium | -- | -- | Medium | High |
| Structured logging (9.1) | -- | -- | -- | -- | Low |
| Docker image (6.3) | -- | Medium | High | Medium | Low |
| Save good/bad examples (10.1, 10.2) | High | -- | -- | -- | Low |
| Bullet-level annotation (10.3) | High | -- | -- | -- | Medium |
| Build style context (10.5) | High | -- | -- | -- | Medium |
| Compare output quality (10.6) | High | -- | -- | -- | Medium |
| Review/curate examples (10.4) | Medium | -- | -- | -- | Low |
