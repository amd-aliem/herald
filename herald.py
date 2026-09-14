#!/usr/bin/env python3
"""
Herald - Multi-Source Repository Activity Tracker
Fetches recent activity from configured sources for Herald skills and reporting.
"""

import copy
import json
import logging
import sys
import os
import argparse
import subprocess
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

    @staticmethod
    def _dedup_commits(activity: Dict[str, Any]) -> int:
        """Remove commits whose SHAs appear as a PR merge or head commit.

        Returns the number of commits removed.
        """
        commits = activity.get("commits", [])
        pulls = activity.get("pulls", [])
        if not commits or not pulls:
            return 0

        pr_shas: set = set()
        for pr in pulls:
            for key in ("merge_commit_sha", "head"):
                val = pr.get(key)
                if isinstance(val, str) and val:
                    pr_shas.add(val)
                elif isinstance(val, dict):
                    sha = val.get("sha")
                    if sha:
                        pr_shas.add(sha)

        if not pr_shas:
            return 0

        before = len(commits)
        activity["commits"] = [
            c for c in commits if c.get("sha") not in pr_shas
        ]
        removed = before - len(activity["commits"])
        if removed:
            logger.debug("  Deduped %d commit(s) already covered by PRs", removed)
        return removed

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
            self._dedup_commits(activity)

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
# Herald — main orchestrator
# ---------------------------------------------------------------------------

class Herald:
    """Main orchestrator for config resolution and activity fetching."""

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
        self._prune_cache()

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
                for path in ["./herald-legacy.config.json",
                             Path.home() / ".herald-legacy.config.json"]:
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

    @staticmethod
    def _preflight_check_team(team: Dict[str, Any]) -> bool:
        """Quick validation before fetching. Returns False if team has problems."""
        team_name = team.get("name", "unnamed")
        sources = team.get("sources", [])
        ok = True

        if not sources:
            logger.error("Team '%s' has no sources configured", team_name)
            return False

        for src in sources:
            repos = src.get("repositories", [])
            if not repos:
                logger.error("Team '%s' has a source with no repositories", team_name)
                ok = False
            for repo in repos:
                parts = repo.split('/')
                if len(parts) != 2 or not all(parts):
                    logger.error("Team '%s': invalid repository '%s' (expected owner/repo)",
                                 team_name, repo)
                    ok = False

        return ok

    def fetch_team_activity(self, team: Dict[str, Any],
                            time_window_days: Optional[int] = None
                            ) -> Tuple[List[Dict[str, Any]], int, datetime]:
        """Fetch activity for one team. Returns (activity_list, days, since)."""
        team_name = team.get("name", "unnamed")
        defaults = self.get_defaults()

        if not self._preflight_check_team(team):
            logger.error("Skipping team '%s' due to config errors. "
                         "Run 'python herald.py validate' for details.", team_name)
            days = time_window_days or defaults.get("time_window_days", 14)
            return [], days, datetime.now(timezone.utc) - timedelta(days=days)

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
    def _compute_stats(activity_list: List[Dict[str, Any]]) -> Dict[str, int]:
        """Compute summary stats across all repos in an activity list."""
        repos = len(activity_list)
        total_commits = 0
        total_prs = 0
        total_issues = 0
        total_releases = 0
        prs_with_diffs = 0

        for repo_activity in activity_list:
            total_commits += len(repo_activity.get("commits", []))
            pulls = repo_activity.get("pulls", [])
            total_prs += len(pulls)
            prs_with_diffs += sum(1 for p in pulls if "_herald_diff" in p)
            total_issues += len(repo_activity.get("issues", []))
            total_releases += len(repo_activity.get("releases", []))

        return {
            "repos": repos,
            "commits": total_commits,
            "pulls": total_prs,
            "pulls_with_diffs": prs_with_diffs,
            "issues": total_issues,
            "releases": total_releases,
        }

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
                "stats": Herald._compute_stats(activity_list),
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
                    output_path: Optional[str] = None,
                    emit: bool = True) -> List[Dict[str, Any]]:
        """Fetch activity for one or more teams and return JSON envelopes.

        When ``emit`` is False, envelopes are returned without writing files or
        printing to stdout (used by the in-process ``digest`` pipeline).
        """
        teams = self._resolve_teams_to_process(team_names, repositories)
        if not teams:
            logger.error("No teams to process.")
            sys.exit(1)

        envelopes: List[Dict[str, Any]] = []
        for team in teams:
            activity, days, since = self.fetch_team_activity(team, time_window_days)
            envelopes.append(self.serialize_activity(team, activity, days, since))

        if not emit:
            return envelopes

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

    def resolve_webhook(self, team_name: Optional[str] = None,
                        explicit_url: Optional[str] = None) -> Optional[str]:
        """Resolve a Teams webhook URL (explicit > env > team secrets file)."""
        if explicit_url:
            return explicit_url
        env_webhook = os.environ.get("HERALD_TEAMS_WEBHOOK")
        if env_webhook:
            return env_webhook
        if team_name:
            secrets_path = self.config_dir / "secrets" / f"{team_name}.json"
            if secrets_path.exists():
                try:
                    with open(secrets_path) as f:
                        return json.load(f).get("teams_webhook_url")
                except Exception as e:
                    logger.error("Failed to read secrets from %s: %s", secrets_path, e)
        return None

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



