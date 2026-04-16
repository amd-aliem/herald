# Herald: Deferred Enhancements

Items evaluated and deferred during the Phase 1 planning cycle. These are not rejected -- they are parked until user demand materializes or prerequisites are met.

## Deferred Items

### 1.3 Severity / Relevance Scoring
**Verdict:** Marginal. Redundant with the existing `priorities` system in `team_context`. The AI already orders bullets by relevance. If revisited, enhance the existing `[HIGH PRIORITY]` tag system rather than adding a parallel keyword-based scoring mechanism.

### 2.3 Mailing List / Discourse Source
**Verdict:** Marginal. Severe data model mismatch (mailing list posts are not commits/PRs/issues). High complexity (~300-400 lines for Discourse alone). Use RSS/Atom source (2.4) pointed at mailing list Atom feeds as a lighter alternative.

### 3.3 Local / Ollama Backend
**Verdict:** Marginal. Redundant with the OpenAI-compatible backend (3.2) pointed at `http://localhost:11434/v1`. Document Ollama as a configuration option for 3.2 rather than building a separate backend class.

### 4.3 Discord Webhook
**Verdict:** Marginal. 2,000-character message limit severely constrains output quality. Can likely be handled by the generalized webhook (4.6) with a Discord-specific template. Build only if users specifically request it.

### 4.5 GitHub Discussions / Issues
**Verdict:** Marginal. Requires GraphQL API (not REST), write-scoped GitHub token (security escalation), and a "target repo" concept. High complexity relative to value.

### 5.1 Interactive Setup Wizard (`herald init`)
**Verdict:** Marginal. Current setup is already 3 commands (cp config, cp group, edit). The wizard adds ongoing maintenance burden (must be updated for every new config field). Defer until `herald validate` (5.2) exists, then reassess.

### 6.1 Built-in Cron Expression Support
**Verdict:** Marginal. Reinvents OS scheduling (cron, systemd timers, launchd). Only build `--schedule-generate` to emit crontab/systemd/launchd snippets -- not a daemon mode.

### 7.1 Cross-Group Correlation
**Verdict:** Marginal. Only valuable for multi-group deployments (currently one group is configured). Doubles AI cost. Revisit when multi-group usage materializes.

### 7.2 Trend Analysis Over Time
**Verdict:** Marginal (but keep design open -- see note below). Introduces persistent state (SQLite) into a stateless tool. Requires ~4 weeks of data before producing useful output. Incompatible with ephemeral CI runners without artifact caching.

**Design Note:** While deferred, the Phase 1 implementation should keep the architecture open for future trend support:
- The structured logging (9.1) should log run metadata (group name, item counts, timestamp) that could later be parsed or stored
- The `generate_group_digest()` method should keep activity counts easily extractable (they already are in the raw activity dicts)
- If a `TrendStore` class is later added, it should slot in at the `generate_group_digest()` level, recording counts after fetching but before summarization
- The `format_prompt()` method should remain extensible for injecting a `HISTORICAL TRENDS:` block between team context and repository activity

### 8.2 Include-Only Patterns
**Verdict:** Marginal. File-path filtering requires extra API calls per PR/commit. Label filtering has limited value (many upstream repos use labels sparingly). Risk of confusion with exclude patterns. If needed, add only label/author include-only (not file-path).

### 9.3 Plugin Architecture
**Verdict:** Marginal. Premature -- Herald has exactly one source and one backend. The existing registries (`SOURCE_REGISTRY`, `AI_BACKEND_REGISTRY`) already decouple type strings from classes. Build a formal plugin system only after a second source type exists and the real plugin API requirements are understood.

### 10.3 `/herald-annotate` (bullet-by-bullet annotation)
**Verdict:** Marginal. 5-10 minutes per annotation session for a biweekly digest is disproportionate effort. Users rarely sustain this level of engagement. The save-good/save-bad skills with free-text annotation capture most of the same signal.

### 10.4 `/herald-review-examples` (example curation)
**Verdict:** Marginal. Only useful after 10+ examples accumulate (months of usage). Curation can be handled within `/herald-build-context` or manually via file management.

### 10.6 `/herald-compare` (quality gate)
**Verdict:** Marginal. Circular AI evaluation (Claude judging Claude's output). Rarely changes the outcome. Consider a programmatic `--verify` flag instead (string matching for suppressed/elevated topics).

### 11.1 Single Binary Distribution (PyInstaller/Nuitka)
**Verdict:** Marginal. Binary size bloat (30-80MB for 1270 lines), PyInstaller fragility, cross-platform CI matrix, and does not eliminate the Claude CLI dependency. Prerequisite: Anthropic API backend (3.1) must exist first. Defer until user demand and prerequisites are met.

---

## When to Revisit

- **After Phase 2 (core features):** Reassess 5.1 (wizard), 9.3 (plugins), 7.1 (cross-group)
- **After multi-group adoption:** Reassess 7.1, 7.2
- **After OpenAI backend (3.2) ships:** Close 3.3 (Ollama) as covered
- **After generalized webhook (4.6) ships:** Reassess 4.3 (Discord), 4.5 (GitHub Discussions)
- **After feedback skills are in use:** Reassess 10.3, 10.4, 10.6
- **After Anthropic API backend (3.1) ships:** Reassess 11.1 (binary)
