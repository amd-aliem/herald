#!/usr/bin/env python3
"""
Herald - Multi-Source Repository Activity Tracker
Fetches recent activity from configured sources and generates AI-powered summaries.
"""

import json
import logging
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

logger = logging.getLogger("herald")

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


def setup_logging(verbose: bool = False, quiet: bool = False):
    """Configure logging for Herald.

    Logs go to stderr so stdout remains clean for digest output.
    A file handler is added later by Herald.__init__() once cache_dir is known.
    """
    herald_logger = logging.getLogger("herald")

    if verbose:
        herald_logger.setLevel(logging.DEBUG)
    elif quiet:
        herald_logger.setLevel(logging.WARNING)
    else:
        herald_logger.setLevel(logging.INFO)

    # stderr handler — concise format for interactive use
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    herald_logger.addHandler(stderr_handler)


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
            logger.warning("Failed to load cache from %s: %s", cache_path, e)
            return None

    def save_cache(self, cache_path: Path, data: Any):
        try:
            with open(cache_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save cache to %s: %s", cache_path, e)


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
        # Exclude filters — applied after fetching to remove noise
        filters = config.get("filters", {})
        self.exclude_authors: List[str] = filters.get("exclude_authors", [])
        self.exclude_titles: List[str] = filters.get("exclude_titles", [])
        self.exclude_labels: List[str] = filters.get("exclude_labels", [])

        # Pre-compile regex patterns and convert labels to set for performance
        self._compiled_title_patterns = []
        for pattern in self.exclude_titles:
            try:
                self._compiled_title_patterns.append(re.compile(pattern, re.IGNORECASE))
            except re.error as e:
                logger.warning("Invalid regex pattern '%s' in exclude_titles: %s (skipping)",
                               pattern, e)
        self._exclude_authors_set = set(self.exclude_authors)
        self._exclude_labels_set = set(self.exclude_labels)

    def validate(self) -> bool:
        valid = True
        for repo in self.repositories:
            parts = repo.split('/')
            if len(parts) != 2 or not all(parts):
                logger.error("Invalid repository format: %s (expected owner/repo)", repo)
                valid = False
        if valid and not os.environ.get('GITHUB_TOKEN'):
            logger.info("Tip: set GITHUB_TOKEN for higher API rate limits "
                        "(5000 vs 60 requests/hour)")
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
                        logger.error("GitHub API rate limit exceeded. Resets at %s", reset_dt)
                    else:
                        logger.error("GitHub API rate limit exceeded.")
                    return None

            if response.status_code == 404:
                logger.error("Resource not found (404): %s", url)
                return None

            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout:
            logger.error("Request timeout for %s", url)
            return None
        except requests.exceptions.RequestException as e:
            logger.error("Request failed for %s: %s", url, e)
            return None

    def github_request_paginated(self, url: str, params: Optional[Dict] = None,
                                    max_pages: int = 5) -> List[Dict]:
        """Fetch paginated GitHub API results using page numbers.

        Fetches up to max_pages of results. Stops early when a page
        returns fewer items than per_page (indicating the last page).
        """
        all_results: List[Dict] = []
        page_params = dict(params or {})
        per_page = int(page_params.get("per_page", 100))

        for page in range(1, max_pages + 1):
            page_params["page"] = page
            data = self.github_request(url, page_params)

            if data is None or not isinstance(data, list):
                break

            all_results.extend(data)

            if len(data) < per_page:
                break  # Last page

        return all_results

    # -- per-activity-type fetchers --

    def fetch_commits(self, repo: str, since: datetime) -> List[Dict]:
        cache_path = self.get_cache_path(repo, "commits")

        if self.is_cache_valid(cache_path):
            cached_data = self.load_cache(cache_path)
            if cached_data:
                logger.debug("Using cached commits data")
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
                logger.debug("Using stale cache for commits")
                return stale_cache
            return []

        self.save_cache(cache_path, data)
        return data

    def fetch_pulls(self, repo: str, since: datetime) -> List[Dict]:
        cache_path = self.get_cache_path(repo, "pulls")

        if self.is_cache_valid(cache_path):
            cached_data = self.load_cache(cache_path)
            if cached_data:
                logger.debug("Using cached pull requests data")
                return cached_data

        url = f"https://api.github.com/repos/{repo}/pulls"
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100
        }

        data = self.github_request_paginated(url, params)
        if not data:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                logger.debug("Using stale cache for pull requests")
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
                logger.debug("Using cached issues data")
                return cached_data

        url = f"https://api.github.com/repos/{repo}/issues"
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100
        }

        data = self.github_request_paginated(url, params)
        if not data:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                logger.debug("Using stale cache for issues")
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
                logger.debug("Using cached releases data")
                return cached_data

        url = f"https://api.github.com/repos/{repo}/releases"
        params = {"per_page": 10}

        data = self.github_request(url, params)
        if data is None:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                logger.debug("Using stale cache for releases")
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

    # -- filtering --

    def _has_filters(self) -> bool:
        return bool(self._exclude_authors_set or self._compiled_title_patterns
                     or self._exclude_labels_set)

    def apply_filters(self, activity: Dict[str, Any]) -> Dict[str, Any]:
        """Apply exclude filters to fetched activity data.

        Filters operate on:
        - exclude_authors: exact match on commit author name, PR/issue user login
        - exclude_titles: regex match on commit message subject, PR/issue title
        - exclude_labels: exact match on PR/issue label names

        Note: Commit filtering uses commit.author.name while PR/issue filtering uses
        user.login. These are different GitHub API fields by design.
        """
        if not self._has_filters():
            return activity

        removed = 0

        def _match_title(text: str) -> bool:
            return any(p.search(text) for p in self._compiled_title_patterns)

        def _match_author(author: str) -> bool:
            return author in self._exclude_authors_set

        def _match_labels(item: Dict) -> bool:
            if not self.exclude_labels:
                return False
            item_labels = {lbl.get("name", "") for lbl in item.get("labels", [])}
            return bool(item_labels & self._exclude_labels_set)

        # Filter commits
        if "commits" in activity:
            original = len(activity["commits"])
            activity["commits"] = [
                c for c in activity["commits"]
                if not (
                    _match_author(c.get("commit", {}).get("author", {}).get("name", ""))
                    or _match_title(c.get("commit", {}).get("message", "").split("\n")[0])
                )
            ]
            removed += original - len(activity["commits"])

        # Filter PRs
        if "pulls" in activity:
            original = len(activity["pulls"])
            activity["pulls"] = [
                p for p in activity["pulls"]
                if not (
                    _match_author(p.get("user", {}).get("login", ""))
                    or _match_title(p.get("title", ""))
                    or _match_labels(p)
                )
            ]
            removed += original - len(activity["pulls"])

        # Filter issues
        if "issues" in activity:
            original = len(activity["issues"])
            activity["issues"] = [
                i for i in activity["issues"]
                if not (
                    _match_author(i.get("user", {}).get("login", ""))
                    or _match_title(i.get("title", ""))
                    or _match_labels(i)
                )
            ]
            removed += original - len(activity["issues"])

        # Releases are not filtered (they are rarely noise)

        if removed:
            logger.info("  Filtered out %d items via exclude patterns", removed)

        return activity

    # -- main fetch_activity implementation --

    def fetch_activity(self, since: datetime) -> List[Dict[str, Any]]:
        """Fetch activity for all configured repositories."""
        results = []
        for repo in self.repositories:
            logger.info("Fetching activity for %s...", repo)

            activity: Dict[str, Any] = {
                "repository": repo,
                "source_type": self.source_type,
                "since": since.isoformat()
            }

            if "commits" in self.activity_types:
                activity["commits"] = self.fetch_commits(repo, since)
                logger.info("  Found %d commits", len(activity['commits']))

            if "pulls" in self.activity_types:
                activity["pulls"] = self.fetch_pulls(repo, since)
                logger.info("  Found %d pull requests", len(activity['pulls']))

            if "issues" in self.activity_types:
                activity["issues"] = self.fetch_issues(repo, since)
                logger.info("  Found %d issues", len(activity['issues']))

            if "releases" in self.activity_types:
                activity["releases"] = self.fetch_releases(repo, since)
                logger.info("  Found %d releases", len(activity['releases']))

            activity = self.apply_filters(activity)
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
            logger.error("Claude CLI not found. Install and authenticate:")
            logger.error("  brew install claude  # or follow https://github.com/anthropics/claude-code")
            logger.error("  claude auth login")
            return None

        try:
            logger.info("Calling Claude CLI...")
            logger.info("Prompt length: %d characters", len(prompt))

            result = subprocess.run(
                ['claude', '-'],
                input=prompt.encode('utf-8'),
                capture_output=True,
                timeout=self.timeout
            )

            if result.returncode != 0:
                logger.error("Claude CLI failed with exit code %d", result.returncode)

                stderr_output = result.stderr.decode('utf-8', errors='replace')
                stdout_output = result.stdout.decode('utf-8', errors='replace')

                if stderr_output:
                    logger.error("Stderr: %s", stderr_output)
                if stdout_output:
                    logger.debug("Stdout: %s", stdout_output)

                self._save_debug_prompt(prompt, label, "failed")
                return None

            output = result.stdout.decode('utf-8', errors='replace').strip()
            if not output:
                logger.warning("Claude CLI returned empty output (exit code: %d)",
                               result.returncode)
                logger.debug("Stderr: %s",
                             result.stderr.decode('utf-8', errors='replace'))
                self._save_debug_prompt(prompt, label, "empty")
                return None

            logger.info("Claude summary generated successfully (%d characters)",
                        len(output))
            return output

        except subprocess.TimeoutExpired:
            logger.error("Claude CLI timeout after %d seconds", self.timeout)
            logger.error("This might indicate: large prompt, network issues, "
                         "or Claude API issues")
            self._save_debug_prompt(prompt, label, "timeout")
            return None
        except Exception as e:
            logger.error("Exception calling Claude CLI: %s (%s)",
                         e, type(e).__name__)
            self._save_debug_prompt(prompt, label, "exception")
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
            logger.debug("Prompt saved to %s", debug_file)
        except Exception as e:
            logger.warning("Could not save debug prompt: %s", e)