# ---------------------------------------------------------------------------
# Teams helpers (used by `herald post`)
# ---------------------------------------------------------------------------

def _prettify_tags(text: str) -> str:
    """Replace status/priority tags with styled indicators for cards."""
    text = text.replace('[HIGH PRIORITY] ', '\U0001f53a ')
    text = text.replace('[MERGED]', '\u2705 Merged')
    text = text.replace('[OPEN]', '\U0001f7e1 Open')
    text = text.replace('[CLOSED]', '\u26aa Closed')
    text = text.replace('[RELEASED]', '\U0001f680 Released')
    return text


def markdown_to_adaptive_card_blocks(markdown_text: str) -> List[Dict]:
    """Convert markdown text into Adaptive Card body blocks.

    Parses the digest markdown into structured sections and builds
    an Adaptive Card with:
    - Accent-styled H1 title container
    - TL;DR paragraph always visible
    - Per-repo ### sections: condensed bullet list (title+link) always
      visible, context sentences behind a toggle
    - Recommended Actions: same condensed/detail split
    - Zero-activity repos (from ## raw sections) collapsed into footer
    """

    # -- Phase 1: Parse into title, H3 sections, and H2 (repo) sections --
    title_text = None
    subtitle_text = None
    preamble: List[str] = []       # lines before first ### or ##
    h3_sections: List[tuple] = []  # (heading, body_lines)
    h2_sections: List[tuple] = []  # (heading, body_lines)

    current_heading = None
    current_level = 0
    current_body: List[str] = []

    def _save_current():
        nonlocal current_heading, current_body, current_level
        if current_heading is not None:
            if current_level == 3:
                h3_sections.append((current_heading, current_body))
            elif current_level == 2:
                h2_sections.append((current_heading, current_body))
        elif current_body:
            preamble.extend(current_body)
        current_heading = None
        current_body = []
        current_level = 0

    for line in markdown_text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('# ') and not stripped.startswith('## '):
            title_text = stripped[2:]
        elif stripped.startswith('## ') and not stripped.startswith('### '):
            _save_current()
            current_heading = stripped[3:]
            current_level = 2
        elif stripped.startswith('### '):
            _save_current()
            current_heading = stripped[4:]
            current_level = 3
        elif stripped.startswith('---'):
            continue  # skip raw separators
        else:
            if stripped.startswith('*Team:') or stripped.startswith('_Team:'):
                # Strip the surrounding markdown emphasis; Adaptive Card
                # TextBlock renders '*'/'_' literally rather than as italics.
                subtitle_text = stripped.strip('*_').strip()
            elif current_heading is not None:
                current_body.append(stripped)
            else:
                preamble.append(stripped)
    _save_current()

    # -- Phase 2: Build Adaptive Card blocks --
    blocks: List[Dict] = []

    # Title container
    if title_text:
        title_items = [{
            "type": "TextBlock",
            "text": title_text,
            "size": "ExtraLarge",
            "weight": "Bolder",
            "wrap": True
        }]
        if subtitle_text:
            title_items.append({
                "type": "TextBlock",
                "text": subtitle_text,
                "size": "Small",
                "isSubtle": True,
                "wrap": True,
                "spacing": "None"
            })
        blocks.append({
            "type": "Container",
            "style": "accent",
            "bleed": True,
            "items": title_items
        })

    # Preamble (TL;DR paragraph)
    if preamble:
        blocks.append({
            "type": "TextBlock",
            "text": "\n\n".join(preamble),
            "wrap": True
        })

    # H3 sections — split bullets at " — " into condensed + detail
    for idx, (heading, body) in enumerate(h3_sections):
        section_id = f"section-{idx}"
        expand_id = f"expand-{idx}"
        collapse_id = f"collapse-{idx}"

        condensed: List[str] = []
        details: List[str] = []
        has_details = False

        for bline in body:
            if (bline.startswith('- ') or bline.startswith('* ')) \
                    and ' \u2014 ' in bline:
                short, context = bline.split(' \u2014 ', 1)
                # Extract links from short part, fall back to context
                links = re.findall(
                    r'\[#?\d+[^]]*\]\([^)]+\)', short)
                if not links:
                    links = re.findall(
                        r'\[#?\d+[^]]*\]\([^)]+\)', context)
                link_str = (" (" + ", ".join(links) + ")"
                            if links else "")
                # Split attribution ("by @...") from the title line
                attr_match = re.search(r'\s+(by @.+)$', short)
                if attr_match:
                    title_line = short[:attr_match.start()]
                    attribution = attr_match.group(1)
                    condensed.append(
                        f"{title_line}\n\u2514\u2500 _{attribution}_")
                else:
                    # Append extracted links to condensed if not
                    # already present in the short part
                    if link_str and '[#' not in short:
                        condensed.append(f"{short} {link_str}")
                    else:
                        condensed.append(short)
                # Build detail line with title and links
                title_match = re.search(r'\*\*(.+?)\*\*', short)
                if title_match:
                    details.append(
                        f"- **{title_match.group(1)}**"
                        f"{link_str}: {context}")
                else:
                    details.append(f"- {context}")
                has_details = True
            else:
                condensed.append(bline)
                details.append(bline)

        # Heading — hyperlink repo name to GitHub
        display_heading = heading
        if '/' in heading and heading[0].isalpha():
            repo_url = f"https://github.com/{heading}"
            display_heading = f"[{heading}]({repo_url})"

        blocks.append({
            "type": "TextBlock",
            "text": display_heading,
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
            "separator": True
        })

        max_condensed = 5
        if condensed:
            shown = condensed[:max_condensed]
            overflow = len(condensed) - max_condensed
            blocks.append({
                "type": "TextBlock",
                "text": _prettify_tags(
                    "\n\n".join(shown)),
                "wrap": True
            })
            if overflow > 0:
                has_details = True  # ensure toggle shows
                blocks.append({
                    "type": "TextBlock",
                    "text": f"_+{overflow} more below..._",
                    "wrap": True,
                    "isSubtle": True,
                    "size": "Small",
                    "spacing": "Small"
                })

        if has_details:
            blocks.append({
                "type": "ActionSet",
                "id": expand_id,
                "actions": [{
                    "type": "Action.ToggleVisibility",
                    "title": "\u25b6 More context",
                    "targetElements": [
                        section_id, collapse_id, expand_id]
                }]
            })
            blocks.append({
                "type": "ActionSet",
                "id": collapse_id,
                "isVisible": False,
                "actions": [{
                    "type": "Action.ToggleVisibility",
                    "title": "\u25bc Less",
                    "targetElements": [
                        section_id, collapse_id, expand_id]
                }]
            })
            blocks.append({
                "type": "Container",
                "id": section_id,
                "isVisible": False,
                "items": [{
                    "type": "TextBlock",
                    "text": _prettify_tags(
                        "\n\n".join(details)),
                    "wrap": True
                }]
            })

    # H2 sections (raw activity counts) — only used for low-activity footer
    low_activity_repos: List[str] = []

    for heading, body in h2_sections:
        raw_line = next(
            (l for l in body
             if 'Raw activity:' in l or 'raw activity:' in l.lower()),
            None
        )
        if raw_line:
            counts = re.findall(
                r'(\d+)\s+(?:commits|PRs|issues|releases)', raw_line)
            total = sum(int(c) for c in counts) if counts else 0
            if total == 0:
                short = heading.split('/')[-1] if '/' in heading \
                    else heading
                low_activity_repos.append(short)

    if low_activity_repos:
        names = ", ".join(low_activity_repos)
        blocks.append({
            "type": "TextBlock",
            "text": f"_No activity: {names}_",
            "wrap": True,
            "separator": True,
            "isSubtle": True,
            "size": "Small",
            "spacing": "Large"
        })

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
                "version": "1.5",
                "msteams": {"width": "Full"},
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
# LLM client — direct Anthropic Messages API (gateway/proxy endpoint compatible)
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Raised when an LLM request fails."""


class AnthropicClient:
    """Minimal client for the Anthropic Messages API.

    Talks directly to any Anthropic-compatible ``/v1/messages`` endpoint, so it
    works against api.anthropic.com or a proxy/corporate LLM gateway. All
    connection settings are taken from an ``ai_backend`` config block, with
    environment variables taking precedence so the same image runs unchanged
    across environments.

    Resolution order (env overrides config):
      - base_url:  ANTHROPIC_BASE_URL   | ai_backend.base_url
      - api_key:   ANTHROPIC_API_KEY    | ai_backend.api_key
      - model:     ANTHROPIC_MODEL      | ai_backend.model
      - headers:   ANTHROPIC_CUSTOM_HEADERS (a "Key: Value" string, one per
                   line) merged over ai_backend.headers (a dict)
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, backend: Optional[Dict[str, Any]] = None):
        backend = backend or {}

        self.base_url = (os.environ.get("ANTHROPIC_BASE_URL")
                         or backend.get("base_url")
                         or self.DEFAULT_BASE_URL).rstrip("/")
        self.api_key = os.environ.get("ANTHROPIC_API_KEY") or backend.get("api_key", "")
        self.model = (os.environ.get("ANTHROPIC_MODEL")
                      or backend.get("model")
                      or "Claude-Sonnet-4-5")
        self.timeout = int(backend.get("timeout", 600))
        self.max_tokens = int(backend.get("max_tokens", 4096))

        # Extra headers: config dict first, then ANTHROPIC_CUSTOM_HEADERS on top.
        self.extra_headers: Dict[str, str] = {}
        cfg_headers = backend.get("headers", {})
        if isinstance(cfg_headers, dict):
            for k, v in cfg_headers.items():
                self.extra_headers[str(k)] = str(v)
        self.extra_headers.update(self._parse_custom_headers(
            os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")))

    @staticmethod
    def _parse_custom_headers(raw: str) -> Dict[str, str]:
        """Parse a 'Key: Value' string (one header per line) into a dict."""
        headers: Dict[str, str] = {}
        for line in raw.replace("\\n", "\n").split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()
        return headers

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str,
                 max_tokens: Optional[int] = None) -> str:
        """Send a single-turn message and return the concatenated text output."""
        if not self.api_key:
            raise LLMError("No API key. Set ANTHROPIC_API_KEY or ai_backend.api_key.")

        url = f"{self.base_url}/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
        }
        headers.update(self.extra_headers)

        payload = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

        try:
            response = requests.post(url, headers=headers, json=payload,
                                     timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise LLMError(f"Request to {url} failed: {e}") from e

        if response.status_code != 200:
            raise LLMError(f"HTTP {response.status_code} from {url}: "
                           f"{response.text[:500]}")

        try:
            data = response.json()
        except ValueError as e:
            raise LLMError(f"Invalid JSON response from {url}: {e}") from e

        parts = [block.get("text", "")
                 for block in data.get("content", [])
                 if block.get("type") == "text"]
        text = "".join(parts).strip()
        if not text:
            raise LLMError(f"Empty response from {url}: {json.dumps(data)[:300]}")
        return text


# ---------------------------------------------------------------------------
# Digest generation — ports the herald-rate-pr and herald-analyze skills
# ---------------------------------------------------------------------------

# System prompt ported verbatim in intent from .claude/skills/herald-rate-pr.
RATE_PR_SYSTEM_PROMPT = """\
You rate how relevant a single GitHub pull request is to a team's focus areas \
and priorities, on a 1-5 scale.

You receive JSON with:
- pr — PR object (title, body, user, state, _herald_diff, _herald_diff_type)
- repo — repository name (owner/repo)
- team_context — { name, focus_areas, priorities }

Evaluate relevance using these signals, in priority order:
1. Priorities match — Does the PR touch team_context.priorities? A match with
   priorities[0] pushes toward 5.
2. Focus areas match — Check title, body, and diff file paths against each
   focus_areas entry.
3. Diff content — If _herald_diff is present, scan changed file paths and code
   for team-relevant subsystems.
4. Title/body keywords — Terms that align with the team's domain.

When _herald_diff is absent or _herald_diff_type is "truncated", rate on
title/body/file summary only; do not penalize for missing diff data.
Rate from the team's perspective, not general importance. A critical kernel fix
is a 2 for a networking team if it doesn't touch networking.

Scale:
5 = critical — Directly impacts the team's top priority; requires attention
4 = relevant — Clearly within focus areas; team should be aware
3 = somewhat relevant — Touches adjacent areas; useful context
2 = tangential — Loosely related; skim-worthy at best
1 = unrelated — No connection to team focus

Respond with EXACTLY two lines and nothing else:
RELEVANCE: <1-5>
REASON: <one sentence>"""

# System prompt ported verbatim in intent from .claude/skills/herald-analyze.
ANALYZE_SYSTEM_PROMPT = """\
You read a Herald activity JSON envelope and produce a team digest in markdown.

Procedure:
1. Extract meta.team_context (name, focus_areas, priorities) and the activity
   array.
2. Triage repos — Sort by combined relevance: sum of _herald_relevance.score
   across PRs, then total item count. Drop repos with zero meaningful activity
   (only bot commits, no PRs/issues/releases).
3. For each kept repo, summarize commits, PRs, issues, releases. Commits that
   duplicate PR merge/head SHAs are already removed.
   - PRs with _herald_relevance: use the score to order and prioritize (lead
     with score 4-5 items) and weave the reason into the summary, but do NOT
     print the numeric score. Never emit a "[RELEVANCE: N]" tag.
   - PRs with _herald_diff: reference key changed files/subsystems; never paste
     raw diff.
   - _herald_comments: surface review blockers or contention in the sentence.
   - _herald_repo_context: use project docs to explain why a change matters.
   - Standalone commits: group minor ones into "N other commits". Give
     individual bullets only to notable standalone commits.
4. Cap at ~8 bullets per repo. Combine related issues/PRs where possible.
5. Repos with only 1-2 minor items: fold into a "Minor activity" section.

Output format:

**TL;DR:** <2-3 sentences leading with items matching priorities[0], then key themes>

### owner/repo
- [HIGH PRIORITY] **Title** [STATUS] ([#N](url)) PR by @author — What changed and why it matters
- **Title** [STATUS] ([#N](url)) PR by @author — What changed and why it matters
- **Title** [STATUS] ([#N](url)) Issue — What the issue reports
- **Title** [STATUS] ([#N](url), +N more) Summary — Grouped description
- **Title** [RELEASED] ([vX.Y.Z](url)) Release
- **Title** Commit (abc1234) — What the commit does
- N other commits including X, Y, and Z.

### Minor activity
- **owner/repo**: brief note

### Recommended Actions
- **Action title** — Recommendation with relevant links

Format rules:
- [STATUS] values (square brackets required): [MERGED], [OPEN], [CLOSED], [RELEASED]
- Item type label required after the link/status: PR, Issue, Summary, Release,
  Commit, or Commits.
  - PRs: [STATUS] ([#N](url)) PR by @author
  - Issues: [STATUS] ([#N](url)) Issue
  - Grouped: [STATUS] ([#N](url), +N more) Summary
  - Releases: [RELEASED] ([tag](url)) Release
  - Commits: Commit (sha) or Commits (sha, sha)
- "by @author" required for PRs; omit for issues, summaries, releases, commits.
- Start directly with **TL;DR:** — no preamble, heading, or sign-off.
- ### headings for each active repo and Recommended Actions.
- Items matching team_context.priorities[0] go first in their repo section,
  tagged [HIGH PRIORITY]. This tag conveys importance; do not add relevance
  scores. Never emit "[RELEVANCE: N]" anywhere in the output.
- Em dash ( — ) separating type/attribution from context sentence is required.
- 1-3 recommended actions; fewer is fine. Be specific, not generic.

Output only the digest markdown. No preamble or commentary."""


def _parse_relevance(text: str) -> Optional[Dict[str, Any]]:
    """Parse a RELEVANCE/REASON response into a relevance dict."""
    score_match = re.search(r'RELEVANCE:\s*([1-5])', text)
    reason_match = re.search(r'REASON:\s*(.+)', text)
    if not score_match:
        return None
    return {
        "score": int(score_match.group(1)),
        "reason": reason_match.group(1).strip() if reason_match else "",
    }


def rate_prs(client: AnthropicClient, envelope: Dict[str, Any]) -> Tuple[int, int]:
    """Rate every PR with a diff that isn't already rated. Mutates envelope.

    Returns (rated, skipped).
    """
    meta = envelope.get("meta", {})
    team_context = meta.get("team_context", {})
    rated = 0
    skipped = 0

    for repo_activity in envelope.get("activity", []):
        repo = repo_activity.get("repository", "unknown")
        for pr in repo_activity.get("pulls", []):
            if "_herald_diff" not in pr:
                continue
            if "_herald_relevance" in pr:
                skipped += 1
                continue

            pr_input = json.dumps({
                "pr": {k: v for k, v in pr.items()
                       if k in ("number", "title", "body", "state", "merged_at",
                                "user", "_herald_diff", "_herald_diff_type",
                                "_herald_comments")},
                "repo": repo,
                "team_context": team_context,
            }, indent=2)

            try:
                response = client.complete(RATE_PR_SYSTEM_PROMPT, pr_input,
                                           max_tokens=200)
                relevance = _parse_relevance(response)
            except LLMError as e:
                logger.warning("  Rating PR %s#%s failed: %s",
                               repo, pr.get("number"), e)
                continue

            if relevance is None:
                logger.warning("  Could not parse rating for %s#%s",
                               repo, pr.get("number"))
                continue

            pr["_herald_relevance"] = relevance
            rated += 1

    return rated, skipped


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "\n... [truncated]"
    return value


# Per-repo caps on items sent to the analyze prompt. Raw GitHub objects are huge
# and unbounded (a busy repo can return hundreds of PRs), so we slim each item to
# the fields the digest actually uses and cap counts to stay within the LLM's
# context window.
MAX_PRS_PER_REPO = 40
MAX_ISSUES_PER_REPO = 25
MAX_COMMITS_PER_REPO = 30
MAX_RELEASES_PER_REPO = 10


def _slim_pr(pr: Dict[str, Any]) -> Dict[str, Any]:
    slim = {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": _get_pr_state(pr),
        "author": (pr.get("user") or {}).get("login", ""),
        "url": pr.get("html_url"),
        "body": _truncate(pr.get("body") or "", 600),
    }
    if "_herald_relevance" in pr:
        slim["_herald_relevance"] = pr["_herald_relevance"]
    if "_herald_diff" in pr:
        slim["_herald_diff"] = _truncate(pr["_herald_diff"], 3000)
        slim["_herald_diff_type"] = pr.get("_herald_diff_type")
    if "_herald_comments" in pr:
        slim["_herald_comments"] = pr["_herald_comments"]
    return slim


def _slim_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    slim = {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "author": (issue.get("user") or {}).get("login", ""),
        "url": issue.get("html_url"),
        "body": _truncate(issue.get("body") or "", 400),
        "labels": [lbl.get("name", "") for lbl in issue.get("labels", [])],
    }
    if "_herald_comments" in issue:
        slim["_herald_comments"] = issue["_herald_comments"]
    return slim


def _slim_commit(commit: Dict[str, Any]) -> Dict[str, Any]:
    sha = commit.get("sha", "")
    return {
        "sha": sha[:7],
        "message": (commit.get("commit", {}).get("message", "") or "").split("\n")[0],
        "author": commit.get("commit", {}).get("author", {}).get("name", ""),
        "url": commit.get("html_url"),
    }


def _slim_release(release: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tag": release.get("tag_name"),
        "name": release.get("name"),
        "url": release.get("html_url"),
        "published_at": release.get("published_at"),
    }


def _pr_sort_key(pr: Dict[str, Any]) -> Tuple[int, str]:
    score = (pr.get("_herald_relevance") or {}).get("score", 0)
    return (score, pr.get("updated_at", ""))


def _compact_activity(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Build a slim, capped copy of the activity for the analyze prompt.

    Extracts only the fields the digest uses and caps items per repo so the
    prompt fits the LLM context window regardless of how busy a repo is.
    """
    compact: Dict[str, Any] = {"meta": envelope.get("meta", {}), "activity": []}

    for repo_activity in envelope.get("activity", []):
        pulls = sorted(repo_activity.get("pulls", []),
                       key=_pr_sort_key, reverse=True)[:MAX_PRS_PER_REPO]
        issues = repo_activity.get("issues", [])[:MAX_ISSUES_PER_REPO]
        commits = repo_activity.get("commits", [])[:MAX_COMMITS_PER_REPO]
        releases = repo_activity.get("releases", [])[:MAX_RELEASES_PER_REPO]

        slim_repo: Dict[str, Any] = {
            "repository": repo_activity.get("repository"),
            "counts": {
                "pulls": len(repo_activity.get("pulls", [])),
                "issues": len(repo_activity.get("issues", [])),
                "commits": len(repo_activity.get("commits", [])),
                "releases": len(repo_activity.get("releases", [])),
            },
            "pulls": [_slim_pr(p) for p in pulls],
            "issues": [_slim_issue(i) for i in issues],
            "commits": [_slim_commit(c) for c in commits],
            "releases": [_slim_release(r) for r in releases],
        }

        ctx = repo_activity.get("_herald_repo_context")
        if isinstance(ctx, dict):
            slim_repo["_herald_repo_context"] = {
                k: _truncate(v, 1500) for k, v in ctx.items()
            }

        compact["activity"].append(slim_repo)

    return compact


def generate_digest(client: AnthropicClient, envelope: Dict[str, Any]) -> str:
    """Produce digest markdown from a rated activity envelope."""
    compact = _compact_activity(envelope)
    user = json.dumps(compact, indent=2)
    return client.complete(ANALYZE_SYSTEM_PROMPT, user, max_tokens=8192)


def _format_date_range(since_iso: str, days: Optional[int]) -> str:
    """Render a 'Aug 31 – Sep 14, 2026 (N days)' range from the meta fields.

    Falls back gracefully if ``since`` is missing or unparseable.
    """
    end = datetime.now()
    start = None
    if since_iso:
        try:
            start = date_parser.parse(since_iso)
        except (ValueError, TypeError):
            start = None

    suffix = f" ({days} days)" if days else ""
    if start is None:
        return f"through {end.strftime('%b %-d, %Y')}{suffix}"

    # Omit the year on the start date when both ends share it.
    start_fmt = "%b %-d" if start.year == end.year else "%b %-d, %Y"
    return (f"{start.strftime(start_fmt)} – "
            f"{end.strftime('%b %-d, %Y')}{suffix}")


def prepend_digest_header(digest_md: str, envelope: Dict[str, Any]) -> str:
    """Prepend a deterministic title + team/date subtitle to digest markdown.

    Herald knows the team name and date window from the envelope meta, so the
    header is generated in code rather than asked of the LLM. The Teams card
    builder reads the ``# `` title and ``*Team:`` subtitle to render its header.
    """
    meta = envelope.get("meta", {})
    team_context = meta.get("team_context", {})
    name = team_context.get("name") or meta.get("team", "Activity")
    date_range = _format_date_range(meta.get("since", ""),
                                    meta.get("time_window_days"))

    title = f"# {name} — Activity Digest"
    subtitle = f"*Team: {name} | {date_range}*"
    return f"{title}\n{subtitle}\n\n{digest_md.lstrip()}"


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


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Herald - Multi-Source Repository Activity Tracker"
    )
    parser.add_argument('-q', '--quiet', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-v', '--verbose', action='store_true', help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command")

    fetch_parser = subparsers.add_parser(
        "fetch", help="Fetch activity and write structured JSON",
        description="Fetch activity from configured sources and write structured JSON.",
        epilog="Examples:\n"
               "  python herald.py fetch -t my-team\n"
               "  python herald.py fetch -t my-team --days 7 --force -o activity.json\n"
               "  python herald.py fetch -t team-a -t team-b\n"
               "  python herald.py fetch --repos owner/repo1,owner/repo2\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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

    list_parser = subparsers.add_parser(
        "list-teams", help="List configured teams",
        description="Show all configured teams and their sources.",
    )
    _add_common_args(list_parser)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate configuration",
        description="Run pre-flight checks on config, secrets, and environment.",
        epilog="Examples:\n"
               "  python herald.py validate\n"
               "  python herald.py validate --config /path/to/config.json\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_args(validate_parser)

    post_parser = subparsers.add_parser(
        "post", help="Post digest markdown to Microsoft Teams (reads stdin)",
        description="Post a digest to Microsoft Teams via Power Automate webhook.\n"
                    "Reads markdown from stdin.",
        epilog="Examples:\n"
               "  python herald.py post --team my-team < digest.md\n"
               "  python herald.py post --webhook-url \"$URL\" < digest.md\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_args(post_parser)
    post_parser.add_argument('--team', '-t', help='Team name (loads webhook from secrets)')
    post_parser.add_argument('--webhook-url', help='Power Automate webhook URL')

    digest_parser = subparsers.add_parser(
        "digest", help="Fetch, rate PRs, and generate a digest via the LLM API",
        description="Full pipeline: fetch activity, rate PRs for relevance, and\n"
                    "generate a markdown digest by calling the Anthropic Messages\n"
                    "API directly (no Claude Code required). Configure the endpoint\n"
                    "with ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL,\n"
                    "and ANTHROPIC_CUSTOM_HEADERS, or an ai_backend config block.",
        epilog="Examples:\n"
               "  python herald.py digest --team my-team\n"
               "  python herald.py digest -t my-team --days 7 --force -o digest.md\n"
               "  python herald.py digest -t my-team --post\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_args(digest_parser)
    digest_parser.add_argument('--team', '-t', action='append',
                               help='Team name (repeatable; default: all teams)')
    digest_parser.add_argument('--days', type=int, help='Number of days to look back')
    digest_parser.add_argument('--output', '-o',
                               help='Output digest file (default: reports/<team>/'
                                    'herald-digest-<date>.md)')
    digest_parser.add_argument('--post', action='store_true',
                               help='Post the digest to Teams after generating it')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

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
        # A Herald digest starts either with the generated "# ... Activity
        # Digest" header or directly with the TL;DR paragraph.
        _head = digest.lstrip()
        if not (_head.startswith("**TL;DR:**") or _head.startswith("# ")):
            logger.warning("Digest does not look like a Herald digest "
                           "(no title or '**TL;DR:**' at the start)")
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

    if args.command == "digest":
        backend = herald.get_defaults().get("ai_backend", {})
        client = AnthropicClient(backend)
        if not client.is_configured():
            logger.error("No API key. Set ANTHROPIC_API_KEY or ai_backend.api_key "
                         "in config.")
            sys.exit(1)
        logger.info("LLM: %s @ %s", client.model, client.base_url)

        envelopes = herald.fetch_teams(team_names=args.team,
                                       time_window_days=args.days, emit=False)
        exit_code = 0
        today = datetime.now().strftime("%Y-%m-%d")

        for envelope in envelopes:
            team_name = envelope["meta"]["team"]
            stats = envelope["meta"].get("stats", {})
            if not envelope.get("activity"):
                logger.warning("No activity for '%s'; skipping digest.", team_name)
                continue
            logger.info("Team '%s': %d repos, %d PRs, %d commits",
                        team_name, stats.get("repos", 0),
                        stats.get("pulls", 0), stats.get("commits", 0))

            rated, skipped = rate_prs(client, envelope)
            logger.info("  Rated %d PRs (skipped %d already-rated)", rated, skipped)

            try:
                digest_md = generate_digest(client, envelope)
            except LLMError as e:
                logger.error("  Digest generation failed for '%s': %s", team_name, e)
                exit_code = 1
                continue

            digest_md = prepend_digest_header(digest_md, envelope)

            if args.output and len(envelopes) == 1:
                out_path = Path(args.output)
            else:
                out_path = (herald.config_dir.parent / "reports" / team_name
                            / f"herald-digest-{today}.md")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(digest_md + "\n", encoding="utf-8")
            logger.info("  Digest saved to %s", out_path)

            if args.post:
                webhook = herald.resolve_webhook(team_name=team_name)
                if not webhook:
                    logger.error("  --post requested but no webhook for '%s'", team_name)
                    exit_code = 1
                elif not post_digest_to_teams(digest_md, webhook):
                    exit_code = 1

        sys.exit(exit_code)



if __name__ == '__main__':
    main()
