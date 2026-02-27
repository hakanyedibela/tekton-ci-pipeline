from __future__ import annotations

import logging
import subprocess
from typing import Optional

log = logging.getLogger(__name__)


class GitPoller:
    """Polls branch heads without cloning by using ``git ls-remote``."""

    def get_head_sha(self, repo_url: str, branch: str) -> Optional[str]:
        """Return the current HEAD SHA for *branch* in *repo_url*, or ``None`` on error."""
        ref = f"refs/heads/{branch}"
        try:
            result = subprocess.run(
                ["git", "ls-remote", repo_url, ref],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log.warning("git ls-remote timed out for %s@%s", repo_url, branch)
            return None
        except FileNotFoundError:
            log.error("git binary not found — is it installed in the container?")
            return None

        if result.returncode != 0:
            log.warning(
                "git ls-remote failed for %s@%s: %s",
                repo_url,
                branch,
                result.stderr.strip(),
            )
            return None

        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                sha = parts[0]
                log.debug("HEAD SHA for %s@%s = %s", repo_url, branch, sha)
                return sha

        log.warning("Ref %s not found in %s", ref, repo_url)
        return None
