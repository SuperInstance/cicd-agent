"""
Git Change Detection
=====================
Polls git repos for new commits and extracts change metadata.
Supports both polling and webhook modes.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet.cicd.git")


@dataclass
class GitCommit:
    """Represents a single git commit."""
    sha: str
    author: str = ""
    email: str = ""
    date: str = ""
    message: str = ""
    branch: str = "main"
    changed_files: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # e.g. [skip-ci], [deploy], [urgent]

    def to_dict(self) -> dict:
        return {
            "sha": self.sha,
            "author": self.author,
            "email": self.email,
            "date": self.date,
            "message": self.message,
            "branch": self.branch,
            "changed_files": self.changed_files,
            "tags": self.tags,
        }


class GitPoller:
    """Polls git repos for new commits.

    Tracks the last-known commit SHA per repo and detects new changes
    by running ``git fetch`` + ``git log`` comparisons.

    Usage::

        poller = GitPoller()
        poller.add_repo("my-agent", "/path/to/repo", "main")
        poller.poll_all()  # -> {"my-agent": [GitCommit(...)]}
    """

    # Patterns to detect CI-trigger tags in commit messages
    TAG_PATTERNS = {
        "skip-ci": r"\[skip-ci\]|\[ci skip\]",
        "deploy": r"\[deploy\]|\[deploy-now\]",
        "urgent": r"\[urgent\]|\[hotfix\]",
        "no-test": r"\[no-test\]",
        "force": r"\[force-ci\]",
    }

    def __init__(self, interval: int = 60):
        self.interval = interval
        self._repos: dict[str, dict] = {}
        self._last_known: dict[str, str] = {}
        self._last_poll: dict[str, str] = {}

    # -- Repo Management --

    def add_repo(self, name: str, path: str, branch: str = "main") -> None:
        """Register a repo for polling."""
        self._repos[name] = {
            "path": path,
            "branch": branch,
        }
        # Initialise the known SHA from the current HEAD
        sha = self._get_head_sha(path, branch)
        if sha:
            self._last_known[name] = sha
        logger.info("GitPoller: tracking repo %s at %s (branch=%s, head=%s)",
                     name, path, branch, sha or "N/A")

    def remove_repo(self, name: str) -> bool:
        """Unregister a repo."""
        if name in self._repos:
            del self._repos[name]
            self._last_known.pop(name, None)
            self._last_poll.pop(name, None)
            return True
        return False

    def set_interval(self, seconds: int) -> None:
        """Update the polling interval."""
        self.interval = seconds

    # -- Polling --

    def poll_all(self) -> dict[str, list[GitCommit]]:
        """Poll every registered repo and return new commits per repo.

        Returns a dict ``{repo_name: [GitCommit, ...]}`` containing only
        the *new* commits since the last known SHA.
        """
        results: dict[str, list[GitCommit]] = {}
        for name, info in self._repos.items():
            try:
                commits = self._poll_repo(name, info["path"], info["branch"])
                results[name] = commits
            except Exception as e:
                logger.error("Error polling %s: %s", name, e)
                results[name] = []
        return results

    def poll_repo(self, name: str) -> list[GitCommit]:
        """Poll a single repo by name."""
        info = self._repos.get(name)
        if info is None:
            logger.warning("Unknown repo: %s", name)
            return []
        return self._poll_repo(name, info["path"], info["branch"])

    def _poll_repo(self, name: str, path: str, branch: str) -> list[GitCommit]:
        """Fetch new commits and compare with the last known SHA."""
        # Step 1: fetch from origin
        self._git_fetch(path)

        # Step 2: get current HEAD on the tracking branch
        current_sha = self._get_head_sha(path, branch)
        if not current_sha:
            logger.warning("Cannot resolve HEAD for %s on branch %s", name, branch)
            return []

        last_known = self._last_known.get(name, "")
        if not last_known:
            # First poll — record HEAD but report no new commits
            self._last_known[name] = current_sha
            self._last_poll[name] = datetime.now(timezone.utc).isoformat()
            return []

        if current_sha == last_known:
            self._last_poll[name] = datetime.now(timezone.utc).isoformat()
            return []

        # Step 3: list new commits between last_known and current
        commits = self._get_new_commits(path, last_known, current_sha, branch)
        if commits:
            self._last_known[name] = current_sha
            logger.info("Repo %s: %d new commit(s)", name, len(commits))

        self._last_poll[name] = datetime.now(timezone.utc).isoformat()
        return commits

    # -- Git Operations --

    def _git_fetch(self, path: str) -> bool:
        """Run ``git fetch origin`` in the repo."""
        try:
            result = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("git fetch failed for %s: %s", path, result.stderr.strip())
                return False
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.error("git fetch error in %s: %s", path, e)
            return False

    def _get_head_sha(self, path: str, branch: str) -> Optional[str]:
        """Get the current HEAD SHA for a branch."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", f"origin/{branch}"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            # Fall back to local branch
            result = subprocess.run(
                ["git", "rev-parse", branch],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    def _get_new_commits(self, path: str, from_sha: str, to_sha: str,
                         branch: str) -> list[GitCommit]:
        """Get a list of new GitCommit objects between two SHAs."""
        try:
            # Pretty-format: sha, author, email, date, subject
            fmt = "%H%n%an%n%ae%n%aI%n%s"
            result = subprocess.run(
                ["git", "log", f"{from_sha}..{to_sha}", f"--pretty=format:{fmt}"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return []

            raw = result.stdout.strip()
            if not raw:
                return []

            # Parse blocks separated by blank lines (each block = one commit)
            blocks = re.split(r"\n\n+", raw)
            commits = []
            for block in blocks:
                lines = block.strip().split("\n")
                if len(lines) < 5:
                    continue
                sha = lines[0].strip()
                author = lines[1].strip()
                email = lines[2].strip()
                date = lines[3].strip()
                message = "\n".join(lines[4:]).strip()

                # Get changed files for this commit
                changed_files = self._get_changed_files(path, sha)
                tags = self._extract_tags(message)

                commits.append(GitCommit(
                    sha=sha,
                    author=author,
                    email=email,
                    date=date,
                    message=message,
                    branch=branch,
                    changed_files=changed_files,
                    tags=tags,
                ))
            return commits

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.error("Error getting commits: %s", e)
            return []

    def _get_changed_files(self, path: str, sha: str) -> list[str]:
        """Get the list of files changed in a commit."""
        try:
            result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return []

    # -- Tag Parsing --

    def _extract_tags(self, message: str) -> list[str]:
        """Extract CI-trigger tags from a commit message."""
        tags = []
        for tag, pattern in self.TAG_PATTERNS.items():
            if re.search(pattern, message, re.IGNORECASE):
                tags.append(tag)
        return tags

    # -- Status --

    def get_status(self) -> dict:
        """Return the current state of the poller."""
        return {
            "interval": self.interval,
            "repos": {
                name: {
                    "branch": info["branch"],
                    "last_known_sha": self._last_known.get(name, ""),
                    "last_poll": self._last_poll.get(name, ""),
                }
                for name, info in self._repos.items()
            },
        }

    def get_last_known(self, repo_name: str) -> Optional[str]:
        """Get the last known commit SHA for a repo."""
        return self._last_known.get(repo_name)

    def set_last_known(self, repo_name: str, sha: str) -> None:
        """Manually set the last known commit SHA (useful for testing)."""
        self._last_known[repo_name] = sha