# AI backend registry — add new backends here
AI_BACKEND_REGISTRY: Dict[str, type] = {
    "claude-cli": ClaudeCLIBackend,
}


# ---------------------------------------------------------------------------
# Herald — main orchestrator
# ---------------------------------------------------------------------------

class Herald:
    """Main orchestrator for fetching activity and generating digests."""

    def __init__(self, config_path: Optional[str] = None, force_refresh: bool = False,
                 dry_run: bool = False, prompt_only: bool = False):
        self.force_refresh = force_refresh
        self.dry_run = dry_run
        self.prompt_only = prompt_only
        self.cache_dir = Path(__file__).parent / ".cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.cache_ttl = 3600
        self.config_dir = Path(__file__).parent  # default, overridden by load_config

        # Add file-based log handler now that cache_dir is known
        herald_logger = logging.getLogger("herald")
        if not any(isinstance(h, logging.FileHandler) for h in herald_logger.handlers):
            try:
                file_handler = logging.FileHandler(self.cache_dir / "herald.log")
                file_handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                )
                herald_logger.addHandler(file_handler)
            except Exception as e:
                logger.warning("Could not create log file handler: %s", e)

        self.config = self.load_config(config_path)
        self._apply_env_overrides()
        self.groups = self.resolve_groups()
        self.all_summaries_failed = False
        self._prune_cache()

        # Initialize AI backend
        defaults = self.config.get("defaults", {})
        ai_config = defaults.get("ai_backend", {"type": "claude-cli"})
        backend_type = ai_config.get("type", "claude-cli")
        backend_cls = AI_BACKEND_REGISTRY.get(backend_type)
        if not backend_cls:
            logger.warning("Unknown AI backend '%s', falling back to claude-cli",
                           backend_type)
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
                        logger.warning("Using legacy config %s. "
                                       "Consider renaming to herald.config.json",
                                       path)
                        break

        if config_path and Path(config_path).exists():
            self.config_dir = Path(config_path).resolve().parent
            try:
                with open(config_path, 'r') as f:
                    user_config = json.load(f)
                    default_config.update(user_config)
                    logger.info("Loaded config from: %s", config_path)
            except Exception as e:
                logger.warning("Failed to load config from %s: %s",
                               config_path, e)
        elif config_path:
            logger.error("Config file not found: %s", config_path)
            sys.exit(1)
        else:
            logger.warning("No config file found. Using defaults. "
                           "Create herald.config.json or use --repos to get started. "
                           "See herald.config.example.json for reference.")

        return default_config

    def _apply_env_overrides(self):
        """Apply HERALD_* environment variable overrides to config.

        Supported variables:
        - HERALD_DAYS: override defaults.time_window_days (integer)
        - HERALD_MAX_COMMITS: override defaults.max_commits (integer)
        - HERALD_TEAMS_WEBHOOK: set webhook URL for groups that lack one
        """
        defaults = self.config.setdefault("defaults", {})

        days = os.environ.get("HERALD_DAYS")
        if days:
            try:
                defaults["time_window_days"] = int(days)
                logger.info("HERALD_DAYS=%s overrides time_window_days", days)
            except ValueError:
                logger.warning("HERALD_DAYS=%s is not a valid integer, ignoring", days)

        max_commits = os.environ.get("HERALD_MAX_COMMITS")
        if max_commits:
            try:
                defaults["max_commits"] = int(max_commits)
                logger.info("HERALD_MAX_COMMITS=%s overrides max_commits", max_commits)
            except ValueError:
                logger.warning("HERALD_MAX_COMMITS=%s is not a valid integer, ignoring",
                               max_commits)

        webhook = os.environ.get("HERALD_TEAMS_WEBHOOK")
        if webhook:
            # Applied later during resolve_groups; store on config for now
            self.config["_env_teams_webhook"] = webhook
            logger.info("HERALD_TEAMS_WEBHOOK set via environment")

    def _apply_env_webhook(self, groups: List[Dict[str, Any]]):
        """Apply HERALD_TEAMS_WEBHOOK to groups that lack a webhook URL."""
        webhook = self.config.get("_env_teams_webhook")
        if not webhook:
            return
        for group in groups:
            if not group.get("teams_webhook_url"):
                group["teams_webhook_url"] = webhook

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
                            logger.warning("Failed to load external config %s: %s",
                                           ext_path, e)
                    else:
                        group_name = group.get("name", group["config_file"])
                        logger.warning("External config file not found: %s "
                                       "(group '%s' will have no sources)",
                                       ext_path, group_name)
                resolved.append(group)
            self._apply_env_webhook(resolved)
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
            groups = [group]
            self._apply_env_webhook(groups)
            return groups

        return []

    def get_defaults(self) -> Dict[str, Any]:
        """Return merged defaults."""
        return self.config.get("defaults", {
            "time_window_days": 14,
            "max_commits": 20,
            "activity_types": ["commits", "pulls", "issues", "releases"]
        })

    def validate(self) -> bool:
        """Validate configuration without making API calls.

        Checks: groups exist, sources have repos, repo format is valid,
        AI backend is available, GITHUB_TOKEN is set.
        Returns True if all checks pass.
        """
        valid = True
        issues = 0

        # Check groups
        if not self.groups:
            print("FAIL: No groups configured.")
            valid = False
        else:
            print(f"OK: {len(self.groups)} group(s) configured")

        for group in self.groups:
            name = group.get("name", "unnamed")
            sources = group.get("sources", [])
            if not sources:
                print(f"WARN: Group '{name}' has no sources")
                issues += 1

            for src in sources:
                repos = src.get("repositories", [])
                if not repos:
                    print(f"WARN: Source in group '{name}' has no repositories")
                    issues += 1
                for repo in repos:
                    parts = repo.split('/')
                    if len(parts) != 2 or not all(parts):
                        print(f"FAIL: Invalid repository format: {repo} "
                              f"(expected owner/repo)")
                        valid = False

            team = group.get("team_context", {})
            if not team.get("name"):
                print(f"WARN: Group '{name}' has no team_context.name "
                      f"(AI summaries will be generic)")
                issues += 1

        # Check AI backend
        if self.ai_backend.validate():
            print(f"OK: AI backend ({self.ai_backend.backend_type}) is available")
        else:
            print(f"WARN: AI backend ({self.ai_backend.backend_type}) not found. "
                  f"Summaries will fall back to raw data.")
            issues += 1

        # Check GITHUB_TOKEN
        if os.environ.get('GITHUB_TOKEN'):
            print("OK: GITHUB_TOKEN is set (5000 requests/hour)")
        else:
            print("WARN: GITHUB_TOKEN not set (limited to 60 requests/hour)")
            issues += 1

        if valid and issues == 0:
            print("\nAll checks passed.")
        elif valid:
            print(f"\nPassed with {issues} warning(s).")
        else:
            print(f"\nValidation failed.")

        return valid

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

        Removes text before the first **TL;DR:** line or ### heading and trailing
        conversational lines after the last substantive content (bullets, headings,
        numbered lists). Falls back to original text if stripping would result in
        empty output.
        """
        if not text:
            return text

        lines = text.split('\n')

        # Strip preamble: find first ### heading or TL;DR line
        # Handle TL;DR variants: **TL;DR:**, **TL;DR**, TL;DR:
        start_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if (stripped.startswith('###')
                    or stripped.startswith('**TL;DR')
                    or stripped.startswith('TL;DR')):
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
            pulls = activity.get("pulls", [])

            # Deduplicate: collect SHAs associated with PRs (merge commits
            # and head SHAs) so they are not repeated in both sections
            pr_shas = set()
            for pr in pulls:
                if pr.get("merge_commit_sha"):
                    pr_shas.add(pr["merge_commit_sha"])
                if pr.get("head", {}).get("sha"):
                    pr_shas.add(pr["head"]["sha"])

            deduped_commits = [c for c in commits
                               if c.get("sha") not in pr_shas]
            skipped = len(commits) - len(deduped_commits)
            if skipped:
                logger.debug("Deduplicated %d commits already covered by PRs in %s",
                             skipped, repo)

            prompt += f"COMMITS ({len(deduped_commits)}):\n"
            for commit in deduped_commits[:20]:
                sha = commit['sha'][:7]
                message = commit['commit']['message'].split('\n')[0][:100]
                author = commit['commit']['author']['name']
                date = commit['commit']['author']['date']
                prompt += f"- {sha}: {message} by {author} on {date}\n"
            prompt += "\n"

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

**TL;DR:** <One sentence (max 25 words) capturing the single most important development or theme across all repositories in this digest.>

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

IMPORTANT: Use exactly the header levels shown above (### for the two section headers). Do not include any other top-level headers.

OUTPUT FORMAT: Output the TL;DR line followed by the two markdown sections (### Summary and ### Recommended Actions).
Do not include any introduction, preamble, conclusion, sign-off, or conversational text.
Start directly with "**TL;DR:**"."""

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
                logger.info("Using cached summary for %s", group_name)
                return cached

        logger.info("Generating AI summary for %s...", group_name)

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
            logger.warning("Failed to save cache to %s: %s", cache_path, e)

    def _prune_cache(self, max_age_days: int = 7):
        """Remove cache files older than max_age_days. Skips herald.log."""
        cutoff = datetime.now().timestamp() - (max_age_days * 86400)
        pruned = 0
        try:
            for f in self.cache_dir.iterdir():
                if f.name == "herald.log" or f.is_dir():
                    continue
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    pruned += 1
            if pruned:
                logger.debug("Pruned %d stale cache files (older than %d days)",
                             pruned, max_age_days)
        except Exception as e:
            logger.debug("Cache pruning failed: %s", e)

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

        logger.info("Detailed report saved to: %s", output_file)

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
            logger.error("No webhook URL provided.")
            return False

        logger.info("Posting digest to Microsoft Teams...")

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
                logger.info("Successfully posted digest to Teams channel.")
                return True
            else:
                logger.error("Error posting to Teams: HTTP %d", response.status_code)
                logger.error("Response: %s", response.text[:500])
                return False

        except requests.exceptions.RequestException as e:
            logger.error("Error posting to Teams: %s", e)
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
                logger.warning("Unknown source type '%s', skipping", src_type)
                continue

            # Merge defaults into source config
            merged_config = {**defaults, **src_config}
            source = source_cls(merged_config, self.cache_dir, self.force_refresh)

            if not source.validate():
                logger.error("Source validation failed for %s in group %s",
                             src_type, group_name)
                continue

            activity = source.fetch_activity(since)
            all_activity.extend(activity)

        if not all_activity:
            return f"No activity found for group {group_name}.\n", True

        # --prompt-only: output the assembled prompt and skip everything else
        if self.prompt_only:
            prompt = self.format_prompt(all_activity, team_context)
            return prompt, True

        # Save detailed report (skip in dry-run mode)
        if self.dry_run:
            logger.info("[dry-run] Would save detailed report for group '%s'",
                        group_name)
        else:
            self.save_detailed_report(group_name, all_activity, time_window_days)

        # Build output
        short_date = datetime.now().strftime('%b %d, %Y')
        repo_names = ", ".join(a["repository"].split('/')[-1] for a in all_activity)
        output = f"# {repo_names} Digest - {short_date}\n"
        output += f"*Group: {group_name}*\n\n"

        # Generate AI summary across all repos in this group (skip in dry-run mode)
        if self.dry_run:
            logger.info("[dry-run] Would call AI backend to summarize %d repo(s)",
                        len(all_activity))
            summary = None
            summary_failed = True
        else:
            summary = self.generate_summary(all_activity, team_context, group_name)
            summary_failed = (not summary or summary.startswith(RAW_ACTIVITY_HEADER))

        if not summary_failed:
            output += summary + "\n\n"
        elif self.dry_run:
            output += "_[dry-run] AI summary skipped_\n\n"
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
                         time_window_days: Optional[int] = None,
                         team_name: Optional[str] = None) -> str:
        """Generate digest across groups. Returns combined markdown output."""
        all_summaries_failed = True

        # If --repos is used, create a temporary single group
        if repositories:
            team_context = {}
            if team_name:
                team_context["name"] = team_name
            temp_group = {
                "name": "cli-repos",
                "sources": [
                    {
                        "type": "github",
                        "repositories": repositories,
                    }
                ],
                "team_context": team_context,
            }
            groups_to_process = [temp_group]
        elif group_names:
            groups_to_process = [g for g in self.groups if g.get("name") in group_names]
            missing = set(group_names) - {g.get("name") for g in groups_to_process}
            if missing:
                logger.warning("Groups not found: %s", ', '.join(missing))
        else:
            groups_to_process = self.groups

        if not groups_to_process:
            logger.error("No groups to process. Use --repos owner/repo or configure groups "
                         "in herald.config.json")
            sys.exit(1)

        # Pre-flight: warn if AI backend is unavailable (before expensive fetches)
        if not self.dry_run and not self.prompt_only and not self.ai_backend.validate():
            logger.warning("AI backend (%s) is not available. "
                           "Summaries will fall back to raw data.",
                           self.ai_backend.backend_type)

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
            logger.warning("All AI summaries failed. Check AI backend setup.")

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
        '--team-name',
        help='Team name for ad-hoc --repos runs (improves AI summary quality)'
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
        '--dry-run',
        action='store_true',
        help='Fetch activity but skip AI summarization, report saving, and Teams posting'
    )
    parser.add_argument(
        '--prompt-only',
        action='store_true',
        help='Output the assembled AI prompt to stdout instead of calling the AI backend'
    )
    parser.add_argument(
        '--validate',
        action='store_true',
        help='Validate configuration and check prerequisites without making API calls'
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

    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable debug-level logging'
    )
    verbosity.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Suppress info messages; show only warnings and errors'
    )

    args = parser.parse_args()

    # Configure logging before anything else
    setup_logging(verbose=args.verbose, quiet=args.quiet)

    # Resolve config path: CLI arg > HERALD_CONFIG env var > auto-discovery
    config_path = args.config or os.environ.get("HERALD_CONFIG")

    # Initialize
    herald = Herald(config_path=config_path, force_refresh=args.force,
                    dry_run=args.dry_run, prompt_only=args.prompt_only)

    # List groups and exit
    if args.list_groups:
        herald.list_groups()
        return

    # Validate config and exit
    if args.validate:
        valid = herald.validate()
        sys.exit(0 if valid else 1)

    # Parse repositories
    repositories = None
    if args.repos:
        repositories = [r.strip() for r in args.repos.split(',')]

    # Generate digest
    output = herald.generate_digest(
        group_names=args.group,
        repositories=repositories,
        time_window_days=args.days,
        team_name=args.team_name
    )

    # Output summary
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        logger.info("Summary written to: %s", args.output)
    else:
        # Digest output goes to stdout (not logging) so it can be piped/redirected
        print(output)

    # Post to Teams if requested
    if args.teams:
        if args.dry_run:
            webhooks = [g.get("teams_webhook_url") for g in herald.groups
                        if g.get("teams_webhook_url")]
            logger.info("[dry-run] Would post to %d Teams webhook(s)", len(webhooks))
        elif herald.all_summaries_failed:
            logger.warning("Skipping Teams post: AI summary generation failed.")
        else:
            # Post to each group's webhook
            for group in herald.groups:
                webhook = group.get("teams_webhook_url")
                if webhook:
                    herald.post_to_teams(output, webhook)


if __name__ == '__main__':
    main()
