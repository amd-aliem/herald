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
import io
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

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, IntPrompt, Confirm
    from rich.syntax import Syntax
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

try:
    from textual.app import App, ComposeResult
    from textual.screen import Screen, ModalScreen
    from textual.widgets import Header, Footer, Static, DataTable, ListView, ListItem, Label, Input
    from textual.containers import Container, VerticalScroll
    from textual.binding import Binding
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RAW_ACTIVITY_HEADER = "**Raw Activity Data:**"
SECRET_FIELD_NAMES = {"teams_webhook_url"}


class _ColorFormatter(logging.Formatter):
    """Compact, color-coded log formatter for interactive terminal use."""

    COLORS = {
        logging.DEBUG:    "\033[2m",       # dim
        logging.INFO:     "\033[36m",      # cyan
        logging.WARNING:  "\033[33m",      # yellow
        logging.ERROR:    "\033[31m",      # red
        logging.CRITICAL: "\033[1;31m",    # bold red
    }
    SYMBOLS = {
        logging.DEBUG:    "  ",
        logging.INFO:     "  ",
        logging.WARNING:  "  ",
        logging.ERROR:    "  ",
        logging.CRITICAL: "  ",
    }
    RESET = "\033[0m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if not self.use_color:
            return f"{record.levelname}: {msg}"
        color = self.COLORS.get(record.levelno, "")
        sym = self.SYMBOLS.get(record.levelno, "")
        return f"{color}{sym}{msg}{self.RESET}"


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

    # stderr handler — color-coded format for interactive use
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(_ColorFormatter())
    herald_logger.addHandler(stderr_handler)


def _is_cache_valid(cache_path: Path, cache_ttl: int, force_refresh: bool) -> bool:
    """Check if a cache file exists and is within its TTL."""
    if force_refresh:
        return False
    if not cache_path.exists():
        return False
    age = datetime.now().timestamp() - cache_path.stat().st_mtime
    return age < cache_ttl


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
        return _is_cache_valid(cache_path, self.cache_ttl, self.force_refresh)

    def load_cache(self, cache_path: Path) -> Optional[Any]:
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return None
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
            logger.info("Tip: export GITHUB_TOKEN=... for higher rate limits")
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

    def _fetch_with_cache(self, repo: str, activity_type: str, url: str,
                          params: Dict, paginated: bool = False,
                          filter_fn=None) -> List[Dict]:
        """Generic fetch-with-cache pattern used by all activity type fetchers."""
        cache_path = self.get_cache_path(repo, activity_type)

        if self.is_cache_valid(cache_path):
            cached_data = self.load_cache(cache_path)
            if cached_data:
                logger.debug("Using cached %s data", activity_type)
                return cached_data

        if paginated:
            data = self.github_request_paginated(url, params)
            fetch_failed = not data
        else:
            data = self.github_request(url, params)
            fetch_failed = data is None

        if fetch_failed:
            stale_cache = self.load_cache(cache_path)
            if stale_cache:
                logger.debug("Using stale cache for %s", activity_type)
                return stale_cache
            return []

        if filter_fn:
            data = filter_fn(data)

        self.save_cache(cache_path, data)
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

        # Filter PRs and issues (same fields: user.login, title, labels)
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

        # Releases are not filtered (they are rarely noise)

        if removed:
            logger.info("  Filtered %d items", removed)

        return activity

    # -- main fetch_activity implementation --

    def fetch_activity(self, since: datetime) -> List[Dict[str, Any]]:
        """Fetch activity for all configured repositories."""
        results = []
        for repo in self.repositories:
            logger.info("Fetching %s ...", repo)

            activity: Dict[str, Any] = {
                "repository": repo,
                "source_type": self.source_type,
                "since": since.isoformat()
            }

            if "commits" in self.activity_types:
                activity["commits"] = self.fetch_commits(repo, since)

            if "pulls" in self.activity_types:
                activity["pulls"] = self.fetch_pulls(repo, since)

            if "issues" in self.activity_types:
                activity["issues"] = self.fetch_issues(repo, since)

            if "releases" in self.activity_types:
                activity["releases"] = self.fetch_releases(repo, since)

            activity = self.apply_filters(activity)

            # Single summary line for this repo
            parts = []
            for key, label in [("commits", "commits"), ("pulls", "PRs"),
                               ("issues", "issues"), ("releases", "releases")]:
                if key in activity:
                    parts.append(f"{len(activity[key])} {label}")
            if parts:
                logger.info("  %s", ", ".join(parts))

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
            logger.info("Calling Claude CLI (%d chars) ...", len(prompt))

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

            logger.info("Summary ready (%d chars)", len(output))
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
                    logger.info("Config: %s", config_path)
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

    @staticmethod
    def _parse_env_int(var_name: str, config_key: str, target: Dict[str, Any]):
        """Read an integer from env var into target dict, warning on bad values."""
        value = os.environ.get(var_name)
        if value:
            try:
                target[config_key] = int(value)
                logger.info("%s=%s overrides %s", var_name, value, config_key)
            except ValueError:
                logger.warning("%s=%s is not a valid integer, ignoring",
                               var_name, value)

    def _apply_env_overrides(self):
        """Apply HERALD_* environment variable overrides to config."""
        defaults = self.config.setdefault("defaults", {})
        self._parse_env_int("HERALD_DAYS", "time_window_days", defaults)
        self._parse_env_int("HERALD_MAX_COMMITS", "max_commits", defaults)

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

    def _load_group_secrets(self, group: Dict[str, Any]):
        """Load secrets from secrets/<group-name>.json and merge into the group dict.

        Looks for a secrets file matching the group name. Only recognized secret
        fields (SECRET_FIELD_NAMES) are merged. Existing values in the group dict
        are not overwritten — this allows inline config to still work, though
        secrets/ is the preferred location.
        """
        group_name = group.get("name")
        if not group_name:
            return

        secrets_path = self.config_dir / "secrets" / f"{group_name}.json"
        if not secrets_path.exists():
            return

        try:
            with open(secrets_path, 'r') as f:
                secrets = json.load(f)
        except Exception as e:
            logger.warning("Failed to load secrets from %s: %s", secrets_path, e)
            return

        merged = 0
        for field in SECRET_FIELD_NAMES:
            if field in secrets and secrets[field]:
                group[field] = secrets[field]
                merged += 1

        if merged:
            logger.debug("Loaded %d secret(s) for group '%s' from %s",
                         merged, group_name, secrets_path)

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
            for g in resolved:
                self._load_group_secrets(g)
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
            for g in groups:
                self._load_group_secrets(g)
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

        # Check secrets directory
        secrets_dir = self.config_dir / "secrets"
        if secrets_dir.is_dir():
            secret_files = list(secrets_dir.glob("*.json"))
            non_example = [f for f in secret_files if f.name != "example.json"]
            if non_example:
                names = ", ".join(f.stem for f in non_example)
                print(f"OK: secrets/ directory found with files for: {names}")
            else:
                print("OK: secrets/ directory exists (no group secret files yet)")
        else:
            print("WARN: secrets/ directory not found. Create it to store "
                  "webhook URLs separately from group configs.")
            issues += 1

        # Warn about webhook URLs embedded in group configs
        for group in self.groups:
            name = group.get("name", "unnamed")
            # Check if the group's external config file contains a webhook URL
            ext_conf = next(
                (g for g in self.config.get("groups", [])
                 if g.get("name") == name and "config_file" in g),
                None
            )
            if ext_conf:
                ext_path = self.config_dir / ext_conf["config_file"]
                if ext_path.exists():
                    try:
                        with open(ext_path, 'r') as f:
                            ext_data = json.load(f)
                        if ext_data.get("teams_webhook_url"):
                            print(f"WARN: Group '{name}' has teams_webhook_url in "
                                  f"{ext_path.name}. Move it to secrets/{name}.json "
                                  f"instead.")
                            issues += 1
                    except Exception:
                        pass

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
            webhook_str = ", webhook: configured" if group.get("teams_webhook_url") else ""
            print(f"  - {name}{team_str}{webhook_str}")
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

        logger.info("Generating AI summary for %s ...", group_name)

        prompt = self.format_prompt(activity_list, team_context)
        summary = self.ai_backend.summarize(prompt, group_name)

        if summary:
            summary = self.strip_conversational_output(summary)
            self.save_cache_text(summary_cache, summary)
            return summary
        else:
            return self.format_raw_data(activity_list)

    def is_cache_valid(self, cache_path: Path) -> bool:
        return _is_cache_valid(cache_path, self.cache_ttl, self.force_refresh)

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

        logger.info("Report saved: %s", output_file)

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

        logger.info("Posting to Teams ...")

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
                logger.info("Posted to Teams successfully")
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
# Validation helpers
# ---------------------------------------------------------------------------

def _capture_validation_output(config_path_str: Optional[str]) -> str:
    """Run Herald.validate() and capture its print output."""
    herald_logger = logging.getLogger("herald")
    old_level = herald_logger.level
    herald_logger.setLevel(logging.ERROR)
    try:
        h = Herald(config_path=config_path_str)
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            h.validate()
        finally:
            sys.stdout = old_stdout
        return buf.getvalue()
    finally:
        herald_logger.setLevel(old_level)


