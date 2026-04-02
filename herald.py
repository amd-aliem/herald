#!/usr/bin/env python3
"""
Herald - Multi-Source Repository Activity Tracker
Fetches recent activity from configured sources and generates AI-powered summaries.
"""

import json
import sys
import os
import argparse
import subprocess
import hashlib
import shutil
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

try:
    import requests
except ImportError:
    print("Error: 'requests' module not found. Install with: pip install requests")
    sys.exit(1)

try:
    from dateutil import parser as date_parser
except ImportError:
    print("Error: 'python-dateutil' module not found. Install with: pip install python-dateutil")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RAW_ACTIVITY_HEADER = "**Raw Activity Data:**"


# ---------------------------------------------------------------------------
# ActivitySource — abstract base for all activity sources
# ---------------------------------------------------------------------------

class ActivitySource(ABC):
    """Abstract base class for activity sources (GitHub, GitLab, etc.)."""

    source_type: str = "base"

    def __init__(self, config: Dict[str, Any], cache_dir: Path, force_refresh: bool = False):
        self.config = config
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl = 3600  # 1 hour
        self.force_refresh = force_refresh

    @abstractmethod
    def fetch_activity(self, since: datetime) -> List[Dict[str, Any]]:
        """Fetch activity since a given date. Returns a list of per-repo activity dicts."""

    @abstractmethod
    def validate(self) -> bool:
        """Validate source configuration. Returns True if valid."""

    # -- caching helpers (shared by all sources) --

    def get_cache_path(self, key: str, activity_type: str) -> Path:
        key_safe = key.replace('/', '_')
        date_str = datetime.now().strftime('%Y-%m-%d')
        return self.cache_dir / f"{key_safe}_{activity_type}_{date_str}.json"

    def is_cache_valid(self, cache_path: Path) -> bool:
        if self.force_refresh:
            return False
        if not cache_path.exists():
            return False
        age = datetime.now().timestamp() - cache_path.stat().st_mtime
        return age < self.cache_ttl

    def load_cache(self, cache_path: Path) -> Optional[Any]:
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load cache from {cache_path}: {e}")
            return None

    def save_cache(self, cache_path: Path, data: Any):
        try:
            with open(cache_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Warning: Failed to save cache to {cache_path}: {e}")


# ---------------------------------------------------------------------------
# GitHubSource — fetches activity from GitHub repositories
# ---------------------------------------------------------------------------

class GitHubSource(ActivitySource):
    """Fetches activity from GitHub repositories via REST API v3."""

    source_type = "github"

    def __init__(self, config: Dict[str, Any], cache_dir: Path, force_refresh: bool = False):
        super().__init__(config, cache_dir, force_refresh)
        self.repositories: List[str] = config.get("repositories", [])
        self.max_commits: int = config.get("max_commits", 20)
        self.activity_types: List[str] = config.get("activity_types",
                                                     ["commits", "pulls", "issues", "releases"])

    def validate(self) -> bool:
        valid = True
        for repo in self.repositories:
            parts = repo.split('/')
            if len(parts) != 2 or not all(parts):
                print(f"Error: Invalid repository format: {repo} (expected owner/repo)")
                valid = False
        return valid

    # -- GitHub API helpers --

    def github_request(self, url: str, params: Optional[Dict] = None) -> Optional[Any]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Herald/2.0"
        }

        github_token = os.environ.get('GITHUB_TOKEN')
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)

            if response.status_code == 403:
                rate_limit = response.headers.get('X-RateLimit-Remaining', 'unknown')
                if rate_limit == '0':
                    reset_time = response.headers.get('X-RateLimit-Reset', 'unknown')
                    if reset_time != 'unknown':
                        reset_dt = datetime.fromtimestamp(int(reset_time))
                        print(f"Error: GitHub API rate limit exceeded. Resets at {reset_dt}")
                    else:
                        print("Error: GitHub API rate limit exceeded.")
                    return None

            if response.status_code == 404:
                print(f"Error: Resource not found (404): {url}")
                return None

            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout:
            print(f"Error: Request timeout for {url}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"Error: Request failed for {url}: {e}")
            return None

    # -- per-activity-type fetchers --

    def fetch_commits(self, repo: str, since: datetime) -> List[Dict]:
        cache_path = self.get_cache_path(repo, "commits")

        if self.is_cache_valid(cache_path):
            cached_data = self.load_cache(cache_path)
            if cached_data:
                print(f"  Using cached commits data")
                return cached_data

        url = f"https://api.github.com/repos/{repo}/commits"
        params = {
            "since": since.isoformat(),
            "per_page": self.max_commits
        }

        data = self.github_request(url, params)
        if data is None:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                print(f"  Using stale cache for commits")
                return stale_cache
            return []

        self.save_cache(cache_path, data)
        return data

    def fetch_pulls(self, repo: str, since: datetime) -> List[Dict]:
        cache_path = self.get_cache_path(repo, "pulls")

        if self.is_cache_valid(cache_path):
            cached_data = self.load_cache(cache_path)
            if cached_data:
                print(f"  Using cached pull requests data")
                return cached_data

        url = f"https://api.github.com/repos/{repo}/pulls"
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100
        }

        data = self.github_request(url, params)
        if data is None:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                print(f"  Using stale cache for pull requests")
                return stale_cache
            return []

        filtered = []
        for pr in data:
            updated_at = date_parser.parse(pr['updated_at'])
            if updated_at >= since:
                filtered.append(pr)

        self.save_cache(cache_path, filtered)
        return filtered

    def fetch_issues(self, repo: str, since: datetime) -> List[Dict]:
        cache_path = self.get_cache_path(repo, "issues")

        if self.is_cache_valid(cache_path):
            cached_data = self.load_cache(cache_path)
            if cached_data:
                print(f"  Using cached issues data")
                return cached_data

        url = f"https://api.github.com/repos/{repo}/issues"
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100
        }

        data = self.github_request(url, params)
        if data is None:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                print(f"  Using stale cache for issues")
                return stale_cache
            return []

        filtered = []
        for issue in data:
            if 'pull_request' in issue:
                continue
            updated_at = date_parser.parse(issue['updated_at'])
            if updated_at >= since:
                filtered.append(issue)

        self.save_cache(cache_path, filtered)
        return filtered

    def fetch_releases(self, repo: str, since: datetime) -> List[Dict]:
        cache_path = self.get_cache_path(repo, "releases")

        if self.is_cache_valid(cache_path):
            cached_data = self.load_cache(cache_path)
            if cached_data:
                print(f"  Using cached releases data")
                return cached_data

        url = f"https://api.github.com/repos/{repo}/releases"
        params = {"per_page": 10}

        data = self.github_request(url, params)
        if data is None:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                print(f"  Using stale cache for releases")
                return stale_cache
            return []

        filtered = []
        for release in data:
            if release['published_at']:
                published_at = date_parser.parse(release['published_at'])
                if published_at >= since:
                    filtered.append(release)

        self.save_cache(cache_path, filtered)
        return filtered

    # -- main fetch_activity implementation --

    def fetch_activity(self, since: datetime) -> List[Dict[str, Any]]:
        """Fetch activity for all configured repositories."""
        results = []
        for repo in self.repositories:
            print(f"\nFetching activity for {repo}...")

            activity: Dict[str, Any] = {
                "repository": repo,
                "source_type": self.source_type,
                "since": since.isoformat()
            }

            if "commits" in self.activity_types:
                activity["commits"] = self.fetch_commits(repo, since)
                print(f"  Found {len(activity['commits'])} commits")

            if "pulls" in self.activity_types:
                activity["pulls"] = self.fetch_pulls(repo, since)
                print(f"  Found {len(activity['pulls'])} pull requests")

            if "issues" in self.activity_types:
                activity["issues"] = self.fetch_issues(repo, since)
                print(f"  Found {len(activity['issues'])} issues")

            if "releases" in self.activity_types:
                activity["releases"] = self.fetch_releases(repo, since)
                print(f"  Found {len(activity['releases'])} releases")

            results.append(activity)

        return results


