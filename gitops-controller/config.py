from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class RepoConfig:
    url: str
    branch: str
    image_name: str
    image_registry: str
    gitops_repo_url: str
    gitops_values_path: str
    registry_secret: str
    gitops_secret: str
    watch_prs: bool
    github_owner: str
    github_repo: str


@dataclass
class WatchConfig:
    poll_interval_seconds: int
    github_token: Optional[str]
    repositories: list[RepoConfig] = field(default_factory=list)


class ControllerConfig:
    def __init__(self, core_v1, namespace: str):
        self._core_v1 = core_v1
        self._namespace = namespace
        self._config_map_name = os.environ.get("WATCH_CONFIG_MAP", "gitops-watch-config")

    def load(self) -> WatchConfig:
        cm = self._core_v1.read_namespaced_config_map(
            name=self._config_map_name,
            namespace=self._namespace,
        )
        raw = yaml.safe_load(cm.data["config.yaml"])

        github_token = os.environ.get("GITHUB_API_TOKEN") or raw.get("github_api_token")

        repos = [
            RepoConfig(
                url=r["url"],
                branch=r.get("branch", "main"),
                image_name=r["image_name"],
                image_registry=r["image_registry"],
                gitops_repo_url=r["gitops_repo_url"],
                gitops_values_path=r["gitops_values_path"],
                registry_secret=r.get("registry_secret", "harbor-registry-secret"),
                gitops_secret=r.get("gitops_secret", "gitops-git-credentials"),
                watch_prs=r.get("watch_prs", False),
                github_owner=r.get("github_owner", ""),
                github_repo=r.get("github_repo", ""),
            )
            for r in raw.get("repositories", [])
        ]

        return WatchConfig(
            poll_interval_seconds=raw.get("poll_interval_seconds", 60),
            github_token=github_token,
            repositories=repos,
        )
