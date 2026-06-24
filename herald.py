#!/usr/bin/env python3
"""
Herald - Multi-Source Repository Activity Tracker
Fetches recent activity from configured sources and generates AI-powered summaries.
"""

import copy
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

logger = logging.getLogger("herald")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RAW_ACTIVITY_HEADER = "**Raw Activity Data:**"
SECRET_FIELD_NAMES = {"teams_webhook_url"}

CONFIG_SEARCH_PATHS = [
    "./config/herald.json",
    "./herald.config.json",
    str(Path.home() / ".config" / "herald" / "herald.json"),
    str(Path.home() / ".herald.config.json"),
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "defaults": {
        "time_window_days": 14,
        "max_commits": 20,
        "activity_types": ["commits", "pulls", "issues", "releases"],
        "ai_backend": {
            "type": "claude-cli",
            "timeout": 600,
        },
    },
    "teams": [],
}


class _ColorFormatter(logging.Formatter):
    """Compact, color-coded log formatter for interactive terminal use."""

    COLORS = {
        logging.DEBUG:    "\033[2m",
        logging.INFO:     "\033[36m",
        logging.WARNING:  "\033[33m",
        logging.ERROR:    "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if not self.use_color:
            return f"{record.levelname}: {msg}"
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{msg}{self.RESET}"


def setup_logging(verbose: bool = False, quiet: bool = False):
    """Configure logging for Herald. Logs go to stderr so stdout stays clean."""
    herald_logger = logging.getLogger("herald")

    if verbose:
        herald_logger.setLevel(logging.DEBUG)
    elif quiet:
        herald_logger.setLevel(logging.WARNING)
    else:
        herald_logger.setLevel(logging.INFO)

    if not any(isinstance(h, logging.StreamHandler) for h in herald_logger.handlers):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(_ColorFormatter())
        herald_logger.addHandler(stderr_handler)


def _get_pr_state(pr: Dict[str, Any]) -> str:
    """Determine PR state: 'merged' if merged_at is set, otherwise return PR state."""
    return "merged" if pr.get("merged_at") else pr.get("state", "unknown")


def _build_github_headers(accept: str = "application/vnd.github.v3+json") -> Dict[str, str]:
    """Build GitHub API request headers with optional auth token."""
    headers = {
        "Accept": accept,
        "User-Agent": "Herald/2.0",
    }
    github_token = os.environ.get('GITHUB_TOKEN')
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    return headers


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
        filters = config.get("filters", {})
        self.exclude_authors: List[str] = filters.get("exclude_authors", [])
        self.exclude_titles: List[str] = filters.get("exclude_titles", [])
        self.exclude_labels: List[str] = filters.get("exclude_labels", [])

        self._compiled_title_patterns = []
        for pattern in self.exclude_titles:
            try:
                self._compiled_title_patterns.append(re.compile(pattern, re.IGNORECASE))
            except re.error as e:
                logger.warning("Invalid regex pattern '%s' in exclude_titles: %s (skipping)",
                               pattern, e)
        self._exclude_authors_set = set(self.exclude_authors)
        self._exclude_labels_set = set(self.exclude_labels)

        self.diff_keywords: List[str] = config.get("diff_keywords", [])
        self.max_diff_size: int = config.get("max_diff_size", 50000)
        self._diff_keywords_lower = [kw.lower() for kw in self.diff_keywords]

        self.fetch_comments: bool = config.get("fetch_comments", False)
        self.max_comments_per_item: int = config.get("max_comments_per_item", 5)

        deep = config.get("deep_analysis", {})
        self.deep_analysis_enabled: bool = deep.get("enabled", False)
        self.deep_clone_dir: Path = Path(deep.get("clone_dir", str(cache_dir / "repos")))
        self.deep_context_files: List[str] = deep.get("context_files", [
            "CLAUDE.md", "README.md", "README.rst", "ARCHITECTURE.md",
            "CONTRIBUTING.md", "docs/architecture.md",
        ])
        self.deep_max_file_size: int = deep.get("max_file_size", 50000)

    def validate(self) -> bool:
        valid = True
        for repo in self.repositories:
            parts = repo.split('/')
            if len(parts) != 2 or not all(parts):
                logger.error("Invalid repository format: %s (expected owner/repo)", repo)
                valid = False
        if valid and not os.environ.get('GITHUB_TOKEN'):
            logger.info("Tip: export GITHUB_TOKEN=... for higher rate limits")
        return valid

    def github_request(self, url: str, params: Optional[Dict] = None) -> Optional[Any]:
        headers = _build_github_headers()

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
                                    max_pages: int = 5) -> Optional[List[Dict]]:
        """Fetch paginated GitHub API results using page numbers."""
        all_results: List[Dict] = []
        page_params = dict(params or {})
        per_page = int(page_params.get("per_page", 100))

        for page in range(1, max_pages + 1):
            page_params["page"] = page
            data = self.github_request(url, page_params)

            if data is None or not isinstance(data, list):
                if page == 1:
                    return None
                break

            all_results.extend(data)

            if len(data) < per_page:
                break

        return all_results

    def _fetch_with_cache(self, repo: str, activity_type: str, url: str,
                          params: Dict, paginated: bool = False,
                          filter_fn=None) -> List[Dict]:
        """Generic fetch-with-cache pattern used by all activity type fetchers."""
        cache_path = self.get_cache_path(repo, activity_type)

        if self.is_cache_valid(cache_path):
            cached_data = self.load_cache(cache_path)
            if cached_data is not None:
                logger.debug("Using cached %s data", activity_type)
                if filter_fn:
                    cached_data = filter_fn(cached_data)
                return cached_data

        if paginated:
            data = self.github_request_paginated(url, params)
            fetch_failed = data is None
        else:
            data = self.github_request(url, params)
            fetch_failed = data is None

        if fetch_failed:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                logger.debug("Using stale cache for %s", activity_type)
                if filter_fn:
                    stale_cache = filter_fn(stale_cache)
                return stale_cache
            return []

        self.save_cache(cache_path, data)

        if filter_fn:
            data = filter_fn(data)
        return data

    def fetch_commits(self, repo: str, since: datetime) -> List[Dict]:
        url = f"https://api.github.com/repos/{repo}/commits"
        params = {"since": since.isoformat(), "per_page": self.max_commits}
        return self._fetch_with_cache(repo, "commits", url, params)

    def fetch_pulls(self, repo: str, since: datetime) -> List[Dict]:
        url = f"https://api.github.com/repos/{repo}/pulls"
        params = {"state": "all", "sort": "updated",
                  "direction": "desc", "per_page": 100}
        return self._fetch_with_cache(
            repo, "pulls", url, params, paginated=True,
            filter_fn=lambda data: [
                pr for pr in data
                if date_parser.parse(pr['updated_at']) >= since
            ],
        )

    def fetch_issues(self, repo: str, since: datetime) -> List[Dict]:
        url = f"https://api.github.com/repos/{repo}/issues"
        params = {"state": "all", "sort": "updated",
                  "direction": "desc", "per_page": 100}
        return self._fetch_with_cache(
            repo, "issues", url, params, paginated=True,
            filter_fn=lambda data: [
                issue for issue in data
                if 'pull_request' not in issue
                and date_parser.parse(issue['updated_at']) >= since
            ],
        )

    def fetch_releases(self, repo: str, since: datetime) -> List[Dict]:
        url = f"https://api.github.com/repos/{repo}/releases"
        params = {"per_page": 10}
        return self._fetch_with_cache(
            repo, "releases", url, params,
            filter_fn=lambda data: [
                r for r in data
                if r['published_at']
                and date_parser.parse(r['published_at']) >= since
            ],
        )

    def _has_filters(self) -> bool:
        return bool(self._exclude_authors_set or self._compiled_title_patterns
                     or self._exclude_labels_set)

    def apply_filters(self, activity: Dict[str, Any]) -> Dict[str, Any]:
        """Apply exclude filters to fetched activity data."""
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

        for key in ("pulls", "issues"):
            if key in activity:
                original = len(activity[key])
                activity[key] = [
                    item for item in activity[key]
                    if not (
                        _match_author(item.get("user", {}).get("login", ""))
                        or _match_title(item.get("title", ""))
                        or _match_labels(item)
                    )
                ]
                removed += original - len(activity[key])

        if removed:
            logger.info("  Filtered %d items", removed)

        return activity

    def _pr_matches_keywords(self, pr: Dict) -> bool:
        title = (pr.get("title") or "").lower()
        body = (pr.get("body") or "").lower()
        text = f"{title} {body}"
        return any(kw in text for kw in self._diff_keywords_lower)

    def _fetch_pr_diff(self, repo: str, pr_number: int) -> Optional[str]:
        url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
        headers = _build_github_headers("application/vnd.github.v3.diff")
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                logger.debug("Failed to fetch diff for %s#%d: HTTP %d",
                             repo, pr_number, response.status_code)
                return None
            return response.text
        except requests.exceptions.RequestException as e:
            logger.debug("Failed to fetch diff for %s#%d: %s", repo, pr_number, e)
            return None

    def _fetch_pr_files(self, repo: str, pr_number: int) -> Optional[List[Dict]]:
        url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
        data = self.github_request_paginated(url, {"per_page": 100}, max_pages=3)
        return data if data else None

    def _format_file_summary(self, files: List[Dict]) -> str:
        lines = []
        total_additions = 0
        total_deletions = 0
        for f in files:
            status = f.get("status", "modified")
            additions = f.get("additions", 0)
            deletions = f.get("deletions", 0)
            total_additions += additions
            total_deletions += deletions
            lines.append(f"  {status}: {f.get('filename', '?')} "
                         f"(+{additions}/-{deletions})")
        header = (f"  {len(files)} files changed, "
                  f"+{total_additions}/-{total_deletions} lines")
        return header + "\n" + "\n".join(lines)

    def enrich_prs_with_diffs(self, repo: str, pulls: List[Dict]) -> None:
        if not self._diff_keywords_lower:
            return

        enriched = 0
        for pr in pulls:
            is_merged = bool(pr.get("merged_at"))
            if not self._pr_matches_keywords(pr):
                continue
            if not is_merged and pr.get("state") != "open":
                continue

            pr_number = pr.get("number")
            if not pr_number:
                continue

            diff_text = self._fetch_pr_diff(repo, pr_number)
            if diff_text is None:
                continue

            if len(diff_text) <= self.max_diff_size:
                pr["_herald_diff"] = diff_text
                pr["_herald_diff_type"] = "full"
            else:
                files = self._fetch_pr_files(repo, pr_number)
                if files:
                    pr["_herald_diff"] = self._format_file_summary(files)
                    pr["_herald_diff_type"] = "summary"
                else:
                    pr["_herald_diff"] = diff_text[:self.max_diff_size] + "\n... [truncated]"
                    pr["_herald_diff_type"] = "truncated"

            enriched += 1

        if enriched:
            logger.info("  Fetched diffs for %d PR(s)", enriched)

    def _fetch_item_comments(self, repo: str, item_number: int,
                             is_pr: bool = False) -> List[Dict]:
        comments: List[Dict] = []

        url = f"https://api.github.com/repos/{repo}/issues/{item_number}/comments"
        data = self.github_request(url, {"per_page": 100})
        if data and isinstance(data, list):
            comments.extend(data)

        if is_pr:
            url = f"https://api.github.com/repos/{repo}/pulls/{item_number}/comments"
            data = self.github_request(url, {"per_page": 100})
            if data and isinstance(data, list):
                comments.extend(data)

        comments.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        return comments[:self.max_comments_per_item]

    @staticmethod
    def _format_comment(comment: Dict, max_body_len: int = 300) -> Dict:
        body = (comment.get("body") or "")
        if len(body) > max_body_len:
            body = body[:max_body_len] + "..."
        return {
            "user": comment.get("user", {}).get("login", "unknown"),
            "created_at": comment.get("created_at", ""),
            "body": body,
        }

    def enrich_with_comments(self, repo: str, items: List[Dict],
                             is_pr: bool = False) -> None:
        if not self.fetch_comments:
            return

        enriched = 0
        for item in items:
            item_number = item.get("number")
            if not item_number:
                continue

            raw_comments = self._fetch_item_comments(repo, item_number, is_pr=is_pr)
            if raw_comments:
                item["_herald_comments"] = [
                    self._format_comment(c) for c in raw_comments
                ]
                enriched += 1

        label = "PR" if is_pr else "issue"
        if enriched:
            logger.info("  Fetched comments for %d %s(s)", enriched, label)

    def _clone_or_pull_repo(self, repo: str) -> Optional[Path]:
        repo_dir = self.deep_clone_dir / repo.replace("/", "_")
        github_token = os.environ.get("GITHUB_TOKEN")

        if github_token:
            clone_url = f"https://x-access-token:{github_token}@github.com/{repo}.git"
        else:
            clone_url = f"https://github.com/{repo}.git"

        try:
            if repo_dir.exists() and (repo_dir / ".git").exists():
                result = subprocess.run(
                    ["git", "-C", str(repo_dir), "pull", "--ff-only", "--depth=1"],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    logger.debug("git pull failed for %s, trying fresh clone: %s",
                                 repo, result.stderr.strip())
                    shutil.rmtree(repo_dir, ignore_errors=True)
                    return self._clone_or_pull_repo(repo)
                logger.debug("Pulled latest for %s", repo)
                return repo_dir

            self.deep_clone_dir.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["git", "clone", "--depth=1", "--single-branch", clone_url, str(repo_dir)],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                logger.warning("Failed to clone %s: %s", repo, result.stderr.strip())
                return None
            logger.info("  Cloned %s for deep analysis", repo)
            return repo_dir

        except subprocess.TimeoutExpired:
            logger.warning("Timeout cloning/pulling %s", repo)
            return None
        except Exception as e:
            logger.warning("Error cloning/pulling %s: %s", repo, e)
            return None

    def _read_context_files(self, repo_dir: Path) -> Dict[str, str]:
        context: Dict[str, str] = {}

        for rel_path in self.deep_context_files:
            file_path = repo_dir / rel_path
            if not file_path.is_file():
                continue
            try:
                size = file_path.stat().st_size
                if size > self.deep_max_file_size:
                    logger.debug("Skipping %s (%.1f KB > %.1f KB limit)",
                                 rel_path, size / 1024, self.deep_max_file_size / 1024)
                    continue
                content = file_path.read_text(encoding="utf-8", errors="replace")
                context[rel_path] = content
                logger.debug("Read context file %s (%d bytes)", rel_path, len(content))
            except Exception as e:
                logger.debug("Failed to read %s: %s", rel_path, e)

        return context

    def _fetch_repo_context(self, repo: str) -> Optional[Dict[str, str]]:
        repo_dir = self._clone_or_pull_repo(repo)
        if not repo_dir:
            return None

        context = self._read_context_files(repo_dir)
        if context:
            logger.info("  Read %d context file(s) for deep analysis", len(context))
        return context if context else None

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

            if "pulls" in activity and self._diff_keywords_lower:
                self.enrich_prs_with_diffs(repo, activity["pulls"])

            if self.fetch_comments:
                if "pulls" in activity:
                    self.enrich_with_comments(repo, activity["pulls"], is_pr=True)
                if "issues" in activity:
                    self.enrich_with_comments(repo, activity["issues"], is_pr=False)

            if self.deep_analysis_enabled:
                repo_context = self._fetch_repo_context(repo)
                if repo_context:
                    activity["_herald_repo_context"] = repo_context

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
        self.teams = self.resolve_teams()
        self._apply_env_webhook(self.teams)
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
        default_config: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)

        if config_path is None:
            env_config = os.environ.get("HERALD_CONFIG")
            if env_config:
                config_path = env_config
            else:
                for path in CONFIG_SEARCH_PATHS:
                    if Path(path).exists():
                        config_path = str(path)
                        break

            if config_path is None:
                for path in ["./occ-digest.config.json",
                             Path.home() / ".occ-digest.config.json"]:
                    if Path(path).exists():
                        config_path = str(path)
                        logger.warning("Using legacy config %s. "
                                       "Consider renaming to config/herald.json",
                                       path)
                        break

        if config_path and Path(config_path).exists():
            self.config_dir = Path(config_path).resolve().parent
            try:
                with open(config_path, 'r') as f:
                    user_config = json.load(f)
                    default_config.update(user_config)
                    logger.info("Config: %s", config_path)
            except Exception as e:
                logger.warning("Failed to load config from %s: %s", config_path, e)
        elif config_path:
            logger.error("Config file not found: %s", config_path)
            sys.exit(1)
        else:
            logger.warning("No config file found. Using defaults. "
                           "Create config/herald.json or use --repos to get started. "
                           "See config/herald.example.json for reference.")

        return default_config

    @staticmethod
    def _parse_env_int(var_name: str, config_key: str, target: Dict[str, Any]):
        value = os.environ.get(var_name)
        if value:
            try:
                target[config_key] = int(value)
                logger.info("%s=%s overrides %s", var_name, value, config_key)
            except ValueError:
                logger.warning("%s=%s is not a valid integer, ignoring", var_name, value)

    def _apply_env_overrides(self):
        defaults = self.config.setdefault("defaults", {})
        self._parse_env_int("HERALD_DAYS", "time_window_days", defaults)
        self._parse_env_int("HERALD_MAX_COMMITS", "max_commits", defaults)

        webhook = os.environ.get("HERALD_TEAMS_WEBHOOK")
        if webhook:
            self.config["_env_teams_webhook"] = webhook
            logger.info("HERALD_TEAMS_WEBHOOK set via environment")

    def _apply_env_webhook(self, teams: List[Dict[str, Any]]):
        webhook = self.config.get("_env_teams_webhook")
        if not webhook:
            return
        for team in teams:
            if not team.get("teams_webhook_url"):
                team["teams_webhook_url"] = webhook

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

    def _load_team_secrets(self, team: Dict[str, Any]):
        """Load secrets from config/secrets/<team-name>.json and merge into the team dict."""
        team_name = team.get("name")
        if not team_name:
            return

        secrets_path = self.config_dir / "secrets" / f"{team_name}.json"
        if not secrets_path.exists():
            return

        try:
            with open(secrets_path, 'r') as f:
                secrets = json.load(f)
        except Exception as e:
            logger.warning("Failed to load secrets from %s: %s", secrets_path, e)
            return

        for field in SECRET_FIELD_NAMES:
            if field in secrets and secrets[field]:
                team[field] = secrets[field]

    def _collect_sub_team_sources(self, team: Dict[str, Any],
                                    all_teams_map: Dict[str, Dict[str, Any]],
                                    visited: set) -> List[Dict[str, Any]]:
        team_name = team.get("name", "unnamed")
        if team_name in visited:
            logger.warning("Circular sub-team reference: %s (chain: %s)",
                           team_name, ' -> '.join(visited))
            return []
        visited.add(team_name)
        all_sources = list(team.get("sources", []))
        for sub_name in team.get("sub_teams", []):
            sub_team = all_teams_map.get(sub_name)
            if not sub_team:
                logger.warning("Sub-team '%s' referenced by '%s' not found",
                               sub_name, team_name)
                continue
            sub_sources = self._collect_sub_team_sources(
                sub_team, all_teams_map, set(visited))
            all_sources.extend(sub_sources)
        return all_sources

    def _resolve_sub_teams_all(self, teams: List[Dict[str, Any]]):
        teams_map = {t.get("name", ""): t for t in teams if t.get("name")}
        for team in teams:
            if not team.get("sub_teams"):
                continue
            merged_sources = self._collect_sub_team_sources(team, teams_map, set())
            seen_repos: set = set()
            deduped_sources: List[Dict[str, Any]] = []
            for src in merged_sources:
                new_repos = [r for r in src.get("repositories", []) if r not in seen_repos]
                if new_repos:
                    seen_repos.update(new_repos)
                    deduped_src = dict(src)
                    deduped_src["repositories"] = new_repos
                    deduped_sources.append(deduped_src)
            team["sources"] = deduped_sources

    def resolve_teams(self) -> List[Dict[str, Any]]:
        """Parse teams from config. Wraps flat config as single team for backward compat."""
        config = self.config

        teams_key = "teams" if "teams" in config else "groups"
        if teams_key in config and config[teams_key]:
            if teams_key == "groups":
                logger.warning("Config uses deprecated 'groups' key. "
                               "Rename to 'teams' in your config file.")
            resolved = []
            for team_entry in config[teams_key]:
                if "config_file" in team_entry:
                    ext_path = self.config_dir / team_entry["config_file"]
                    if ext_path.exists():
                        try:
                            with open(ext_path, 'r') as f:
                                ext_config = json.load(f)
                            name = team_entry.get("name", ext_path.stem)
                            ext_config["name"] = name
                            resolved.append(ext_config)
                            continue
                        except Exception as e:
                            logger.warning("Failed to load external config %s: %s",
                                           ext_path, e)
                    else:
                        team_name = team_entry.get("name", team_entry["config_file"])
                        logger.warning("External config file not found: %s "
                                       "(team '%s' will have no sources)",
                                       ext_path, team_name)
                resolved.append(team_entry)
            for team in resolved:
                self._load_team_secrets(team)
            self._resolve_sub_teams_all(resolved)
            return resolved

        if "repositories" in config:
            defaults = config.get("defaults", {})
            team = {
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
                team["teams_webhook_url"] = webhook
            return [team]

        return []

    def get_defaults(self) -> Dict[str, Any]:
        """Return merged defaults."""
        return self.config.get("defaults", DEFAULT_CONFIG["defaults"])

    @staticmethod
    def _resolve_team_context(team: Dict[str, Any]) -> Dict[str, Any]:
        legacy = team.get("team_context", {})
        return {
            "name": team.get("display_name") or legacy.get("name", ""),
            "focus_areas": team.get("focus_areas") or legacy.get("focus_areas", []),
            "priorities": team.get("priorities") or legacy.get("priorities", []),
        }

    def validate(self) -> bool:
        """Validate configuration without making API calls."""
        valid = True
        issues = 0

        if not self.teams:
            print("FAIL: No teams configured.")
            valid = False
        else:
            print(f"OK: {len(self.teams)} team(s) configured")

        for team in self.teams:
            name = team.get("name", "unnamed")
            sources = team.get("sources", [])
            if not sources:
                print(f"WARN: Team '{name}' has no sources")
                issues += 1

            for src in sources:
                repos = src.get("repositories", [])
                if not repos:
                    print(f"WARN: Source in team '{name}' has no repositories")
                    issues += 1
                for repo in repos:
                    parts = repo.split('/')
                    if len(parts) != 2 or not all(parts):
                        print(f"FAIL: Invalid repository format: {repo} (expected owner/repo)")
                        valid = False

            tc = self._resolve_team_context(team)
            if not tc.get("name"):
                print(f"WARN: Team '{name}' has no display_name (AI summaries will be generic)")
                issues += 1

        all_team_names = {t.get("name", "") for t in self.teams}
        for team in self.teams:
            name = team.get("name", "unnamed")
            for sub in team.get("sub_teams", []):
                if sub not in all_team_names:
                    print(f"WARN: Team '{name}' references sub-team '{sub}' which does not exist")
                    issues += 1
                elif sub == name:
                    print(f"WARN: Team '{name}' references itself as sub-team")
                    issues += 1

        secrets_dir = self.config_dir / "secrets"
        if secrets_dir.is_dir():
            secret_files = list(secrets_dir.glob("*.json"))
            non_example = [f for f in secret_files if not f.name.endswith(".example.json")]
            if non_example:
                names = ", ".join(f.stem for f in non_example)
                print(f"OK: secrets/ directory found with files for: {names}")
            else:
                print("OK: secrets/ directory exists (no team secret files yet)")
        else:
            print("WARN: secrets/ directory not found. Create it to store webhook URLs.")
            issues += 1

        if self.ai_backend.validate():
            print(f"OK: AI backend ({self.ai_backend.backend_type}) is available")
        else:
            print(f"WARN: AI backend ({self.ai_backend.backend_type}) not found. "
                  f"Summaries will fall back to raw data.")
            issues += 1

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
            print("\nValidation failed.")

        return valid

    def _resolve_teams_to_process(self, team_names: Optional[List[str]] = None,
                                   repositories: Optional[List[str]] = None
                                   ) -> List[Dict[str, Any]]:
        if repositories:
            return [{
                "name": "cli-repos",
                "sources": [{"type": "github", "repositories": repositories}],
                "team_context": {},
            }]
        if team_names:
            teams = [t for t in self.teams if t.get("name") in team_names]
            missing = set(team_names) - {t.get("name") for t in teams}
            if missing:
                logger.warning("Teams not found: %s", ', '.join(missing))
            return teams
        return self.teams

    def fetch_team_activity(self, team: Dict[str, Any],
                            time_window_days: Optional[int] = None
                            ) -> Tuple[List[Dict[str, Any]], int, datetime]:
        """Fetch activity for one team. Returns (activity_list, days, since)."""
        team_name = team.get("name", "unnamed")
        defaults = self.get_defaults()

        if time_window_days is None:
            time_window_days = team.get("time_window_days",
                                          defaults.get("time_window_days", 14))

        since = datetime.now(timezone.utc) - timedelta(days=time_window_days)
        all_activity: List[Dict[str, Any]] = []

        for src_config in team.get("sources", []):
            src_type = src_config.get("type", "github")
            source_cls = SOURCE_REGISTRY.get(src_type)
            if not source_cls:
                logger.warning("Unknown source type '%s', skipping", src_type)
                continue

            merged_config = {**defaults, **src_config}
            source = source_cls(merged_config, self.cache_dir, self.force_refresh)

            if not source.validate():
                logger.error("Source validation failed for %s in team %s",
                             src_type, team_name)
                continue

            all_activity.extend(source.fetch_activity(since))

        return all_activity, time_window_days, since

    @staticmethod
    def serialize_activity(team: Dict[str, Any], activity_list: List[Dict[str, Any]],
                           time_window_days: int, since: datetime) -> Dict[str, Any]:
        """Build the activity JSON envelope written by `herald fetch`."""
        team_name = team.get("name", "unnamed")
        return {
            "meta": {
                "team": team_name,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "time_window_days": time_window_days,
                "since": since.isoformat(),
                "team_context": Herald._resolve_team_context(team),
            },
            "activity": activity_list,
        }

    @staticmethod
    def deserialize_activity(data: Dict[str, Any]) -> Tuple[Dict[str, Any],
                                                             List[Dict[str, Any]]]:
        """Parse activity JSON envelope into (meta, activity_list)."""
        meta = data.get("meta", {})
        activity = data.get("activity", [])
        if not isinstance(activity, list):
            raise ValueError("activity must be a list")
        return meta, activity

    def fetch_teams(self, team_names: Optional[List[str]] = None,
                    repositories: Optional[List[str]] = None,
                    time_window_days: Optional[int] = None,
                    output_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch activity for one or more teams and return JSON envelopes."""
        teams = self._resolve_teams_to_process(team_names, repositories)
        if not teams:
            logger.error("No teams to process.")
            sys.exit(1)

        envelopes: List[Dict[str, Any]] = []
        for team in teams:
            activity, days, since = self.fetch_team_activity(team, time_window_days)
            envelopes.append(self.serialize_activity(team, activity, days, since))

        if len(teams) == 1:
            payload = envelopes[0]
            text = json.dumps(payload, indent=2) + "\n"
            if output_path:
                path = Path(output_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                logger.info("Wrote activity JSON: %s", path)
            else:
                print(text, end="")
            return envelopes

        for envelope in envelopes:
            safe = envelope["meta"]["team"].replace(' ', '-').lower()
            path = Path(output_path) if output_path else self.cache_dir / f"activity-{safe}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
            logger.info("Wrote activity JSON: %s", path)

        return envelopes

    def list_teams(self):
        """Print configured teams and exit."""
        if not self.teams:
            print("No teams configured.")
            return
        print("Configured teams:")
        for team in self.teams:
            name = team.get("name", "unnamed")
            sources = team.get("sources", [])
            source_summary = []
            for src in sources:
                src_type = src.get("type", "unknown")
                repos = src.get("repositories", [])
                source_summary.append(f"{src_type}: {', '.join(repos)}")
            display = self._resolve_team_context(team).get("name", "")
            team_str = f" (display: {display})" if display else ""
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
                state = _get_pr_state(pr)
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

    def get_summary_cache_path(self, team_name: str) -> Path:
        safe_name = team_name.replace('/', '_').replace(' ', '_')
        date_str = datetime.now().strftime('%Y-%m-%d')
        return self.cache_dir / f"{safe_name}_summary_{date_str}.txt"

    def generate_summary(self, activity_list: List[Dict[str, Any]],
                          team_context: Dict[str, Any],
                          team_name: str) -> str:
        """Generate AI summary using the configured backend."""
        summary_cache = self.get_summary_cache_path(team_name)
        if not self.force_refresh and self.is_cache_valid(summary_cache):
            cached = self.load_cache_text(summary_cache)
            if cached:
                print(f"\n  Using cached summary for {team_name}")
                return cached

        print(f"\n  Generating AI summary for {team_name}...")

        prompt = self.format_prompt(activity_list, team_context)
        summary = self.ai_backend.summarize(prompt, team_name)

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

    def save_detailed_report(self, team_name: str, activity_list: List[Dict[str, Any]],
                              time_window_days: int):
        """Save detailed raw activity data to reports/<team_name>/."""
        safe_name = team_name.replace(' ', '-').lower()
        output_dir = Path(__file__).parent / "reports" / safe_name
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = output_dir / f"herald-detailed-{timestamp}.md"

        output = "# Herald Activity Detailed Report\n"
        output += f"Team: {team_name}\n"
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
                state = _get_pr_state(pr)
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
                state = _get_pr_state(pr)
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

    # -- team digest generation --

    def generate_team_digest(self, team: Dict[str, Any],
                               time_window_days: Optional[int] = None) -> Tuple[str, bool]:
        """Process one team end-to-end: fetch activity, generate summary, save report."""
        team_name = team.get("name", "unnamed")
        team_context = self._resolve_team_context(team)
        defaults = self.get_defaults()

        if time_window_days is None:
            time_window_days = team.get("time_window_days",
                                          defaults.get("time_window_days", 14))

        since = datetime.now(timezone.utc) - timedelta(days=time_window_days)

        all_activity: List[Dict[str, Any]] = []
        sources_config = team.get("sources", [])

        for src_config in sources_config:
            src_type = src_config.get("type", "github")
            source_cls = SOURCE_REGISTRY.get(src_type)
            if not source_cls:
                print(f"Warning: Unknown source type '{src_type}', skipping")
                continue

            merged_config = {**defaults, **src_config}
            source = source_cls(merged_config, self.cache_dir, self.force_refresh)

            if not source.validate():
                print(f"Error: Source validation failed for {src_type} in team {team_name}")
                continue

            activity = source.fetch_activity(since)
            all_activity.extend(activity)

        if not all_activity:
            return f"No activity found for team {team_name}.\n", True

        self.save_detailed_report(team_name, all_activity, time_window_days)

        short_date = datetime.now().strftime('%b %d, %Y')
        repo_names = ", ".join(a["repository"].split('/')[-1] for a in all_activity)
        output = f"# {repo_names} Digest - {short_date}\n"
        output += f"*Team: {team_name}*\n\n"

        summary = self.generate_summary(all_activity, team_context, team_name)
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

    def generate_digest(self, team_names: Optional[List[str]] = None,
                         repositories: Optional[List[str]] = None,
                         time_window_days: Optional[int] = None) -> str:
        """Generate digest across teams. Returns combined markdown output."""
        all_summaries_failed = True

        if repositories:
            temp_team = {
                "name": "cli-repos",
                "sources": [
                    {
                        "type": "github",
                        "repositories": repositories,
                    }
                ],
                "team_context": {},
            }
            teams_to_process = [temp_team]
        elif team_names:
            teams_to_process = [t for t in self.teams if t.get("name") in team_names]
            missing = set(team_names) - {t.get("name") for t in teams_to_process}
            if missing:
                print(f"Warning: Teams not found: {', '.join(missing)}")
        else:
            teams_to_process = self.teams

        if not teams_to_process:
            print("Error: No teams to process.")
            sys.exit(1)

        combined_output = ""
        for team in teams_to_process:
            result = self.generate_team_digest(team, time_window_days)
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
# Teams helpers (used by `herald post`)
# ---------------------------------------------------------------------------

def markdown_to_adaptive_card_blocks(markdown_text: str) -> List[Dict]:
    """Convert markdown text into Adaptive Card body blocks."""
    blocks = []
    for line in markdown_text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith('# '):
            blocks.append({
                "type": "TextBlock",
                "text": stripped[2:],
                "size": "ExtraLarge",
                "weight": "Bolder",
                "wrap": True,
            })
        elif stripped.startswith('## '):
            blocks.append({
                "type": "TextBlock",
                "text": stripped[3:],
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
                "separator": True,
            })
        elif stripped.startswith('### '):
            blocks.append({
                "type": "TextBlock",
                "text": stripped[4:],
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
                "separator": True,
            })
        elif stripped.startswith('---'):
            blocks.append({"type": "TextBlock", "text": " ", "separator": True})
        elif stripped.startswith(('- ', '* ')):
            blocks.append({
                "type": "TextBlock",
                "text": "• " + stripped[2:],
                "wrap": True,
            })
        else:
            blocks.append({"type": "TextBlock", "text": stripped, "wrap": True})

    return blocks


def post_digest_to_teams(digest_output: str, webhook_url: str) -> bool:
    """Post digest markdown to Microsoft Teams via Power Automate webhook."""
    if not webhook_url:
        logger.error("No webhook URL provided.")
        return False

    logger.info("Posting to Teams ...")
    card_body = markdown_to_adaptive_card_blocks(digest_output)
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": card_body,
            },
        }],
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if response.status_code in (200, 202):
            logger.info("Posted to Teams successfully")
            return True
        logger.error("Error posting to Teams: HTTP %d", response.status_code)
        logger.error("Response: %s", response.text[:500])
        return False
    except requests.exceptions.RequestException as e:
        logger.error("Error posting to Teams: %s", e)
        return False


# ---------------------------------------------------------------------------
# Config migration
# ---------------------------------------------------------------------------

def _migrate_config_layout():
    """Detect old directory layout and migrate to config/ structure.

    Old layout:
        herald.config.json, groups/, secrets/
    New layout:
        config/herald.json, config/teams/, config/secrets/, config/backups/
    """
    cwd = Path.cwd()
    old_config = cwd / "herald.config.json"
    old_groups = cwd / "groups"
    old_secrets = cwd / "secrets"
    new_config_dir = cwd / "config"

    if new_config_dir.exists() and (new_config_dir / "herald.json").exists():
        if old_config.exists():
            print("Warning: Both config/herald.json and herald.config.json exist. "
                  "Using config/herald.json. Remove the old file to silence this warning.")
        return

    if not old_config.exists():
        return

    print("Migrating to new config/ directory layout...")

    new_teams_dir = new_config_dir / "teams"
    new_secrets_dir = new_config_dir / "secrets"
    new_backups_dir = new_config_dir / "backups"
    for d in [new_config_dir, new_teams_dir, new_secrets_dir, new_backups_dir]:
        d.mkdir(parents=True, exist_ok=True)

    try:
        with open(old_config, 'r') as f:
            config_data = json.load(f)
    except Exception as e:
        print(f"Error: Failed to read {old_config} during migration: {e}")
        return

    if "groups" in config_data:
        config_data["teams"] = config_data.pop("groups")

    for team in config_data.get("teams", []):
        cf = team.get("config_file", "")
        if cf.startswith("groups/"):
            team["config_file"] = "teams/" + cf[len("groups/"):]

    new_config_path = new_config_dir / "herald.json"
    with open(new_config_path, 'w') as f:
        json.dump(config_data, f, indent=2)
        f.write("\n")
    print("  herald.config.json -> config/herald.json")

    if old_groups.is_dir():
        for src_file in sorted(old_groups.glob("*.json")):
            if ".backup." in src_file.name:
                dst = new_backups_dir / src_file.name
                shutil.move(str(src_file), str(dst))
                print(f"  groups/{src_file.name} -> config/backups/{src_file.name}")
            else:
                dst = new_teams_dir / src_file.name
                shutil.move(str(src_file), str(dst))
                print(f"  groups/{src_file.name} -> config/teams/{src_file.name}")
        try:
            old_groups.rmdir()
        except OSError:
            pass

    if old_secrets.is_dir():
        for src_file in sorted(old_secrets.glob("*.json")):
            dst = new_secrets_dir / src_file.name
            shutil.move(str(src_file), str(dst))
            print(f"  secrets/{src_file.name} -> config/secrets/{src_file.name}")
        try:
            old_secrets.rmdir()
        except OSError:
            pass

    backup_name = f"herald.config.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    shutil.move(str(old_config), str(new_backups_dir / backup_name))
    print(f"  Old herald.config.json backed up to config/backups/{backup_name}")
    print("Migration complete. Config is now in config/")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _add_common_args(parser: argparse.ArgumentParser):
    parser.add_argument('--config', help='Path to configuration file')
    parser.add_argument('--force', action='store_true',
                        help='Bypass activity cache')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Suppress informational messages')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable debug logging')


def _run_legacy_digest(herald: Herald, args: argparse.Namespace):
    logger.warning("Top-level digest flags are deprecated. "
                   "Use `/herald-digest` skill or legacy mode until Phase 4 removal.")
    repositories = None
    if args.repos:
        repositories = [r.strip() for r in args.repos.split(',')]

    output = herald.generate_digest(
        team_names=args.team,
        repositories=repositories,
        time_window_days=args.days,
    )

    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        logger.info("Output saved: %s", args.output)
    else:
        print("\n" + "=" * 80)
        print(output)
        print("=" * 80)

    if args.teams:
        if herald.all_summaries_failed:
            logger.warning("Skipping Teams post: AI summary generation failed.")
        else:
            for team in herald.teams:
                webhook = team.get("teams_webhook_url")
                if webhook:
                    herald.post_to_teams(output, webhook)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Herald - Multi-Source Repository Activity Tracker"
    )
    parser.add_argument('-q', '--quiet', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-v', '--verbose', action='store_true', help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command")

    fetch_parser = subparsers.add_parser(
        "fetch", help="Fetch activity and write structured JSON"
    )
    _add_common_args(fetch_parser)
    fetch_parser.add_argument('--team', '-t', action='append',
                              help='Team name (repeatable; default: all teams)')
    fetch_parser.add_argument('--repos',
                              help='Comma-separated repositories (overrides teams)')
    fetch_parser.add_argument('--days', type=int,
                              help='Number of days to look back')
    fetch_parser.add_argument('--output', '-o',
                              help='Output file (default: stdout for single team)')

    list_parser = subparsers.add_parser("list-teams", help="List configured teams")
    _add_common_args(list_parser)

    validate_parser = subparsers.add_parser("validate", help="Validate configuration")
    _add_common_args(validate_parser)

    post_parser = subparsers.add_parser(
        "post", help="Post digest markdown to Microsoft Teams (reads stdin)"
    )
    _add_common_args(post_parser)
    post_parser.add_argument('--team', '-t', help='Team name (loads webhook from secrets)')
    post_parser.add_argument('--webhook-url', help='Power Automate webhook URL')

    # Legacy top-level flags (deprecated digest path)
    parser.add_argument('--config', help=argparse.SUPPRESS)
    parser.add_argument('--repos', help=argparse.SUPPRESS)
    parser.add_argument('--days', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--output', help=argparse.SUPPRESS)
    parser.add_argument('--force', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--teams', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--team', '-t', action='append', dest='team', help=argparse.SUPPRESS)
    parser.add_argument('--group', '-g', action='append', dest='team', help=argparse.SUPPRESS)
    parser.add_argument('--list-teams', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--list-groups', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--validate', action='store_true', help=argparse.SUPPRESS)

    args = parser.parse_args()

    setup_logging(verbose=args.verbose, quiet=args.quiet)
    _migrate_config_layout()

    config_path = args.config or os.environ.get("HERALD_CONFIG")
    herald = Herald(config_path=config_path, force_refresh=args.force)

    if args.command == "fetch":
        repositories = None
        if args.repos:
            repositories = [r.strip() for r in args.repos.split(',')]
        herald.fetch_teams(
            team_names=args.team,
            repositories=repositories,
            time_window_days=args.days,
            output_path=args.output,
        )
        return

    if args.command == "list-teams":
        herald.list_teams()
        return

    if args.command == "validate":
        sys.exit(0 if herald.validate() else 1)

    if args.command == "post":
        digest = sys.stdin.read()
        if not digest.strip():
            logger.error("No digest content on stdin")
            sys.exit(1)
        webhook = args.webhook_url or os.environ.get("HERALD_TEAMS_WEBHOOK")
        if not webhook and args.team:
            secrets_path = herald.config_dir / "secrets" / f"{args.team}.json"
            if secrets_path.exists():
                try:
                    with open(secrets_path) as f:
                        webhook = json.load(f).get("teams_webhook_url")
                except Exception as e:
                    logger.error("Failed to read secrets from %s: %s", secrets_path, e)
        if not webhook:
            logger.error("No webhook URL. Use --webhook-url, --team, or HERALD_TEAMS_WEBHOOK")
            sys.exit(1)
        sys.exit(0 if post_digest_to_teams(digest, webhook) else 1)

    # Legacy hidden flags
    if args.list_teams or args.list_groups:
        herald.list_teams()
        return
    if args.validate:
        sys.exit(0 if herald.validate() else 1)

    legacy_digest = any([
        args.repos, args.days is not None, args.output,
        args.teams, args.team, args.force,
    ])
    if legacy_digest or args.command is None:
        _run_legacy_digest(herald, args)
        return

    parser.print_help()


if __name__ == '__main__':
    main()