def _colorize_validation_line(line: str, use_markers: bool = False) -> str:
    """Colorize a single validation output line with Rich markup."""
    if line.startswith("OK:"):
        if use_markers:
            return f"[green] [OK] {line[3:]}[/green]"
        return f"[green]{line}[/green]"
    if line.startswith("WARN:"):
        if use_markers:
            return f"[yellow] [!!] {line[5:]}[/yellow]"
        return f"[yellow]{line}[/yellow]"
    if line.startswith("FAIL:"):
        if use_markers:
            return f"[red] [XX] {line[5:]}[/red]"
        return f"[red]{line}[/red]"
    if line.startswith("All checks passed"):
        return f"\n[bold green]{line}[/bold green]"
    if line.startswith("Passed with"):
        return f"\n[bold yellow]{line}[/bold yellow]"
    if line.startswith("Validation failed"):
        return f"\n[bold red]{line}[/bold red]"
    return line


# ---------------------------------------------------------------------------
# ConfigStore — shared file I/O for ConfigManager and ConfigApp
# ---------------------------------------------------------------------------

class ConfigStore:
    """Shared config file I/O, secret management, and group resolution."""

    DEFAULT_CONFIG = {
        "defaults": {
            "time_window_days": 14,
            "max_commits": 20,
            "activity_types": ["commits", "pulls", "issues", "releases"],
            "ai_backend": {
                "type": "claude-cli",
                "timeout": 600
            }
        },
        "groups": []
    }

    def _init_config(self, config_path: Optional[str] = None):
        """Initialize config state. Call from subclass __init__."""
        self.config_path = self._resolve_config_path(config_path)
        self.config_dir = self.config_path.parent if self.config_path else Path.cwd()
        self.config = self._load_config()

    def _resolve_config_path(self, config_path: Optional[str]) -> Optional[Path]:
        if config_path:
            return Path(config_path).resolve()
        for path in ["./herald.config.json",
                     str(Path.home() / ".herald.config.json")]:
            if Path(path).exists():
                return Path(path).resolve()
        return None

    def _load_config(self) -> Dict[str, Any]:
        if self.config_path and self.config_path.exists():
            return self._load_json_file(self.config_path)
        return dict(self.DEFAULT_CONFIG)

    def _load_json_file(self, path: Path) -> Dict[str, Any]:
        with open(path, 'r') as f:
            return json.load(f)

    def _save_json_file(self, path: Path, data: Dict[str, Any]):
        serialized = json.dumps(data, indent=2) + "\n"
        json.loads(serialized)  # round-trip validate
        if path.exists():
            self._backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, 'w') as f:
            f.write(serialized)
        os.replace(str(tmp_path), str(path))

    def _backup_file(self, path: Path):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = path.with_suffix(f".backup.{timestamp}.json")
        shutil.copy2(str(path), str(backup_path))

    def _mask_secret(self, value: str) -> str:
        if not value or len(value) <= 20:
            return "****"
        return value[:12] + "****" + value[-4:]

    def _load_group_config(self, group_entry: Dict[str, Any]) -> Dict[str, Any]:
        if "config_file" in group_entry:
            ext_path = self.config_dir / group_entry["config_file"]
            if ext_path.exists():
                ext = self._load_json_file(ext_path)
                ext["name"] = group_entry.get("name", ext_path.stem)
                return ext
        return dict(group_entry)

    def _load_secrets(self, group_name: str) -> Dict[str, Any]:
        secrets_path = self.config_dir / "secrets" / f"{group_name}.json"
        if secrets_path.exists():
            return self._load_json_file(secrets_path)
        return {}

    def _save_secrets(self, group_name: str, secrets: Dict[str, Any]):
        secrets_path = self.config_dir / "secrets" / f"{group_name}.json"
        self._save_json_file(secrets_path, secrets)

    def _save_main_config(self):
        if not self.config_path:
            self.config_path = Path.cwd() / "herald.config.json"
            self.config_dir = self.config_path.parent
        self._save_json_file(self.config_path, self.config)

    def _save_group_config(self, group_entry: Dict[str, Any],
                            full_config: Dict[str, Any], is_external: bool):
        if is_external:
            ext_path = self.config_dir / group_entry["config_file"]
            save_data = {k: v for k, v in full_config.items() if k != "name"}
            self._save_json_file(ext_path, save_data)
        else:
            groups = self.config.get("groups", [])
            for i, g in enumerate(groups):
                if g.get("name") == group_entry.get("name"):
                    groups[i] = full_config
                    break
            self._save_main_config()


# ---------------------------------------------------------------------------
# ConfigManager — interactive configuration TUI
# ---------------------------------------------------------------------------