# Source registry — add new sources here
SOURCE_REGISTRY: Dict[str, type] = {
    "github": GitHubSource,
}


# ---------------------------------------------------------------------------
# AIBackend — abstract base for AI summarization backends
# ---------------------------------------------------------------------------

class AIBackend(ABC):
    """Abstract base class for AI summarization backends."""

    backend_type: str = "base"

    def __init__(self, config: Dict[str, Any], cache_dir: Path, force_refresh: bool = False):
        self.config = config
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.force_refresh = force_refresh

    @abstractmethod
    def summarize(self, prompt: str, label: str = "") -> Optional[str]:
        """Send prompt to AI backend, return response text or None on failure.

        Args:
            prompt: The text prompt to send to the AI backend
            label: Optional label used for debug/cache file naming
        """

    @abstractmethod
    def validate(self) -> bool:
        """Check if the backend is available (e.g., CLI installed, API key set)."""


class ClaudeCLIBackend(AIBackend):
    """AI backend using Claude CLI subprocess."""

    backend_type = "claude-cli"

    def __init__(self, config: Dict[str, Any], cache_dir: Path, force_refresh: bool = False):
        super().__init__(config, cache_dir, force_refresh)
        self.timeout = config.get("timeout", 600)

    def validate(self) -> bool:
        """Check if Claude CLI is installed."""
        return shutil.which('claude') is not None

    def summarize(self, prompt: str, label: str = "") -> Optional[str]:
        """Call Claude CLI with the prompt and return the summary."""
        if not shutil.which('claude'):
            print("\nError: Claude CLI not found.")
            print("Please install and authenticate:")
            print("  brew install claude  # or follow https://github.com/anthropics/claude-code")
            print("  claude auth login")
            return None

        try:
            print(f"  Calling Claude CLI...")
            print(f"  Prompt length: {len(prompt)} characters")

            result = subprocess.run(
                ['claude', '-'],
                input=prompt.encode('utf-8'),
                capture_output=True,
                timeout=self.timeout
            )

            if result.returncode != 0:
                print(f"\n{'='*80}")
                print(f"ERROR: Claude CLI failed with exit code {result.returncode}")
                print(f"{'='*80}")

                stderr_output = result.stderr.decode('utf-8', errors='replace')
                stdout_output = result.stdout.decode('utf-8', errors='replace')

                if stderr_output:
                    print(f"\nStderr output:")
                    print(f"{'-'*80}")
                    print(stderr_output)
                    print(f"{'-'*80}")

                if stdout_output:
                    print(f"\nStdout output:")
                    print(f"{'-'*80}")
                    print(stdout_output)
                    print(f"{'-'*80}")

                self._save_debug_prompt(prompt, label, "failed")
                return None

            output = result.stdout.decode('utf-8', errors='replace').strip()
            if not output:
                print("\nWarning: Claude CLI returned empty output")
                print(f"  Exit code: {result.returncode}")
                print(f"  Stderr: {result.stderr.decode('utf-8', errors='replace')}")
                self._save_debug_prompt(prompt, label, "empty")
                return None

            print("  Claude summary generated successfully")
            print(f"  Summary length: {len(output)} characters")
            return output

        except subprocess.TimeoutExpired:
            print(f"\n{'='*80}")
            print(f"ERROR: Claude CLI timeout after {self.timeout} seconds")
            print(f"{'='*80}")
            print("This might indicate:")
            print("  - The prompt is too large")
            print("  - Network connectivity issues")
            print("  - Claude API issues")
            self._save_debug_prompt(prompt, label, "timeout")
            print(f"{'='*80}\n")
            return None
        except Exception as e:
            print(f"\n{'='*80}")
            print(f"ERROR: Exception calling Claude CLI: {e}")
            print(f"{'='*80}")
            print(f"Exception type: {type(e).__name__}")
            self._save_debug_prompt(prompt, label, "exception")
            print(f"{'='*80}\n")
            return None

    def _save_debug_prompt(self, prompt: str, label: str, reason: str):
        """Save prompt to a debug file for troubleshooting."""
        safe_label = label.replace('/', '_')
        debug_file = self.cache_dir / (
            f"debug_prompt_{reason}_{safe_label}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        try:
            with open(debug_file, 'w') as f:
                f.write(prompt)
            print(f"\nDebug: Prompt saved to {debug_file}")
        except Exception as e:
            print(f"\nWarning: Could not save debug prompt: {e}")


# AI backend registry — add new backends here
AI_BACKEND_REGISTRY: Dict[str, type] = {
    "claude-cli": ClaudeCLIBackend,
}


# ---------------------------------------------------------------------------
# Herald — main orchestrator
# ---------------------------------------------------------------------------

class Herald:
    """Main orchestrator for fetching activity and generating digests."""

    def __init__(self, config_path: Optional[str] = None, force_refresh: bool = False):
        self.force_refresh = force_refresh
        self.cache_dir = Path(__file__).parent / ".cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.cache_ttl = 3600
        self.config_dir = Path(__file__).parent  # default, overridden by load_config
        self.config = self.load_config(config_path)
        self.groups = self.resolve_groups()
        self.all_summaries_failed = False

        # Initialize AI backend
        defaults = self.config.get("defaults", {})
        ai_config = defaults.get("ai_backend", {"type": "claude-cli"})
        backend_type = ai_config.get("type", "claude-cli")
        backend_cls = AI_BACKEND_REGISTRY.get(backend_type)
        if not backend_cls:
            print(f"Warning: Unknown AI backend '{backend_type}', falling back to claude-cli")
            backend_cls = ClaudeCLIBackend
        self.ai_backend = backend_cls(ai_config, self.cache_dir, force_refresh)

    def load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """Load configuration from file or use defaults."""
        default_config: Dict[str, Any] = {
            "defaults": {
                "time_window_days": 14,
                "max_commits": 20,
                "activity_types": ["commits", "pulls", "issues", "releases"]
            },
            "groups": []
        }

        if config_path is None:
            # Search for config files; prefer herald.config.json, fall back to occ-digest
            for path in ["./herald.config.json",
                         Path.home() / ".herald.config.json"]:
                if Path(path).exists():
                    config_path = str(path)
                    break

            if config_path is None:
                for path in ["./occ-digest.config.json",
                             Path.home() / ".occ-digest.config.json"]:
                    if Path(path).exists():
                        config_path = str(path)
                        print(f"Warning: Using legacy config {path}. "
                              f"Consider renaming to herald.config.json")
                        break

        if config_path and Path(config_path).exists():
            self.config_dir = Path(config_path).resolve().parent
            try:
                with open(config_path, 'r') as f:
                    user_config = json.load(f)
                    default_config.update(user_config)
                    print(f"Loaded config from: {config_path}")
            except Exception as e:
                print(f"Warning: Failed to load config from {config_path}: {e}")

        return default_config

    def resolve_groups(self) -> List[Dict[str, Any]]:
        """Parse groups from config. Wraps flat config as single group for backward compat."""
        config = self.config

        # New-style: groups array present
        if "groups" in config and config["groups"]:
            resolved = []
            for group in config["groups"]:
                # Support external config files
                if "config_file" in group:
                    ext_path = self.config_dir / group["config_file"]
                    if ext_path.exists():
                        try:
                            with open(ext_path, 'r') as f:
                                ext_config = json.load(f)
                            # Merge: external config overrides, but keep group name
                            name = group.get("name", ext_path.stem)
                            ext_config["name"] = name
                            resolved.append(ext_config)
                            continue
                        except Exception as e:
                            print(f"Warning: Failed to load external config {ext_path}: {e}")
                resolved.append(group)
            return resolved

        # Legacy flat config: repositories at top level
        if "repositories" in config:
            defaults = config.get("defaults", {})
            group = {
                "name": "default",
                "sources": [
                    {
                        "type": "github",
                        "repositories": config["repositories"],
                        "max_commits": config.get("max_commits",
                                                   defaults.get("max_commits", 20)),
                        "activity_types": config.get("activity_types",
                                                      defaults.get("activity_types",
                                                                    ["commits", "pulls",
                                                                     "issues", "releases"])),
                    }
                ],
                "team_context": config.get("team_context", {}),
            }
            webhook = config.get("teams_webhook_url")
            if webhook:
                group["teams_webhook_url"] = webhook
            return [group]

        return []

    def get_defaults(self) -> Dict[str, Any]:
        """Return merged defaults."""
        return self.config.get("defaults", {
            "time_window_days": 14,
            "max_commits": 20,
            "activity_types": ["commits", "pulls", "issues", "releases"]
        })

    def list_groups(self):
        """Print configured groups and exit."""
        if not self.groups:
            print("No groups configured.")
            return
        print("Configured groups:")
        for group in self.groups:
            name = group.get("name", "unnamed")
            sources = group.get("sources", [])
            source_summary = []
            for src in sources:
                src_type = src.get("type", "unknown")
                repos = src.get("repositories", [])
                source_summary.append(f"{src_type}: {', '.join(repos)}")
            team = group.get("team_context", {}).get("name", "")
            team_str = f" (team: {team})" if team else ""
            print(f"  - {name}{team_str}")
            for s in source_summary:
                print(f"      {s}")

    # -- output cleaning --

    def strip_conversational_output(self, text: str) -> str:
        """Strip conversational preamble and postamble from AI output.

        Removes text before the first ### heading and trailing conversational
        lines after the last substantive content (bullets, headings, numbered lists).
        Falls back to original text if stripping would result in empty output.
        """
        if not text:
            return text

        lines = text.split('\n')

        # Strip preamble: find first ### heading
        start_idx = 0
        for i, line in enumerate(lines):
            if line.strip().startswith('###'):
                start_idx = i
                break

        # Strip postamble: find last substantive line (bullet, heading, bold, numbered)
        end_idx = len(lines) - 1
        while end_idx > start_idx:
            stripped = lines[end_idx].strip()
            if not stripped:
                end_idx -= 1
                continue
            # Check for markdown formatting (note: '*' covers '**')
            if (stripped.startswith(('-', '*', '#')) or
                    re.match(r'^\d+\.?\s', stripped)):
                break
            end_idx -= 1

        result = '\n'.join(lines[start_idx:end_idx + 1])
        return result if result.strip() else text

    # -- prompt formatting --

    def format_prompt(self, activity_list: List[Dict[str, Any]],
                                  team_context: Dict[str, Any]) -> str:
        """Format activity data into a prompt for Claude CLI."""
        team_name = team_context.get('name', 'team')

        # Build header
        repo_names = ", ".join(a["repository"] for a in activity_list)
        prompt = f"You are analyzing GitHub repository activity for the {team_name}. "
        prompt += f"Below is the activity for {repo_names}.\n\n"

        # Add team context if available
        if team_context:
            prompt += f"TEAM CONTEXT:\nThe {team_name} focuses on:\n"
            for area in team_context.get('focus_areas', []):
                prompt += f"- {area}\n"

            prompt += "\nTeam priorities:\n"
            for priority in team_context.get('priorities', []):
                prompt += f"- {priority}\n"
            prompt += "\n"

        prompt += "REPOSITORY ACTIVITY:\n\n"

        # Format each repository's activity
        for activity in activity_list:
            repo = activity["repository"]
            prompt += f"--- {repo} ---\n\n"

            commits = activity.get("commits", [])
            prompt += f"COMMITS ({len(commits)}):\n"
            for commit in commits[:20]:
                sha = commit['sha'][:7]
                message = commit['commit']['message'].split('\n')[0][:100]
                author = commit['commit']['author']['name']
                date = commit['commit']['author']['date']
                prompt += f"- {sha}: {message} by {author} on {date}\n"
            prompt += "\n"

            pulls = activity.get("pulls", [])
            prompt += f"PULL REQUESTS ({len(pulls)}):\n"
            for pr in pulls[:30]:
                state = "merged" if pr.get('merged_at') else pr['state']
                body = (pr.get('body') or '')[:200].replace('\n', ' ')
                prompt += (f"- #{pr['number']}: {pr['title']} [{state}] "
                           f"by {pr['user']['login']} - {pr['html_url']}\n")
                if body:
                    prompt += f"  Description: {body}\n"
            prompt += "\n"

            issues = activity.get("issues", [])
            prompt += f"ISSUES ({len(issues)}):\n"
            for issue in issues[:30]:
                state = issue['state']
                body = (issue.get('body') or '')[:200].replace('\n', ' ')
                prompt += (f"- #{issue['number']}: {issue['title']} [{state}] "
                           f"by {issue['user']['login']} - {issue['html_url']}\n")
                if body:
                    prompt += f"  Description: {body}\n"
            prompt += "\n"

            releases = activity.get("releases", [])
            prompt += f"RELEASES ({len(releases)}):\n"
            for release in releases:
                notes = (release.get('body') or '')[:300].replace('\n', ' ')
                prompt += f"- {release['tag_name']}: {release['name']} - {release['html_url']}\n"
                if notes:
                    prompt += f"  Notes: {notes}\n"
            prompt += "\n"

        # Analysis instructions
        prompt += """Please analyze this activity and provide a structured response with the following format:

### Summary

Create 3-7 concise bullet points highlighting:
1. Major features, enhancements, or changes
2. Critical bugs or security issues
3. Release activity and breaking changes
4. Significant ongoing development efforts
5. Notable patterns in contributor activity

"""

        # Dynamic priority injection based on team_context
        priorities = team_context.get('priorities', [])
        if priorities:
            top_priority = priorities[0]
            prompt += (f'**CRITICAL PRIORITY**: Activity related to "{top_priority}" '
                       f'MUST be listed at the TOP of your summary. '
                       f'These are the highest priority items.\n\n')

        prompt += """Each bullet should:
- Start with a bold topic title followed by a status tag: [OPEN], [MERGED], [CLOSED], or [RELEASED]
- Include a brief description with relevant GitHub links
- **IMPORTANT**: Add 1-2 sentences explaining WHY this change is important or WHY it happened (the motivation, impact, or context)
- Be specific and actionable

"""

        if priorities:
            prompt += f"Order your bullets with:\n"
            prompt += f'1. Items related to "{priorities[0]}" FIRST (if any)\n'
            prompt += f"2. Then other items in order of importance\n\n"
            prompt += ('Tag items that directly relate to the team\'s priorities '
                       'by prepending "[HIGH PRIORITY]" to the bold topic title. '
                       'Only use this tag when the item clearly aligns with the '
                       'team\'s stated priorities — not every bullet needs it.\n\n')

        prompt += f"### Recommended Actions for {team_name}\n\n"
        prompt += """Provide up to 3 specific, actionable items based on this activity. Fewer is fine if the activity does not warrant action for this team — do not force recommendations.

These recommendations should be tailored to the team's focus areas and priorities mentioned above. Consider:
- Code/features to review or integrate that align with team priorities
- Issues to investigate or contribute to that affect the team's focus areas
- Upcoming changes to prepare for or test
- Testing or validation efforts needed for team-relevant features
- Collaboration opportunities with upstream contributors
- Security or performance implications for the team's platform

Each recommendation should be concrete, directly related to the activity above, and clearly explain WHY it matters to the team.

IMPORTANT: Use exactly the header levels shown above (### for both sections). Do not include any other top-level headers.

OUTPUT FORMAT: Output ONLY the two markdown sections (### Summary and ### Recommended Actions).
Do not include any introduction, preamble, conclusion, sign-off, or conversational text.
Start directly with "### Summary"."""

        return prompt

    def get_summary_cache_path(self, group_name: str) -> Path:
        safe_name = group_name.replace('/', '_').replace(' ', '_')
        date_str = datetime.now().strftime('%Y-%m-%d')
        return self.cache_dir / f"{safe_name}_summary_{date_str}.txt"

    def generate_summary(self, activity_list: List[Dict[str, Any]],
                          team_context: Dict[str, Any],
                          group_name: str) -> str:
        """Generate AI summary using the configured backend."""
        summary_cache = self.get_summary_cache_path(group_name)
        if not self.force_refresh and self.is_cache_valid(summary_cache):
            cached = self.load_cache_text(summary_cache)
            if cached:
                print(f"\n  Using cached summary for {group_name}")
                return cached

        print(f"\n  Generating AI summary for {group_name}...")

        prompt = self.format_prompt(activity_list, team_context)
        summary = self.ai_backend.summarize(prompt, group_name)

        if summary:
            summary = self.strip_conversational_output(summary)
            self.save_cache_text(summary_cache, summary)
            return summary
        else:
            return self.format_raw_data(activity_list)

    def is_cache_valid(self, cache_path: Path) -> bool:
        if self.force_refresh:
            return False
        if not cache_path.exists():
            return False
        age = datetime.now().timestamp() - cache_path.stat().st_mtime
        return age < self.cache_ttl

    def load_cache_text(self, cache_path: Path) -> Optional[str]:
        try:
            with open(cache_path, 'r') as f:
                return f.read()
        except Exception:
            return None

    def save_cache_text(self, cache_path: Path, text: str):
        try:
            with open(cache_path, 'w') as f:
                f.write(text)
        except Exception as e:
            print(f"Warning: Failed to save cache to {cache_path}: {e}")

    # -- report generation --

    def save_detailed_report(self, group_name: str, activity_list: List[Dict[str, Any]],
                              time_window_days: int):
        """Save detailed raw activity data to reports/<group_name>/."""
        safe_name = group_name.replace(' ', '-').lower()
        output_dir = Path(__file__).parent / "reports" / safe_name
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = output_dir / f"herald-detailed-{timestamp}.md"

        output = "# Herald Activity Detailed Report\n"
        output += f"Group: {group_name}\n"
        output += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        output += f"Time Period: Last {time_window_days} days\n\n"

        for activity in activity_list:
            repo = activity["repository"]
            output += f"## {repo}\n\n"

            commits = activity.get("commits", [])
            pulls = activity.get("pulls", [])
            issues = activity.get("issues", [])
            releases = activity.get("releases", [])

            output += f"### Commits ({len(commits)})\n"
            for commit in commits:
                sha = commit['sha'][:7]
                message = commit['commit']['message'].split('\n')[0]
                author = commit['commit']['author']['name']
                date = commit['commit']['author']['date']
                url = commit['html_url']
                output += f"- `{sha}`: {message} by {author} on {date}\n  {url}\n"

            output += f"\n### Pull Requests ({len(pulls)})\n"
            for pr in pulls:
                state = "merged" if pr.get('merged_at') else pr['state']
                body = (pr.get('body') or '')[:300]
                output += f"- #{pr['number']}: {pr['title']} [{state}] by {pr['user']['login']}\n"
                output += f"  {pr['html_url']}\n"
                if body:
                    output += f"  Description: {body}\n"

            output += f"\n### Issues ({len(issues)})\n"
            for issue in issues:
                body = (issue.get('body') or '')[:300]
                output += (f"- #{issue['number']}: {issue['title']} [{issue['state']}] "
                           f"by {issue['user']['login']}\n")
                output += f"  {issue['html_url']}\n"
                if body:
                    output += f"  Description: {body}\n"

            output += f"\n### Releases ({len(releases)})\n"
            if releases:
                for release in releases:
                    notes = (release.get('body') or '')[:500]
                    output += f"- {release['tag_name']}: {release['name']}\n"
                    output += f"  {release['html_url']}\n"
                    if notes:
                        output += f"  Notes: {notes}\n"
            else:
                output += "No releases in this time period.\n"

            output += "\n" + "=" * 80 + "\n\n"

        with open(output_file, 'w') as f:
            f.write(output)

        print(f"\nDetailed report saved to: {output_file}")

    def format_raw_data(self, activity_list: List[Dict[str, Any]]) -> str:
        """Format raw activity data as markdown (fallback when Claude CLI is unavailable)."""
        output = f"{RAW_ACTIVITY_HEADER}\n\n"

        for activity in activity_list:
            repo = activity["repository"]
            output += f"#### {repo}\n\n"

            commits = activity.get("commits", [])
            pulls = activity.get("pulls", [])
            issues = activity.get("issues", [])
            releases = activity.get("releases", [])

            output += f"### Commits ({len(commits)})\n"
            for commit in commits[:10]:
                sha = commit['sha'][:7]
                message = commit['commit']['message'].split('\n')[0][:100]
                output += f"- {sha}: {message}\n"

            output += f"\n### Pull Requests ({len(pulls)})\n"
            for pr in pulls[:10]:
                state = "merged" if pr.get('merged_at') else pr['state']
                output += f"- #{pr['number']}: {pr['title']} [{state}] - {pr['html_url']}\n"

            output += f"\n### Issues ({len(issues)})\n"
            for issue in issues[:10]:
                output += (f"- #{issue['number']}: {issue['title']} [{issue['state']}] "
                           f"- {issue['html_url']}\n")

            output += f"\n### Releases ({len(releases)})\n"
            for release in releases:
                output += (f"- {release['tag_name']}: {release['name']} "
                           f"- {release['html_url']}\n")

            output += "\n"

        return output

    # -- Teams integration --

    def markdown_to_adaptive_card_blocks(self, markdown_text: str) -> List[Dict]:
        """Convert markdown text into Adaptive Card body blocks."""
        blocks = []
        lines = markdown_text.split('\n')
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith('# '):
                blocks.append({
                    "type": "TextBlock",
                    "text": stripped[2:],
                    "size": "ExtraLarge",
                    "weight": "Bolder",
                    "wrap": True
                })
            elif stripped.startswith('## '):
                blocks.append({
                    "type": "TextBlock",
                    "text": stripped[3:],
                    "size": "Large",
                    "weight": "Bolder",
                    "wrap": True,
                    "separator": True
                })
            elif stripped.startswith('### '):
                blocks.append({
                    "type": "TextBlock",
                    "text": stripped[4:],
                    "size": "Medium",
                    "weight": "Bolder",
                    "wrap": True,
                    "separator": True
                })
            elif stripped.startswith('---'):
                blocks.append({
                    "type": "TextBlock",
                    "text": " ",
                    "separator": True
                })
            elif stripped.startswith('- ') or stripped.startswith('* '):
                blocks.append({
                    "type": "TextBlock",
                    "text": "• " + stripped[2:],
                    "wrap": True
                })
            else:
                blocks.append({
                    "type": "TextBlock",
                    "text": stripped,
                    "wrap": True
                })

        return blocks

    def post_to_teams(self, digest_output: str, webhook_url: str) -> bool:
        """Post digest to Microsoft Teams via Power Automate webhook."""
        if not webhook_url:
            print("Error: No webhook URL provided.")
            return False

        print("\nPosting digest to Microsoft Teams...")

        card_body = self.markdown_to_adaptive_card_blocks(digest_output)

        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": card_body
                    }
                }
            ]
        }

        try:
            response = requests.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )

            if response.status_code in (200, 202):
                print("Successfully posted digest to Teams channel.")
                return True
            else:
                print(f"Error posting to Teams: HTTP {response.status_code}")
                print(f"Response: {response.text[:500]}")
                return False

        except requests.exceptions.RequestException as e:
            print(f"Error posting to Teams: {e}")
            return False

    # -- group digest generation --

    def generate_group_digest(self, group: Dict[str, Any],
                               time_window_days: Optional[int] = None) -> Tuple[str, bool]:
        """Process one group end-to-end: fetch activity, generate summary, save report."""
        group_name = group.get("name", "unnamed")
        team_context = group.get("team_context", {})
        defaults = self.get_defaults()

        if time_window_days is None:
            time_window_days = group.get("time_window_days",
                                          defaults.get("time_window_days", 14))

        since = datetime.now(timezone.utc) - timedelta(days=time_window_days)

        # Create sources and fetch activity
        all_activity: List[Dict[str, Any]] = []
        sources_config = group.get("sources", [])

        for src_config in sources_config:
            src_type = src_config.get("type", "github")
            source_cls = SOURCE_REGISTRY.get(src_type)
            if not source_cls:
                print(f"Warning: Unknown source type '{src_type}', skipping")
                continue

            # Merge defaults into source config
            merged_config = {**defaults, **src_config}
            source = source_cls(merged_config, self.cache_dir, self.force_refresh)

            if not source.validate():
                print(f"Error: Source validation failed for {src_type} in group {group_name}")
                continue

            activity = source.fetch_activity(since)
            all_activity.extend(activity)

        if not all_activity:
            return f"No activity found for group {group_name}.\n", True

        # Save detailed report
        self.save_detailed_report(group_name, all_activity, time_window_days)

        # Build output
        short_date = datetime.now().strftime('%b %d, %Y')
        repo_names = ", ".join(a["repository"].split('/')[-1] for a in all_activity)
        output = f"# {repo_names} Digest - {short_date}\n"
        output += f"*Group: {group_name}*\n\n"

        # Generate AI summary across all repos in this group
        summary = self.generate_summary(all_activity, team_context, group_name)
        summary_failed = (not summary or summary.startswith(RAW_ACTIVITY_HEADER))

        if not summary_failed:
            output += summary + "\n\n"
        else:
            output += "_AI summary unavailable - see detailed report for raw data_\n\n"

        # Add raw activity counts per repo
        for activity in all_activity:
            repo = activity["repository"]
            output += f"## {repo}\n"
            output += f"*Last {time_window_days} days (since {since.strftime('%Y-%m-%d')})*\n\n"

            commits = activity.get("commits", [])
            pulls = activity.get("pulls", [])
            issues = activity.get("issues", [])
            releases = activity.get("releases", [])

            pulls_merged = sum(1 for p in pulls if p.get('merged_at'))
            pulls_open = sum(1 for p in pulls if p['state'] == 'open')
            pulls_closed = sum(1 for p in pulls if p['state'] == 'closed'
                               and not p.get('merged_at'))

            issues_open = sum(1 for i in issues if i['state'] == 'open')
            issues_closed = sum(1 for i in issues if i['state'] == 'closed')

            output += "---\n"
            output += f"*Raw activity: {len(commits)} commits, {len(pulls)} PRs "
            output += f"({pulls_merged} merged, {pulls_open} open, {pulls_closed} closed), "
            output += f"{len(issues)} issues ({issues_open} open, {issues_closed} closed), "
            output += f"{len(releases)} releases*\n\n"

        return output, summary_failed

    def generate_digest(self, group_names: Optional[List[str]] = None,
                         repositories: Optional[List[str]] = None,
                         time_window_days: Optional[int] = None) -> str:
        """Generate digest across groups. Returns combined markdown output."""
        all_summaries_failed = True

        # If --repos is used, create a temporary single group
        if repositories:
            temp_group = {
                "name": "cli-repos",
                "sources": [
                    {
                        "type": "github",
                        "repositories": repositories,
                    }
                ],
                "team_context": {},
            }
            groups_to_process = [temp_group]
        elif group_names:
            groups_to_process = [g for g in self.groups if g.get("name") in group_names]
            missing = set(group_names) - {g.get("name") for g in groups_to_process}
            if missing:
                print(f"Warning: Groups not found: {', '.join(missing)}")
        else:
            groups_to_process = self.groups

        if not groups_to_process:
            print("Error: No groups to process.")
            sys.exit(1)

        combined_output = ""
        for group in groups_to_process:
            result = self.generate_group_digest(group, time_window_days)
            if isinstance(result, tuple):
                output, summary_failed = result
                if not summary_failed:
                    all_summaries_failed = False
            else:
                output = result
            combined_output += output

        self.all_summaries_failed = all_summaries_failed
        if all_summaries_failed:
            print("\nWarning: All Claude summaries failed. Check Claude CLI setup.")

        return combined_output


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Herald - Multi-Source Repository Activity Tracker"
    )
    parser.add_argument(
        '--config',
        help='Path to configuration file'
    )
    parser.add_argument(
        '--repos',
        help='Comma-separated list of repositories (owner/repo). Overrides configured groups.'
    )
    parser.add_argument(
        '--days',
        type=int,
        help='Number of days to look back (default: 14)'
    )
    parser.add_argument(
        '--output',
        help='Output file path (default: stdout)'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force regenerate summaries, ignoring cache'
    )
    parser.add_argument(
        '--teams',
        action='store_true',
        help='Post digest to Microsoft Teams via Power Automate webhook'
    )
    parser.add_argument(
        '--group', '-g',
        action='append',
        help='Run only specific group(s) by name (can be repeated)'
    )
    parser.add_argument(
        '--list-groups',
        action='store_true',
        help='Print configured groups and exit'
    )

    args = parser.parse_args()

    # Initialize
    herald = Herald(config_path=args.config, force_refresh=args.force)

    # List groups and exit
    if args.list_groups:
        herald.list_groups()
        return

    # Parse repositories
    repositories = None
    if args.repos:
        repositories = [r.strip() for r in args.repos.split(',')]

    # Generate digest
    output = herald.generate_digest(
        group_names=args.group,
        repositories=repositories,
        time_window_days=args.days
    )

    # Output summary
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"\nSummary written to: {args.output}")
    else:
        print("\n" + "=" * 80)
        print(output)
        print("=" * 80)

    # Post to Teams if requested
    if args.teams:
        if herald.all_summaries_failed:
            print("\nSkipping Teams post: AI summary generation failed.")
        else:
            # Post to each group's webhook
            for group in herald.groups:
                webhook = group.get("teams_webhook_url")
                if webhook:
                    herald.post_to_teams(output, webhook)


if __name__ == '__main__':
    main()
