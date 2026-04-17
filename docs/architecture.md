# Architecture

## Class Hierarchy

```
herald.py
  ActivitySource (ABC)     -- cache helpers, abstract fetch_activity/validate
  GitHubSource             -- GitHub REST API v3 fetcher
  SOURCE_REGISTRY          -- maps type strings to source classes
  AIBackend (ABC)          -- abstract base for AI summarization backends
  ClaudeCLIBackend         -- Claude CLI subprocess backend (default)
  AI_BACKEND_REGISTRY      -- maps type strings to backend classes
  Herald                   -- orchestrator: config, groups, prompts, AI backend, reports, Teams
  main()                   -- CLI argument parsing
```

## Data Flow

1. Config loaded from `herald.config.json`, resolved into groups
2. Each group's sources fetch activity from their respective APIs (cached with 1-hour TTL)
3. A detailed raw report is saved to `reports/<group>/`
4. Activity + team context formatted into a prompt sent to the configured AI backend
5. AI response is stripped of conversational preamble/postamble
6. Final digest output to stdout/file; optionally posted to Teams
