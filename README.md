# Herald - Multi-Source Repository Activity Tracker

Herald fetches recent activity from GitHub repositories and generates AI-powered summaries using Claude CLI. Configure groups of repositories with team-specific context so the AI highlights what matters most to your team.

## Quick Start

### Prerequisites

- Python 3.8+
- [Claude CLI](https://github.com/anthropics/claude-code) installed and authenticated
- (Optional) `GITHUB_TOKEN` environment variable for higher API rate limits

### Install

```bash
pip install -r requirements.txt
```

### Configure

Copy the example config and create your group config:

```bash
cp herald.config.example.json herald.config.json
cp groups/example.json groups/my-team.json
# Edit groups/my-team.json with your repositories and team context
cp secrets/example.json secrets/my-team.json
# Edit secrets/my-team.json with your webhook URL
```

### Run

```bash
python herald.py
```

## Usage

```bash
# Use default config (herald.config.json)
python herald.py

# Override time window
python herald.py --days 30

# Override repositories (ignores configured groups)
python herald.py --repos owner/repo1,owner/repo2

# Save output to file
python herald.py --output digest.md

# Force regenerate, ignoring cache
python herald.py --force

# Post to Microsoft Teams
python herald.py --teams

# Run specific group(s) only
python herald.py --group my-team
python herald.py -g team-a -g team-b

# List configured groups
python herald.py --list-groups

# Use a custom config file
python herald.py --config /path/to/config.json

# Override team name in AI output
python herald.py --team-name "Platform Team"

# Validate config without running
python herald.py --validate

# Preview what would run without calling AI or posting
python herald.py --dry-run

# Output the assembled AI prompt only (no AI call)
python herald.py --prompt-only

# Verbose or quiet logging
python herald.py -v          # DEBUG level
python herald.py -q          # WARNING level only
```

## Sample Output

Herald generates a digest with a TL;DR, categorized summary, and recommended actions:

```
**TL;DR:** Three attestation-related PRs merged improving SEV-SNP quote parsing and VCEK caching.

### Summary

#### High Priority — AMD SEV-SNP
- **PR #412** (merged): Refactored SNP quote parser to handle v3 report format...
- **PR #408** (merged): Added VCEK certificate caching to reduce KDS lookups...

#### Other Notable Activity
- **Issue #415** (opened): Request for TDX attestation parity with SNP flow...

### Recommended Actions
1. Review PR #412 for compatibility with existing attestation verification tests
2. Track Issue #415 — may require AMD-side changes to keep feature parity
```

## Documentation

| Document | Description |
|---|---|
| [User Guide](docs/user-guide.md) | Configuration reference, environment variables, reports, troubleshooting |
| [Development Guide](docs/development.md) | Adding sources and AI backends, automation setup |
| [Architecture](docs/architecture.md) | Class hierarchy and data flow |

## License

MIT License