class ConfigManager(ConfigStore):
    """Interactive configuration manager using rich TUI."""

    def __init__(self, config_path: Optional[str] = None):
        self.console = Console()
        self._init_config(config_path)

    def _backup_file(self, path: Path):
        super()._backup_file(path)
        self.console.print(f"  Backup saved: {path.name}", style="dim")

    def _save_main_config(self):
        super()._save_main_config()
        self.console.print(f"[green]Saved {self.config_path.name}[/green]")

    def _save_secrets(self, group_name: str, secrets: Dict[str, Any]):
        super()._save_secrets(group_name, secrets)
        self.console.print(f"[green]Saved secrets/{group_name}.json[/green]")

    def _save_group_config(self, group_entry: Dict[str, Any],
                            full_config: Dict[str, Any], is_external: bool):
        super()._save_group_config(group_entry, full_config, is_external)
        if is_external:
            self.console.print(f"[green]Saved {group_entry['config_file']}[/green]")

    def _numbered_menu(self, title: str, options: List[str],
                       allow_back: bool = True) -> Optional[int]:
        self.console.print()
        self.console.print(f"[bold]{title}[/bold]")
        for i, option in enumerate(options, 1):
            self.console.print(f"  {i}. {option}")
        if allow_back:
            self.console.print(f"  0. Back")

        choices = [str(i) for i in range(0 if allow_back else 1, len(options) + 1)]
        try:
            choice = IntPrompt.ask("Select", choices=choices, show_choices=False)
        except KeyboardInterrupt:
            return None
        if choice == 0 and allow_back:
            return None
        return choice - 1

    # -- Main menu --

    def run(self):
        self.console.print(Panel(
            "[bold]Herald Configuration Manager[/bold]\n"
            f"Config: {self.config_path or '(none - will create new)'}",
            border_style="blue"
        ))

        while True:
            choice = self._numbered_menu("Main Menu", [
                "Edit Defaults",
                "Manage Groups",
                "Manage Secrets",
                "Validate Configuration",
                "Exit",
            ], allow_back=False)

            if choice is None or choice == 4:
                self.console.print("[dim]Goodbye.[/dim]")
                break
            try:
                if choice == 0:
                    self._edit_defaults()
                elif choice == 1:
                    self._manage_groups()
                elif choice == 2:
                    self._manage_secrets()
                elif choice == 3:
                    self._validate_config()
            except KeyboardInterrupt:
                self.console.print()
                continue

    # -- 1. Edit Defaults --

    def _edit_defaults(self):
        while True:
            defaults = self.config.setdefault("defaults", {})
            ai = defaults.get("ai_backend", {})

            table = Table(title="Current Defaults")
            table.add_column("Setting", style="cyan")
            table.add_column("Value", style="green")
            table.add_row("time_window_days", str(defaults.get("time_window_days", 14)))
            table.add_row("max_commits", str(defaults.get("max_commits", 20)))
            table.add_row("activity_types",
                          ", ".join(defaults.get("activity_types",
                                                  ["commits", "pulls", "issues", "releases"])))
            table.add_row("ai_backend.type", ai.get("type", "claude-cli"))
            table.add_row("ai_backend.timeout", str(ai.get("timeout", 600)))
            self.console.print(table)

            choice = self._numbered_menu("Edit which setting?", [
                "time_window_days",
                "max_commits",
                "activity_types",
                "ai_backend.type",
                "ai_backend.timeout",
            ])
            if choice is None:
                return

            try:
                if choice == 0:
                    val = IntPrompt.ask("time_window_days",
                                        default=defaults.get("time_window_days", 14))
                    defaults["time_window_days"] = val
                elif choice == 1:
                    val = IntPrompt.ask("max_commits",
                                        default=defaults.get("max_commits", 20))
                    defaults["max_commits"] = val
                elif choice == 2:
                    all_types = ["commits", "pulls", "issues", "releases"]
                    current = defaults.get("activity_types", all_types)
                    self.console.print(f"  Current: {', '.join(current)}")
                    self.console.print(f"  Available: {', '.join(all_types)}")
                    raw = Prompt.ask("Enter types (comma-separated)",
                                    default=",".join(current))
                    parsed = [t.strip() for t in raw.split(",") if t.strip()]
                    invalid = [t for t in parsed if t not in all_types]
                    if invalid:
                        self.console.print(f"[red]Invalid types: {', '.join(invalid)}[/red]")
                        continue
                    defaults["activity_types"] = parsed
                elif choice == 3:
                    available = list(AI_BACKEND_REGISTRY.keys())
                    val = Prompt.ask("ai_backend type",
                                    default=ai.get("type", "claude-cli"),
                                    choices=available)
                    defaults.setdefault("ai_backend", {})["type"] = val
                elif choice == 4:
                    val = IntPrompt.ask("ai_backend timeout (seconds)",
                                        default=ai.get("timeout", 600))
                    defaults.setdefault("ai_backend", {})["timeout"] = val
            except KeyboardInterrupt:
                self.console.print()
                continue

            if Confirm.ask("Save changes?"):
                self._save_main_config()

    # -- 2. Manage Groups --

    def _manage_groups(self):
        while True:
            groups = self.config.get("groups", [])

            table = Table(title="Configured Groups")
            table.add_column("#", style="dim")
            table.add_column("Name", style="cyan")
            table.add_column("Source", style="green")
            table.add_column("Repos", style="yellow")
            table.add_column("Config File", style="dim")

            for i, g in enumerate(groups, 1):
                full = self._load_group_config(g)
                sources = full.get("sources", [])
                src_type = sources[0].get("type", "?") if sources else "-"
                repos = []
                for s in sources:
                    repos.extend(s.get("repositories", []))
                config_file = g.get("config_file", "(inline)")
                table.add_row(str(i), g.get("name", "unnamed"), src_type,
                              str(len(repos)), config_file)

            self.console.print(table)

            opts = [f"View/Edit {g.get('name', 'unnamed')}" for g in groups]
            opts.append("Create new group")
            if groups:
                opts.append("Delete group")

            choice = self._numbered_menu("Groups", opts)
            if choice is None:
                return

            if choice < len(groups):
                self._edit_group(choice)
            elif choice == len(groups):
                self._create_group()
            elif choice == len(groups) + 1:
                self._delete_group()

    def _edit_group(self, index: int):
        groups = self.config.get("groups", [])
        group_entry = groups[index]
        group_name = group_entry.get("name", "unnamed")
        is_external = "config_file" in group_entry
        full_config = self._load_group_config(group_entry)

        while True:
            choice = self._numbered_menu(f"Edit Group: {group_name}", [
                "Edit sources (repositories, filters)",
                "Edit team context",
                "View full config (JSON)",
            ])
            if choice is None:
                return

            if choice == 0:
                self._edit_group_sources(group_entry, full_config, is_external)
                # Reload after edit
                full_config = self._load_group_config(group_entry)
            elif choice == 1:
                self._edit_group_team_context(group_entry, full_config, is_external)
                full_config = self._load_group_config(group_entry)
            elif choice == 2:
                display = dict(full_config)
                display.pop("name", None)  # name is on the stub
                syntax = Syntax(json.dumps(display, indent=2), "json",
                                theme="monokai", line_numbers=True)
                self.console.print(Panel(syntax,
                                         title=f"Group: {group_name}",
                                         border_style="blue"))

    def _edit_group_sources(self, group_entry: Dict[str, Any],
                             full_config: Dict[str, Any], is_external: bool):
        sources = full_config.setdefault("sources", [])
        if not sources:
            sources.append({"type": "github", "repositories": []})
            full_config["sources"] = sources

        while True:
            # Show current repos across all sources
            for si, src in enumerate(sources):
                repos = src.get("repositories", [])
                filters = src.get("filters", {})
                self.console.print(f"\n[bold]Source {si + 1}[/bold] (type: {src.get('type', 'github')})")
                if repos:
                    for r in repos:
                        self.console.print(f"  - {r}")
                else:
                    self.console.print("  (no repositories)")
                if filters:
                    if filters.get("exclude_authors"):
                        self.console.print(f"  Exclude authors: {', '.join(filters['exclude_authors'])}")
                    if filters.get("exclude_titles"):
                        self.console.print(f"  Exclude titles: {', '.join(filters['exclude_titles'])}")
                    if filters.get("exclude_labels"):
                        self.console.print(f"  Exclude labels: {', '.join(filters['exclude_labels'])}")

            choice = self._numbered_menu("Sources", [
                "Add repository",
                "Remove repository",
                "Edit filters",
            ])
            if choice is None:
                return

            try:
                if choice == 0:
                    repo = Prompt.ask("Repository (owner/repo)")
                    parts = repo.strip().split('/')
                    if len(parts) != 2 or not all(parts):
                        self.console.print("[red]Invalid format. Use owner/repo[/red]")
                        continue
                    # Add to first source
                    repos = sources[0].setdefault("repositories", [])
                    if repo in repos:
                        self.console.print("[yellow]Already present[/yellow]")
                        continue
                    repos.append(repo)
                    if Confirm.ask("Save changes?"):
                        self._save_group_config(group_entry, full_config, is_external)

                elif choice == 1:
                    all_repos = []
                    for src in sources:
                        all_repos.extend(src.get("repositories", []))
                    if not all_repos:
                        self.console.print("[yellow]No repositories to remove[/yellow]")
                        continue
                    idx = self._numbered_menu("Remove which repository?", all_repos)
                    if idx is None:
                        continue
                    repo_to_remove = all_repos[idx]
                    if Confirm.ask(f"Remove [bold]{repo_to_remove}[/bold]?"):
                        for src in sources:
                            repos = src.get("repositories", [])
                            if repo_to_remove in repos:
                                repos.remove(repo_to_remove)
                                break
                        if Confirm.ask("Save changes?"):
                            self._save_group_config(group_entry, full_config, is_external)

                elif choice == 2:
                    self._edit_filters(sources[0], group_entry, full_config, is_external)

            except KeyboardInterrupt:
                self.console.print()
                continue

    def _edit_filters(self, source: Dict[str, Any], group_entry: Dict[str, Any],
                       full_config: Dict[str, Any], is_external: bool):
        filters = source.setdefault("filters", {})
        while True:
            self.console.print()
            self.console.print("[bold]Current Filters[/bold]")
            self.console.print(f"  exclude_authors: {filters.get('exclude_authors', [])}")
            self.console.print(f"  exclude_titles:  {filters.get('exclude_titles', [])}")
            self.console.print(f"  exclude_labels:  {filters.get('exclude_labels', [])}")

            choice = self._numbered_menu("Edit filters", [
                "Set exclude_authors",
                "Set exclude_titles",
                "Set exclude_labels",
                "Clear all filters",
            ])
            if choice is None:
                return

            try:
                if choice == 0:
                    current = ", ".join(filters.get("exclude_authors", []))
                    raw = Prompt.ask("Exclude authors (comma-separated)", default=current)
                    filters["exclude_authors"] = [a.strip() for a in raw.split(",")
                                                   if a.strip()]
                elif choice == 1:
                    current = ", ".join(filters.get("exclude_titles", []))
                    raw = Prompt.ask("Exclude title patterns (comma-separated regex)",
                                    default=current)
                    patterns = [p.strip() for p in raw.split(",") if p.strip()]
                    # Validate regex patterns
                    valid = True
                    for p in patterns:
                        try:
                            re.compile(p)
                        except re.error as e:
                            self.console.print(f"[red]Invalid regex '{p}': {e}[/red]")
                            valid = False
                    if not valid:
                        continue
                    filters["exclude_titles"] = patterns
                elif choice == 2:
                    current = ", ".join(filters.get("exclude_labels", []))
                    raw = Prompt.ask("Exclude labels (comma-separated)", default=current)
                    filters["exclude_labels"] = [l.strip() for l in raw.split(",")
                                                  if l.strip()]
                elif choice == 3:
                    if Confirm.ask("Clear all filters?"):
                        filters.clear()

                if Confirm.ask("Save changes?"):
                    # Clean up empty filters dict
                    if not any(filters.values()):
                        source.pop("filters", None)
                    self._save_group_config(group_entry, full_config, is_external)

            except KeyboardInterrupt:
                self.console.print()
                continue

    def _edit_group_team_context(self, group_entry: Dict[str, Any],
                                  full_config: Dict[str, Any], is_external: bool):
        tc = full_config.setdefault("team_context", {})
        while True:
            self.console.print()
            self.console.print("[bold]Team Context[/bold]")
            self.console.print(f"  name:        {tc.get('name', '(not set)')}")
            self.console.print(f"  focus_areas: {tc.get('focus_areas', [])}")
            self.console.print(f"  priorities:  {tc.get('priorities', [])}")

            choice = self._numbered_menu("Edit team context", [
                "Set team name",
                "Set focus areas",
                "Set priorities",
            ])
            if choice is None:
                return

            try:
                if choice == 0:
                    val = Prompt.ask("Team name", default=tc.get("name", ""))
                    tc["name"] = val
                elif choice == 1:
                    current = ", ".join(tc.get("focus_areas", []))
                    raw = Prompt.ask("Focus areas (comma-separated)", default=current)
                    tc["focus_areas"] = [a.strip() for a in raw.split(",") if a.strip()]
                elif choice == 2:
                    current = ", ".join(tc.get("priorities", []))
                    raw = Prompt.ask("Priorities (comma-separated, first = highest)",
                                    default=current)
                    tc["priorities"] = [p.strip() for p in raw.split(",") if p.strip()]

                if Confirm.ask("Save changes?"):
                    self._save_group_config(group_entry, full_config, is_external)

            except KeyboardInterrupt:
                self.console.print()
                continue

    def _create_group(self):
        try:
            name = Prompt.ask("Group name").strip()
            if not name:
                self.console.print("[red]Name cannot be empty[/red]")
                return

            # Check for duplicate
            existing = [g.get("name") for g in self.config.get("groups", [])]
            if name in existing:
                self.console.print(f"[red]Group '{name}' already exists[/red]")
                return

            use_external = Confirm.ask(
                f"Create external config file (groups/{name}.json)?", default=True)

            if use_external:
                # Create the external file
                ext_config = {
                    "sources": [
                        {
                            "type": "github",
                            "repositories": []
                        }
                    ],
                    "team_context": {
                        "name": "",
                        "focus_areas": [],
                        "priorities": []
                    }
                }
                ext_path = self.config_dir / "groups" / f"{name}.json"
                self._save_json_file(ext_path, ext_config)
                self.console.print(f"[green]Created groups/{name}.json[/green]")

                # Add stub to main config
                stub = {
                    "name": name,
                    "config_file": f"groups/{name}.json"
                }
                self.config.setdefault("groups", []).append(stub)
            else:
                # Inline group
                inline = {
                    "name": name,
                    "sources": [
                        {
                            "type": "github",
                            "repositories": []
                        }
                    ],
                    "team_context": {
                        "name": "",
                        "focus_areas": [],
                        "priorities": []
                    }
                }
                self.config.setdefault("groups", []).append(inline)

            self._save_main_config()
            self.console.print(f"[green]Group '{name}' created[/green]")

        except KeyboardInterrupt:
            self.console.print()

    def _delete_group(self):
        groups = self.config.get("groups", [])
        if not groups:
            self.console.print("[yellow]No groups to delete[/yellow]")
            return

        names = [g.get("name", "unnamed") for g in groups]
        idx = self._numbered_menu("Delete which group?", names)
        if idx is None:
            return

        group = groups[idx]
        name = group.get("name", "unnamed")

        if not Confirm.ask(f"[red]Delete group '{name}'?[/red]"):
            return

        # Remove from config
        groups.pop(idx)

        # Optionally delete external file
        if "config_file" in group:
            ext_path = self.config_dir / group["config_file"]
            if ext_path.exists():
                if Confirm.ask(f"Also delete {group['config_file']}?"):
                    self._backup_file(ext_path)
                    ext_path.unlink()
                    self.console.print(f"[green]Deleted {group['config_file']}[/green]")

        # Warn about secrets
        secrets_path = self.config_dir / "secrets" / f"{name}.json"
        if secrets_path.exists():
            self.console.print(
                f"[yellow]Note: secrets/{name}.json still exists. "
                f"Remove manually if no longer needed.[/yellow]"
            )

        self._save_main_config()
        self.console.print(f"[green]Group '{name}' removed[/green]")

    # -- 3. Manage Secrets --

    def _manage_secrets(self):
        while True:
            groups = self.config.get("groups", [])

            table = Table(title="Group Secrets")
            table.add_column("Group", style="cyan")
            table.add_column("Webhook URL", style="green")
            table.add_column("Source", style="dim")

            for g in groups:
                name = g.get("name", "unnamed")
                secrets = self._load_secrets(name)
                webhook = secrets.get("teams_webhook_url", "")
                if webhook:
                    table.add_row(name, self._mask_secret(webhook),
                                  f"secrets/{name}.json")
                else:
                    table.add_row(name, "(not set)", "-")

            self.console.print(table)

            if not groups:
                self.console.print("[yellow]No groups configured. "
                                   "Create a group first.[/yellow]")
                return

            names = [g.get("name", "unnamed") for g in groups]
            choice = self._numbered_menu("Set/update webhook for which group?", names)
            if choice is None:
                return

            group_name = names[choice]
            secrets = self._load_secrets(group_name)
            current = secrets.get("teams_webhook_url", "")

            try:
                if current:
                    self.console.print(f"  Current: {self._mask_secret(current)}")
                    action = self._numbered_menu("Action", [
                        "Update webhook URL",
                        "Remove webhook URL",
                    ])
                    if action is None:
                        continue
                    if action == 1:
                        if Confirm.ask("Remove webhook URL?"):
                            secrets.pop("teams_webhook_url", None)
                            if secrets:
                                self._save_secrets(group_name, secrets)
                            else:
                                # Remove empty secrets file
                                secrets_path = self.config_dir / "secrets" / f"{group_name}.json"
                                if secrets_path.exists():
                                    self._backup_file(secrets_path)
                                    secrets_path.unlink()
                                    self.console.print(
                                        f"[green]Removed secrets/{group_name}.json[/green]")
                        continue
                    # Fall through to set URL

                url = Prompt.ask("Webhook URL").strip()
                if not url:
                    self.console.print("[yellow]Skipped (empty URL)[/yellow]")
                    continue

                secrets["teams_webhook_url"] = url
                self._save_secrets(group_name, secrets)

            except KeyboardInterrupt:
                self.console.print()
                continue

    # -- 4. Validate Configuration --

    def _validate_config(self):
        self.console.print()
        self.console.print(Panel("[bold]Validating Configuration[/bold]",
                                  border_style="blue"))
        try:
            config_path_str = str(self.config_path) if self.config_path else None
            output = _capture_validation_output(config_path_str)
            for line in output.strip().split('\n'):
                self.console.print(_colorize_validation_line(line))
        except Exception as e:
            self.console.print(f"[red]Validation error: {e}[/red]")


