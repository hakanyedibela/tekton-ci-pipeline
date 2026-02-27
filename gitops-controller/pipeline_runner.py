from __future__ import annotations

import logging
import os
import time
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from config import RepoConfig

log = logging.getLogger(__name__)

_GROUP = "tekton.dev"
_VERSION = "v1"
_PLURAL = "pipelineruns"


def _now_label() -> str:
    return str(int(time.time()))


class PipelineRunner:
    """Creates Tekton PipelineRuns via the Kubernetes custom-resource API."""

    def __init__(self, custom_api, namespace: str):
        self._custom = custom_api
        self._namespace = namespace

    def create_for_branch(self, repo: RepoConfig, sha: str) -> str:
        """Create a full CI/CD PipelineRun for a branch commit. Returns the run name."""
        name = f"branch-{repo.image_name}-{sha[:7]}-{_now_label()}"
        body = self._branch_run_body(name, repo, sha)
        self._create(body)
        return name

    def create_for_pr(self, repo: RepoConfig, pr_number: int, sha: str) -> str:
        """Create a lightweight PR CI PipelineRun. Returns the run name."""
        name = f"pr-{repo.image_name}-{pr_number}-{sha[:7]}-{_now_label()}"
        body = self._pr_run_body(name, repo, pr_number, sha)
        self._create(body)
        return name

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _create(self, body: dict[str, Any]) -> None:
        try:
            self._custom.create_namespaced_custom_object(
                group=_GROUP,
                version=_VERSION,
                namespace=self._namespace,
                plural=_PLURAL,
                body=body,
            )
        except ApiException as exc:
            log.error("Failed to create PipelineRun: %s", exc)
            raise

    def _branch_run_body(self, name: str, repo: RepoConfig, sha: str) -> dict:
        return {
            "apiVersion": f"{_GROUP}/{_VERSION}",
            "kind": "PipelineRun",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "gitops-controller",
                    "tekton.dev/pipeline": "java-microservice-ci-cd",
                    "gitops-controller/repo": repo.image_name,
                    "gitops-controller/branch": repo.branch,
                },
            },
            "spec": {
                "pipelineRef": {"name": "java-microservice-ci-cd"},
                "timeouts": {"pipeline": "1h0m0s"},
                "params": [
                    {"name": "git-url", "value": repo.url},
                    {"name": "git-revision", "value": sha},
                    {"name": "image-registry", "value": repo.image_registry},
                    {"name": "image-name", "value": repo.image_name},
                    {"name": "gitops-repo-url", "value": repo.gitops_repo_url},
                    {
                        "name": "gitops-values-path",
                        "value": repo.gitops_values_path,
                    },
                    {"name": "deploy-to-prod", "value": "false"},
                    {
                        "name": "maven-goals",
                        "value": ["clean", "verify", "-Dmaven.test.failure.ignore=false"],
                    },
                ],
                "workspaces": [
                    {
                        "name": "source",
                        "volumeClaimTemplate": {
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {"requests": {"storage": "2Gi"}},
                            }
                        },
                    },
                    {
                        "name": "maven-cache",
                        "persistentVolumeClaim": {"claimName": "maven-cache-pvc"},
                    },
                    {
                        "name": "docker-credentials",
                        "secret": {"secretName": repo.registry_secret},
                    },
                    {
                        "name": "gitops-credentials",
                        "secret": {"secretName": repo.gitops_secret},
                    },
                ],
            },
        }

    def _pr_run_body(self, name: str, repo: RepoConfig, pr_number: int, sha: str) -> dict:
        return {
            "apiVersion": f"{_GROUP}/{_VERSION}",
            "kind": "PipelineRun",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "gitops-controller",
                    "tekton.dev/pipeline": "java-microservice-pr-ci",
                    "gitops-controller/repo": repo.image_name,
                    "gitops-controller/pr": str(pr_number),
                },
            },
            "spec": {
                "pipelineRef": {"name": "java-microservice-pr-ci"},
                "timeouts": {"pipeline": "30m0s"},
                "params": [
                    {"name": "git-url", "value": repo.url},
                    {"name": "git-revision", "value": sha},
                    {"name": "pr-number", "value": str(pr_number)},
                    {
                        "name": "maven-goals",
                        "value": ["clean", "verify", "-Dmaven.test.failure.ignore=false"],
                    },
                ],
                "workspaces": [
                    {
                        "name": "source",
                        "volumeClaimTemplate": {
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {"requests": {"storage": "2Gi"}},
                            }
                        },
                    },
                    {
                        "name": "maven-cache",
                        "persistentVolumeClaim": {"claimName": "maven-cache-pvc"},
                    },
                ],
            },
        }
