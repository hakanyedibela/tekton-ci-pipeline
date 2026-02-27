from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_TIMEOUT = 15


class GitHubPoller:
    """Polls GitHub REST API for open pull requests."""

    def __init__(self, token: Optional[str] = None):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    def get_open_prs(self, owner: str, repo: str) -> list[dict]:
        """Return list of open PRs as ``[{number, head_sha, branch}, ...]``."""
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/pulls"
        params = {"state": "open", "per_page": 100}
        results: list[dict] = []
        page = 1

        while True:
            params["page"] = page
            try:
                resp = self._session.get(url, params=params, timeout=_TIMEOUT)
                resp.raise_for_status()
            except requests.RequestException as exc:
                log.warning("GitHub API error for %s/%s: %s", owner, repo, exc)
                break

            prs = resp.json()
            if not prs:
                break

            for pr in prs:
                results.append(
                    {
                        "number": pr["number"],
                        "head_sha": pr["head"]["sha"],
                        "branch": pr["head"]["ref"],
                    }
                )

            if len(prs) < 100:
                break
            page += 1

        log.debug("Found %d open PRs for %s/%s", len(results), owner, repo)
        return results