# ---------------------------------------------------------------------------
# ConfigApp — Textual TUI for configuration management
# ---------------------------------------------------------------------------

if HAS_TEXTUAL:

    class VimDataTable(DataTable):
        """DataTable with vim hjkl keys mapped to arrow equivalents."""

        BINDINGS = [
            Binding("j", "cursor_down", "Down", show=False),
            Binding("k", "cursor_up", "Up", show=False),
            Binding("h", "cursor_left", "Left", show=False),
            Binding("l", "cursor_right", "Right", show=False),
        ]

    class VimListView(ListView):
        """ListView with vim jk keys mapped to arrow equivalents."""

        BINDINGS = [
            Binding("j", "cursor_down", "Down", show=False),
            Binding("k", "cursor_up", "Up", show=False),
        ]

    class ConfirmModal(ModalScreen[bool]):
        """Modal dialog for yes/no confirmation.  y to confirm, n/Escape to cancel."""

        ESCAPE_TO_MINIMIZE = False

        BINDINGS = [
            Binding("y", "confirm", "Yes", show=False),
            Binding("n", "cancel", "No", show=False),
            Binding("escape", "cancel", "Cancel", show=False),
            Binding("ctrl+c", "cancel", "Cancel", priority=True, show=False),
        ]

        DEFAULT_CSS = """
        ConfirmModal {
            align: center middle;
        }
        ConfirmModal > Container {
            width: 60;
            height: auto;
            max-height: 14;
            border: heavy $error;
            background: $surface;
            padding: 1 2;
            border-title-color: $error;
            border-title-style: bold;
        }
        ConfirmModal > Container > #confirm-icon {
            width: 100%;
            content-align: center middle;
            color: $warning;
            margin-bottom: 1;
        }
        ConfirmModal > Container > #confirm-msg {
            width: 100%;
            content-align: center middle;
            margin-bottom: 1;
        }
        ConfirmModal > Container > #confirm-hint {
            width: 100%;
            content-align: center middle;
            color: $text-muted;
        }
        """

        def __init__(self, message: str) -> None:
            super().__init__()
            self._message = message

        def compose(self) -> ComposeResult:
            with Container(id="confirm-dialog"):
                yield Label("! WARNING !", id="confirm-icon")
                yield Label(self._message, id="confirm-msg")
                yield Label("y = confirm  /  n or Ctrl-C = cancel", id="confirm-hint")

        def on_mount(self) -> None:
            self.query_one("#confirm-dialog", Container).border_title = "Confirm"

        def action_confirm(self) -> None:
            self.dismiss(True)

        def action_cancel(self) -> None:
            self.dismiss(False)

    class InputModal(ModalScreen[str]):
        """Generic single-field text input modal.  Enter to submit, Escape/Ctrl-C to cancel."""

        ESCAPE_TO_MINIMIZE = False

        BINDINGS = [
            Binding("ctrl+c", "cancel", "Cancel", priority=True, show=False),
        ]

        DEFAULT_CSS = """
        InputModal {
            align: center middle;
        }
        InputModal > Container {
            width: 70;
            height: auto;
            max-height: 20;
            border: heavy $accent;
            background: $surface;
            padding: 1 2;
            border-title-color: $accent;
            border-title-style: bold;
        }
        InputModal Input {
            margin: 1 0;
        }
        InputModal > Container > #input-hint {
            width: 100%;
            content-align: center middle;
            color: $text-muted;
        }
        """

        def __init__(self, title: str, label: str, default: str = "",
                     placeholder: str = "") -> None:
            super().__init__()
            self._title = title
            self._label = label
            self._default = default
            self._placeholder = placeholder

        def compose(self) -> ComposeResult:
            with Container(id="input-dialog"):
                yield Label(self._label, id="input-label")
                yield Input(
                    value=self._default,
                    placeholder=self._placeholder,
                    id="input-field",
                )
                yield Label("Enter = save  /  Esc or Ctrl-C = cancel", id="input-hint")

        def on_mount(self) -> None:
            self.query_one("#input-dialog", Container).border_title = self._title
            self.query_one("#input-field").focus()

        def _key_escape(self) -> None:
            self.dismiss("")

        def action_cancel(self) -> None:
            self.dismiss("")

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.dismiss(event.value)

    class SelectModal(ModalScreen[str]):
        """Generic option picker modal."""

        DEFAULT_CSS = """
        SelectModal {
            align: center middle;
        }
        SelectModal > Container {
            width: 70;
            height: auto;
            max-height: 20;
            border: heavy $accent;
            background: $surface;
            padding: 1 2;
            border-title-color: $accent;
            border-title-style: bold;
        }
        SelectModal > Container > #select-list {
            height: auto;
            max-height: 12;
            margin: 1 0;
        }
        """

        BINDINGS = [
            Binding("b", "cancel", "Cancel"),
            Binding("escape", "cancel", "Cancel", show=False),
            Binding("ctrl+c", "cancel", "Cancel", priority=True, show=False),
        ]

        def __init__(self, title: str,
                     options: list) -> None:
            super().__init__()
            self._title = title
            self._options = options  # list of (value, label)

        def compose(self) -> ComposeResult:
            with Container(id="select-dialog"):
                yield VimListView(
                    *[ListItem(Label(label), id=f"opt-{value}")
                      for value, label in self._options],
                    id="select-list",
                )

        def on_mount(self) -> None:
            dialog = self.query_one("#select-dialog", Container)
            dialog.border_title = self._title

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            item_id = event.item.id
            if item_id and item_id.startswith("opt-"):
                self.dismiss(item_id[4:])

        def action_cancel(self) -> None:
            self.dismiss("")

    class MainMenuScreen(Screen):
        """Main menu with navigation options."""

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("b", "quit", "Quit"),
        ]

        def compose(self) -> ComposeResult:
            yield Header()
            config_label = str(self.app.config_path) if self.app.config_path else "(none - will create new)"
            yield Static(
                "[bold bright_cyan]"
                "  _  _ ___ ___    _   _    ___  \n"
                " | || | __| _ \\  /_\\ | |  |   \\ \n"
                " | __ | _||   / / _ \\| |__| |) |\n"
                " |_||_|___|_|_\\/_/ \\_\\____|___/ \n"
                "[/bold bright_cyan]\n"
                f"  [dim]Config:[/dim] [italic]{config_label}[/italic]",
                id="banner",
            )
            yield VimListView(
                ListItem(Label("[bold]Edit Defaults[/bold]        [dim]Time window, commit limits, AI backend[/dim]"), id="defaults"),
                ListItem(Label("[bold]Manage Groups[/bold]        [dim]Add, edit, or remove repo groups[/dim]"), id="groups"),
                ListItem(Label("[bold]Manage Secrets[/bold]       [dim]Teams webhook URLs per group[/dim]"), id="secrets"),
                ListItem(Label("[bold]Validate Config[/bold]      [dim]Check config + prerequisites[/dim]"), id="validate"),
                ListItem(Label("[dim]Exit[/dim]"), id="exit"),
                id="main-menu",
            )
            yield Static("[dim]Enter[/dim] select  [dim]hjkl[/dim] navigate  [dim]b[/dim] exit", id="help")
            yield Footer()

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            item_id = event.item.id
            if item_id == "defaults":
                self.app.push_screen(DefaultsScreen())
            elif item_id == "groups":
                self.app.push_screen(GroupsScreen())
            elif item_id == "secrets":
                self.app.push_screen(SecretsScreen())
            elif item_id == "validate":
                self.app.push_screen(ValidateScreen())
            elif item_id == "exit":
                self.app.exit()

        def action_quit(self) -> None:
            self.app.exit()

    class DefaultsScreen(Screen):
        """View and edit default settings."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
            Binding("e", "edit_setting_key", "Edit"),
        ]

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("[bold bright_cyan]>> Defaults[/bold bright_cyan]", id="screen-title")
            table = VimDataTable(id="defaults-table", zebra_stripes=True)
            table.add_columns("Setting", "Value")
            yield table
            yield Static("[dim]e[/dim] edit  [dim]hjkl[/dim] navigate  [dim]b[/dim] back", id="help")
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_table()

        def _refresh_table(self) -> None:
            table = self.query_one("#defaults-table", DataTable)
            table.clear()
            defaults = self.app.config.get("defaults", {})
            ai = defaults.get("ai_backend", {})
            table.add_row("time_window_days", str(defaults.get("time_window_days", 14)))
            table.add_row("max_commits", str(defaults.get("max_commits", 20)))
            table.add_row("activity_types",
                          ", ".join(defaults.get("activity_types",
                                                 ["commits", "pulls", "issues", "releases"])))
            table.add_row("ai_backend.type", ai.get("type", "claude-cli"))
            table.add_row("ai_backend.timeout", str(ai.get("timeout", 600)))

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            self._edit_by_row(event.cursor_row)

        def action_edit_setting_key(self) -> None:
            table = self.query_one("#defaults-table", DataTable)
            self._edit_by_row(table.cursor_row)

        def _edit_by_row(self, row_index: int) -> None:
            settings = [
                "time_window_days", "max_commits", "activity_types",
                "ai_backend.type", "ai_backend.timeout"
            ]
            if row_index < 0 or row_index >= len(settings):
                return
            setting = settings[row_index]
            self._edit_setting(setting)

        def _edit_setting(self, setting: str) -> None:
            defaults = self.app.config.setdefault("defaults", {})
            ai = defaults.setdefault("ai_backend", {})

            if setting in ("time_window_days", "max_commits", "ai_backend.timeout"):
                labels = {
                    "time_window_days": "Time window (days)",
                    "max_commits": "Max commits per repo",
                    "ai_backend.timeout": "AI backend timeout (seconds)",
                }
                if setting == "ai_backend.timeout":
                    current = str(ai.get("timeout", 600))
                else:
                    current = str(defaults.get(setting, 14 if setting == "time_window_days" else 20))

                def on_int_input(value: str) -> None:
                    if not value:
                        return
                    try:
                        int_val = int(value)
                    except ValueError:
                        self.notify("Must be an integer", severity="error")
                        return
                    if setting == "ai_backend.timeout":
                        ai["timeout"] = int_val
                    else:
                        defaults[setting] = int_val
                    self.app._save_main_config()
                    self.notify(f"Saved {setting} = {int_val}")
                    self._refresh_table()

                self.app.push_screen(
                    InputModal(setting, labels[setting], default=current),
                    on_int_input,
                )

            elif setting == "activity_types":
                all_types = ["commits", "pulls", "issues", "releases"]
                current = defaults.get("activity_types", all_types)

                def on_types_input(value: str) -> None:
                    if not value:
                        return
                    parsed = [t.strip() for t in value.split(",") if t.strip()]
                    invalid = [t for t in parsed if t not in all_types]
                    if invalid:
                        self.notify(
                            f"Invalid types: {', '.join(invalid)}",
                            severity="error",
                        )
                        return
                    defaults["activity_types"] = parsed
                    self.app._save_main_config()
                    self.notify(f"Saved activity_types")
                    self._refresh_table()

                self.app.push_screen(
                    InputModal("Activity Types",
                               "Comma-separated (commits,pulls,issues,releases)",
                               default=",".join(current)),
                    on_types_input,
                )

            elif setting == "ai_backend.type":
                available = list(AI_BACKEND_REGISTRY.keys())
                options = [(k, k) for k in available]

                def on_type_select(value: str) -> None:
                    if not value:
                        return
                    ai["type"] = value
                    self.app._save_main_config()
                    self.notify(f"Saved ai_backend.type = {value}")
                    self._refresh_table()

                self.app.push_screen(
                    SelectModal("AI Backend", options), on_type_select
                )

        def action_go_back(self) -> None:
            self.app.pop_screen()

    class GroupsScreen(Screen):
        """List and manage groups."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
            Binding("c", "create_group", "Create"),
            Binding("e", "edit_group", "Edit"),
            Binding("r", "rename_group", "Rename"),
            Binding("d", "delete_group", "Delete"),
        ]

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("[bold bright_cyan]>> Groups[/bold bright_cyan]", id="screen-title")
            table = VimDataTable(id="groups-table", zebra_stripes=True)
            table.add_columns("#", "Name", "Source", "Repos", "Config File")
            yield table
            yield Static(
                "[dim]e[/dim] edit  "
                "[dim]r[/dim] rename  "
                "[dim]c[/dim] create  "
                "[dim]d[/dim] delete  "
                "[dim]hjkl[/dim] navigate  "
                "[dim]b[/dim] back",
                id="help",
            )
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_table()

        def _refresh_table(self) -> None:
            table = self.query_one("#groups-table", DataTable)
            table.clear()
            groups = self.app.config.get("groups", [])
            for i, g in enumerate(groups, 1):
                full = self.app._load_group_config(g)
                sources = full.get("sources", [])
                src_type = sources[0].get("type", "?") if sources else "-"
                repos = []
                for s in sources:
                    repos.extend(s.get("repositories", []))
                config_file = g.get("config_file", "(inline)")
                table.add_row(str(i), g.get("name", "unnamed"), src_type,
                              str(len(repos)), config_file)

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            self._open_group(event.cursor_row)

        def action_edit_group(self) -> None:
            table = self.query_one("#groups-table", DataTable)
            self._open_group(table.cursor_row)

        def _open_group(self, row_index: int) -> None:
            groups = self.app.config.get("groups", [])
            if 0 <= row_index < len(groups):
                self.app.push_screen(GroupDetailScreen(row_index))

        def action_create_group(self) -> None:
            def on_name_input(name: str) -> None:
                name = name.strip()
                if not name:
                    return

                existing = [g.get("name") for g in self.app.config.get("groups", [])]
                if name in existing:
                    self.notify(f"Group '{name}' already exists",
                                severity="error")
                    return

                def on_config_type(choice: str) -> None:
                    if not choice:
                        return  # Cancelled

                    if choice == "external":
                        ext_config = {
                            "sources": [{"type": "github", "repositories": []}],
                            "team_context": {
                                "name": "",
                                "focus_areas": [],
                                "priorities": []
                            }
                        }
                        ext_path = self.app.config_dir / "groups" / f"{name}.json"
                        self.app._save_json_file(ext_path, ext_config)

                        stub = {"name": name, "config_file": f"groups/{name}.json"}
                        self.app.config.setdefault("groups", []).append(stub)
                    else:
                        inline = {
                            "name": name,
                            "sources": [{"type": "github", "repositories": []}],
                            "team_context": {
                                "name": "",
                                "focus_areas": [],
                                "priorities": []
                            }
                        }
                        self.app.config.setdefault("groups", []).append(inline)

                    self.app._save_main_config()
                    self.notify(f"Group '{name}' created")
                    self._refresh_table()

                self.app.push_screen(
                    SelectModal("Config Storage", [
                        ("external", f"External file (groups/{name}.json)"),
                        ("inline", "Inline in herald.config.json"),
                    ]),
                    on_config_type,
                )

            self.app.push_screen(
                InputModal("Create Group", "Group name",
                           placeholder="my-team"),
                on_name_input,
            )

        def action_rename_group(self) -> None:
            groups = self.app.config.get("groups", [])
            if not groups:
                self.notify("No groups to rename", severity="warning")
                return

            table = self.query_one("#groups-table", DataTable)
            row_index = table.cursor_row
            if row_index < 0 or row_index >= len(groups):
                return

            group = groups[row_index]
            old_name = group.get("name", "unnamed")

            def on_name_input(new_name: str) -> None:
                new_name = new_name.strip()
                if not new_name or new_name == old_name:
                    return

                existing = [g.get("name") for g in self.app.config.get("groups", [])]
                if new_name in existing:
                    self.notify(f"Group '{new_name}' already exists",
                                severity="error")
                    return

                # Re-fetch in case config changed while modal was open
                current_groups = self.app.config.get("groups", [])
                if row_index >= len(current_groups):
                    return
                grp = current_groups[row_index]

                # Rename external config file if present
                if "config_file" in grp:
                    old_path = self.app.config_dir / grp["config_file"]
                    new_cfg_rel = f"groups/{new_name}.json"
                    new_path = self.app.config_dir / new_cfg_rel
                    if old_path.exists():
                        old_path.rename(new_path)
                    grp["config_file"] = new_cfg_rel

                # Rename secrets file if present
                old_secrets = self.app.config_dir / "secrets" / f"{old_name}.json"
                if old_secrets.exists():
                    new_secrets = self.app.config_dir / "secrets" / f"{new_name}.json"
                    old_secrets.rename(new_secrets)

                grp["name"] = new_name
                self.app._save_main_config()
                self.notify(f"Renamed '{old_name}' -> '{new_name}'")
                self._refresh_table()

            self.app.push_screen(
                InputModal("Rename Group", "New name", default=old_name),
                on_name_input,
            )

        def action_delete_group(self) -> None:
            groups = self.app.config.get("groups", [])
            if not groups:
                self.notify("No groups to delete", severity="warning")
                return

            table = self.query_one("#groups-table", DataTable)
            row_index = table.cursor_row
            if row_index < 0 or row_index >= len(groups):
                return

            group = groups[row_index]
            name = group.get("name", "unnamed")

            def on_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                current_groups = self.app.config.get("groups", [])
                if row_index >= len(current_groups):
                    return
                current_groups.pop(row_index)

                def finish_delete() -> None:
                    secrets_path = self.app.config_dir / "secrets" / f"{name}.json"
                    if secrets_path.exists():
                        self.notify(
                            f"secrets/{name}.json still exists. Remove manually if unneeded.",
                            severity="warning",
                        )
                    self.app._save_main_config()
                    self.notify(f"Group '{name}' removed")
                    self._refresh_table()

                if "config_file" in group:
                    ext_path = self.app.config_dir / group["config_file"]
                    if ext_path.exists():
                        def on_delete_file(delete_it: bool) -> None:
                            if delete_it:
                                self.app._backup_file(ext_path)
                                ext_path.unlink()
                                self.notify(f"Deleted {group['config_file']}")
                            finish_delete()

                        self.app.push_screen(
                            ConfirmModal(f"Also delete {group['config_file']}?"),
                            on_delete_file,
                        )
                        return

                finish_delete()

            self.app.push_screen(
                ConfirmModal(f"Delete group '{name}'?"), on_confirm
            )

        def action_go_back(self) -> None:
            self.app.pop_screen()

    class GroupScreenBase(Screen):
        """Base class for screens that operate on a single group by index."""

        def __init__(self, group_index: int) -> None:
            super().__init__()
            self.group_index = group_index

        def _get_group_name(self) -> str:
            groups = self.app.config.get("groups", [])
            return groups[self.group_index].get("name", "unnamed")

        def _get_group_data(self):
            groups = self.app.config.get("groups", [])
            group_entry = groups[self.group_index]
            is_external = "config_file" in group_entry
            full_config = self.app._load_group_config(group_entry)
            return group_entry, full_config, is_external

        def action_go_back(self) -> None:
            self.app.pop_screen()

    class GroupDetailScreen(GroupScreenBase):
        """View/edit a specific group."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
        ]

        def compose(self) -> ComposeResult:
            group_name = self._get_group_name()
            _, full_config, _ = self._get_group_data()
            sources = full_config.get("sources", [])
            repos = []
            for s in sources:
                repos.extend(s.get("repositories", []))
            tc = full_config.get("team_context", {})

            yield Header()
            yield Static(
                f"[bold bright_cyan]>> Group: {group_name}[/bold bright_cyan]\n"
                f"  [dim]Repos:[/dim] {', '.join(repos) if repos else '(none)'}\n"
                f"  [dim]Team:[/dim]  {tc.get('name') or '(not set)'}",
                id="group-title",
            )
            yield VimListView(
                ListItem(Label("[bold]Edit sources[/bold]      [dim]Repositories and filters[/dim]"), id="sources"),
                ListItem(Label("[bold]Edit team context[/bold] [dim]Name, focus areas, priorities[/dim]"), id="team-context"),
                ListItem(Label("[bold]View config JSON[/bold]  [dim]Read-only rendered view[/dim]"), id="view-json"),
                id="group-menu",
            )
            yield Static("[dim]Enter[/dim] select  [dim]hjkl[/dim] navigate  [dim]b[/dim] back", id="help")
            yield Footer()

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            item_id = event.item.id
            if item_id == "sources":
                self.app.push_screen(EditSourcesScreen(self.group_index))
            elif item_id == "team-context":
                self.app.push_screen(EditTeamContextScreen(self.group_index))
            elif item_id == "view-json":
                self.app.push_screen(JsonViewScreen(self.group_index))

    class EditSourcesScreen(GroupScreenBase):
        """Full screen for managing repositories on a group."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
            Binding("a", "add_repo", "Add repo"),
            Binding("d", "delete_repo", "Delete repo"),
            Binding("f", "edit_filters", "Filters"),
        ]

        def compose(self) -> ComposeResult:
            group_name = self._get_group_name()
            yield Header()
            yield Static(
                f"[bold bright_cyan]>> Sources: {group_name}[/bold bright_cyan]",
                id="screen-title",
            )
            table = VimDataTable(id="sources-table", zebra_stripes=True)
            table.add_columns("#", "Repository")
            yield table
            yield Static(
                "[dim]a[/dim] add  "
                "[dim]d[/dim] delete  "
                "[dim]f[/dim] filters  "
                "[dim]hjkl[/dim] navigate  "
                "[dim]b[/dim] back",
                id="help",
            )
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_table()

        def _refresh_table(self) -> None:
            table = self.query_one("#sources-table", DataTable)
            table.clear()
            _, full_config, _ = self._get_group_data()
            sources = full_config.get("sources", [])
            idx = 1
            for src in sources:
                for repo in src.get("repositories", []):
                    table.add_row(str(idx), repo)
                    idx += 1

        def action_add_repo(self) -> None:
            def on_input(value: str) -> None:
                if not value:
                    return
                repo = value.strip()
                parts = repo.split("/")
                if len(parts) != 2 or not all(parts):
                    self.notify("Invalid format. Use owner/repo",
                                severity="error")
                    return
                group_entry, full_config, is_external = self._get_group_data()
                sources = full_config.setdefault("sources", [])
                if not sources:
                    sources.append({"type": "github", "repositories": []})
                repos_list = sources[0].setdefault("repositories", [])
                if repo in repos_list:
                    self.notify("Already present", severity="warning")
                    return
                repos_list.append(repo)
                self.app._save_group_config(group_entry, full_config,
                                            is_external)
                self.notify(f"Added {repo}")
                self._refresh_table()

            self.app.push_screen(
                InputModal("Add Repository", "Repository (owner/repo)",
                           placeholder="owner/repo"),
                on_input,
            )

        def action_delete_repo(self) -> None:
            _, full_config, _ = self._get_group_data()
            all_repos = []
            for src in full_config.get("sources", []):
                all_repos.extend(src.get("repositories", []))
            if not all_repos:
                self.notify("No repositories to delete", severity="warning")
                return

            table = self.query_one("#sources-table", DataTable)
            row_index = table.cursor_row
            if row_index < 0 or row_index >= len(all_repos):
                return
            repo_to_remove = all_repos[row_index]

            def on_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                group_entry, full_config, is_external = self._get_group_data()
                for src in full_config.get("sources", []):
                    repos = src.get("repositories", [])
                    if repo_to_remove in repos:
                        repos.remove(repo_to_remove)
                        break
                self.app._save_group_config(group_entry, full_config,
                                            is_external)
                self.notify(f"Removed {repo_to_remove}")
                self._refresh_table()

            self.app.push_screen(
                ConfirmModal(f"Remove '{repo_to_remove}'?"), on_confirm
            )

        def action_edit_filters(self) -> None:
            self.app.push_screen(EditFiltersScreen(self.group_index))

    class EditFiltersScreen(GroupScreenBase):
        """Full screen for managing filters on a source."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
            Binding("e", "edit_filter", "Edit"),
            Binding("x", "clear_all", "Clear all"),
        ]

        def compose(self) -> ComposeResult:
            group_name = self._get_group_name()
            yield Header()
            yield Static(
                f"[bold bright_cyan]>> Filters: {group_name}[/bold bright_cyan]",
                id="screen-title",
            )
            table = VimDataTable(id="filters-table", zebra_stripes=True)
            table.add_columns("Filter", "Values")
            yield table
            yield Static(
                "[dim]e[/dim] edit  "
                "[dim]x[/dim] clear all  "
                "[dim]hjkl[/dim] navigate  "
                "[dim]b[/dim] back",
                id="help",
            )
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_table()

        def _refresh_table(self) -> None:
            table = self.query_one("#filters-table", DataTable)
            table.clear()
            _, full_config, _ = self._get_group_data()
            sources = full_config.get("sources", [])
            filters = sources[0].get("filters", {}) if sources else {}
            table.add_row("exclude_authors",
                          ", ".join(filters.get("exclude_authors", [])) or "(none)")
            table.add_row("exclude_titles",
                          ", ".join(filters.get("exclude_titles", [])) or "(none)")
            table.add_row("exclude_labels",
                          ", ".join(filters.get("exclude_labels", [])) or "(none)")

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            self._edit_row(event.cursor_row)

        def action_edit_filter(self) -> None:
            table = self.query_one("#filters-table", DataTable)
            self._edit_row(table.cursor_row)

        def _edit_row(self, row_index: int) -> None:
            filter_keys = ["exclude_authors", "exclude_titles", "exclude_labels"]
            labels = {
                "exclude_authors": "Exclude authors (comma-separated)",
                "exclude_titles": "Exclude title patterns (comma-separated regex)",
                "exclude_labels": "Exclude labels (comma-separated)",
            }
            if row_index < 0 or row_index >= len(filter_keys):
                return
            key = filter_keys[row_index]

            _, full_config, _ = self._get_group_data()
            sources = full_config.get("sources", [])
            filters = sources[0].get("filters", {}) if sources else {}
            current = ", ".join(filters.get(key, []))

            def on_input(value: str) -> None:
                if value == "" and not current:
                    return  # Cancel on empty when nothing was set
                group_entry, full_config, is_external = self._get_group_data()
                sources = full_config.setdefault("sources", [])
                if not sources:
                    sources.append({"type": "github", "repositories": []})
                source = sources[0]
                filters = source.setdefault("filters", {})

                parsed = [v.strip() for v in value.split(",") if v.strip()]

                # Validate regex for exclude_titles
                if key == "exclude_titles" and parsed:
                    for p in parsed:
                        try:
                            re.compile(p)
                        except re.error as e:
                            self.notify(f"Invalid regex '{p}': {e}",
                                        severity="error")
                            return

                if parsed:
                    filters[key] = parsed
                else:
                    filters.pop(key, None)

                if not any(filters.values()):
                    source.pop("filters", None)

                self.app._save_group_config(group_entry, full_config,
                                            is_external)
                self.notify(f"Updated {key}")
                self._refresh_table()

            self.app.push_screen(
                InputModal(key, labels[key], default=current),
                on_input,
            )

        def action_clear_all(self) -> None:
            def on_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                group_entry, full_config, is_external = self._get_group_data()
                sources = full_config.get("sources", [])
                if sources:
                    sources[0].pop("filters", None)
                self.app._save_group_config(group_entry, full_config,
                                            is_external)
                self.notify("Filters cleared")
                self._refresh_table()

            self.app.push_screen(
                ConfirmModal("Clear all filters?"), on_confirm
            )

    class EditTeamContextScreen(GroupScreenBase):
        """Full screen for editing team context fields."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
            Binding("e", "edit_field", "Edit"),
        ]

        def compose(self) -> ComposeResult:
            group_name = self._get_group_name()
            yield Header()
            yield Static(
                f"[bold bright_cyan]>> Team Context: {group_name}[/bold bright_cyan]",
                id="screen-title",
            )
            table = VimDataTable(id="team-context-table", zebra_stripes=True)
            table.add_columns("Field", "Value")
            yield table
            yield Static(
                "[dim]e[/dim] edit  "
                "[dim]hjkl[/dim] navigate  "
                "[dim]b[/dim] back",
                id="help",
            )
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_table()

        def _refresh_table(self) -> None:
            table = self.query_one("#team-context-table", DataTable)
            table.clear()
            _, full_config, _ = self._get_group_data()
            tc = full_config.get("team_context", {})
            table.add_row("name", tc.get("name", "") or "(not set)")
            table.add_row("focus_areas",
                          ", ".join(tc.get("focus_areas", [])) or "(none)")
            table.add_row("priorities",
                          ", ".join(tc.get("priorities", [])) or "(none)")

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            self._edit_row(event.cursor_row)

        def action_edit_field(self) -> None:
            table = self.query_one("#team-context-table", DataTable)
            self._edit_row(table.cursor_row)

        def _edit_row(self, row_index: int) -> None:
            fields = ["name", "focus_areas", "priorities"]
            labels = {
                "name": "Team name",
                "focus_areas": "Focus areas (comma-separated)",
                "priorities": "Priorities (comma-separated, first = highest)",
            }
            if row_index < 0 or row_index >= len(fields):
                return
            field = fields[row_index]

            _, full_config, _ = self._get_group_data()
            tc = full_config.get("team_context", {})

            if field == "name":
                current = tc.get("name", "")
            else:
                current = ", ".join(tc.get(field, []))

            def on_input(value: str) -> None:
                if value == "" and not current:
                    return
                group_entry, full_config, is_external = self._get_group_data()
                tc = full_config.setdefault("team_context", {})

                if field == "name":
                    tc["name"] = value.strip()
                else:
                    tc[field] = [v.strip() for v in value.split(",")
                                 if v.strip()]

                self.app._save_group_config(group_entry, full_config,
                                            is_external)
                self.notify(f"Updated {field}")
                self._refresh_table()

            self.app.push_screen(
                InputModal(field, labels[field], default=current),
                on_input,
            )

    class JsonViewScreen(GroupScreenBase):
        """Read-only JSON view of a group config."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
            Binding("j", "scroll_down", "Down", show=False),
            Binding("k", "scroll_up", "Up", show=False),
        ]

        def compose(self) -> ComposeResult:
            group_name = self._get_group_name()
            _, full_config, _ = self._get_group_data()
            display = dict(full_config)
            display.pop("name", None)
            formatted = json.dumps(display, indent=2)

            yield Header()
            yield Static(
                f"[bold bright_cyan]>> Config: {group_name}[/bold bright_cyan]",
                id="screen-title",
            )
            with VerticalScroll(id="json-scroll"):
                yield Static(
                    Syntax(formatted, "json", theme="monokai",
                           word_wrap=True),
                    id="json-view",
                )
            yield Static("[dim]j/k[/dim] scroll  [dim]b[/dim] back", id="help")
            yield Footer()

        def action_scroll_down(self) -> None:
            self.query_one("#json-scroll", VerticalScroll).scroll_down()

        def action_scroll_up(self) -> None:
            self.query_one("#json-scroll", VerticalScroll).scroll_up()

    class SecretsScreen(Screen):
        """Manage webhook secrets per group."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
        ]

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("[bold bright_cyan]>> Secrets[/bold bright_cyan]", id="screen-title")
            table = VimDataTable(id="secrets-table", zebra_stripes=True)
            table.add_columns("Group", "Webhook URL", "Source")
            yield table
            yield Static(
                "[dim]Enter[/dim] set/update  [dim]hjkl[/dim] navigate  [dim]b[/dim] back",
                id="help",
            )
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_table()

        def _refresh_table(self) -> None:
            table = self.query_one("#secrets-table", DataTable)
            table.clear()
            groups = self.app.config.get("groups", [])
            for g in groups:
                name = g.get("name", "unnamed")
                secrets = self.app._load_secrets(name)
                webhook = secrets.get("teams_webhook_url", "")
                if webhook:
                    table.add_row(name, self.app._mask_secret(webhook),
                                  f"secrets/{name}.json")
                else:
                    table.add_row(name, "(not set)", "-")

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            row_index = event.cursor_row
            groups = self.app.config.get("groups", [])
            if row_index < 0 or row_index >= len(groups):
                return

            group_name = groups[row_index].get("name", "unnamed")
            secrets = self.app._load_secrets(group_name)
            current = secrets.get("teams_webhook_url", "")

            if current:
                # Webhook exists — offer update or remove
                def on_action(action: str) -> None:
                    if not action:
                        return
                    if action == "update":
                        def on_url(url: str) -> None:
                            url = url.strip()
                            if not url:
                                return
                            fresh = self.app._load_secrets(group_name)
                            fresh["teams_webhook_url"] = url
                            self.app._save_secrets(group_name, fresh)
                            self.notify(f"Updated webhook for {group_name}")
                            self._refresh_table()

                        self.app.push_screen(
                            InputModal("Webhook URL", "Enter URL",
                                       default=current),
                            on_url,
                        )
                    elif action == "remove":
                        def on_remove_confirm(confirmed: bool) -> None:
                            if not confirmed:
                                return
                            fresh = self.app._load_secrets(group_name)
                            fresh.pop("teams_webhook_url", None)
                            if fresh:
                                self.app._save_secrets(group_name, fresh)
                            else:
                                secrets_path = (self.app.config_dir /
                                                "secrets" /
                                                f"{group_name}.json")
                                if secrets_path.exists():
                                    self.app._backup_file(secrets_path)
                                    secrets_path.unlink()
                            self.notify(f"Removed webhook for {group_name}")
                            self._refresh_table()

                        self.app.push_screen(
                            ConfirmModal("Remove webhook URL?"),
                            on_remove_confirm,
                        )

                self.app.push_screen(
                    SelectModal("Webhook Action", [
                        ("update", "Update URL"),
                        ("remove", "Remove URL"),
                    ]),
                    on_action,
                )
            else:
                # No webhook — offer to set one
                def on_url(url: str) -> None:
                    url = url.strip()
                    if not url:
                        return
                    fresh = self.app._load_secrets(group_name)
                    fresh["teams_webhook_url"] = url
                    self.app._save_secrets(group_name, fresh)
                    self.notify(f"Set webhook for {group_name}")
                    self._refresh_table()

                self.app.push_screen(
                    InputModal("Set Webhook", "Webhook URL",
                               placeholder="https://..."),
                    on_url,
                )

        def action_go_back(self) -> None:
            self.app.pop_screen()

    class ValidateScreen(Screen):
        """Run and display configuration validation."""

        BINDINGS = [
            Binding("b", "go_back", "Back"),
            Binding("j", "scroll_down", "Down", show=False),
            Binding("k", "scroll_up", "Up", show=False),
            Binding("r", "rerun", "Re-run"),
        ]

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("[bold bright_cyan]>> Validate[/bold bright_cyan]", id="screen-title")
            with VerticalScroll(id="validate-scroll"):
                yield Static("[dim]Running validation...[/dim]", id="validate-output")
            yield Static(
                "[dim]j/k[/dim] scroll  [dim]r[/dim] re-run  [dim]b[/dim] back", id="help"
            )
            yield Footer()

        def action_scroll_down(self) -> None:
            self.query_one("#validate-scroll", VerticalScroll).scroll_down()

        def action_scroll_up(self) -> None:
            self.query_one("#validate-scroll", VerticalScroll).scroll_up()

        def on_mount(self) -> None:
            self._run_validation()

        def _run_validation(self) -> None:
            output_widget = self.query_one("#validate-output", Static)
            try:
                config_path_str = (str(self.app.config_path)
                                   if self.app.config_path else None)
                output = _capture_validation_output(config_path_str)
                lines = [_colorize_validation_line(line, use_markers=True)
                         for line in output.strip().split('\n')]
                output_widget.update("\n".join(lines))
            except Exception as e:
                output_widget.update(f"[red]Validation error: {e}[/red]")

        def action_rerun(self) -> None:
            self._run_validation()

        def action_go_back(self) -> None:
            self.app.pop_screen()

    class ConfigApp(ConfigStore, App):
        """Textual TUI for Herald configuration management."""

        TITLE = "Herald Configuration"
        SUB_TITLE = "Manage your Herald config interactively"

        CSS = """
        Screen {
            background: $surface;
        }

        /* --- Banner / screen titles --- */
        #banner {
            margin: 1 3;
            height: auto;
            padding: 1 2;
            background: $panel;
            border: round $accent;
        }
        #screen-title {
            margin: 1 3 0 3;
            height: auto;
            padding: 0 1;
        }

        /* --- List views (main menu, group detail) --- */
        #main-menu, #group-menu {
            margin: 1 3;
            height: auto;
            padding: 1 0;
        }
        #main-menu > ListItem, #group-menu > ListItem {
            padding: 0 2;
        }

        /* --- Data tables --- */
        #defaults-table, #groups-table, #secrets-table,
        #sources-table, #filters-table, #team-context-table {
            margin: 1 3;
            height: auto;
            max-height: 60%;
            border: round $primary-background-lighten-2;
            padding: 0 1;
        }

        /* --- JSON view --- */
        VerticalScroll {
            margin: 1 3;
            height: 1fr;
            border: round $primary-background-lighten-2;
            padding: 1 2;
        }
        #json-view {
            height: auto;
        }

        /* --- Group detail title --- */
        #group-title {
            margin: 1 3;
            height: auto;
            padding: 1 2;
            background: $panel;
            border: round $accent;
        }

        /* --- Validate output --- */
        #validate-output {
            height: auto;
            padding: 0 1;
        }

        /* --- Help bar (bottom hint line) --- */
        #help {
            dock: bottom;
            height: 1;
            margin: 0 3;
            padding: 0 1;
            color: $text-muted;
            border-top: dashed $primary-background-lighten-2;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit", "Quit", show=False),
            Binding("q", "quit", "Quit"),
        ]

        def __init__(self, config_path: Optional[str] = None, **kwargs):
            super().__init__(**kwargs)
            self._init_config(config_path)

        def on_mount(self) -> None:
            self.push_screen(MainMenuScreen())


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Herald - Multi-Source Repository Activity Tracker"
    )

    subparsers = parser.add_subparsers(dest="command")

    # 'config' subcommand
    config_parser = subparsers.add_parser(
        "config",
        help="Interactive configuration manager (requires textual or rich)"
    )
    config_parser.add_argument(
        '--config',
        help='Path to configuration file'
    )

    # All existing flags on the top-level parser (unchanged)
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

    # Handle 'config' subcommand
    if args.command == "config":
        config_path = args.config or os.environ.get("HERALD_CONFIG")
        if HAS_TEXTUAL:
            app = ConfigApp(config_path=config_path)
            app.run()
        elif HAS_RICH:
            print("Tip: Install textual for enhanced TUI: pip install textual")
            manager = ConfigManager(config_path=config_path)
            try:
                manager.run()
            except KeyboardInterrupt:
                print("\nExiting.")
        else:
            print("Error: 'textual' module not found. Install with: pip install textual")
            sys.exit(1)
        return

    # -- Existing run logic (command is None) --

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
        logger.info("Output saved: %s", args.output)
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
