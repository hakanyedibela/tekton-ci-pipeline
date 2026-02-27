"""GitOps polling controller.

Runs a reconciliation loop that:
1. Reads repository configuration from a ConfigMap.
2. Polls each repo's branch HEAD (via git ls-remote) and GitHub PRs (via REST API).
3. Creates Tekton PipelineRuns when new commits are detected.
4. Persists the last-seen SHAs so each commit triggers exactly one PipelineRun.
"""
from __future__ import annotations

import logging
import os
import time

from kubernetes import client as k8s_client, config as k8s_config

from config import ControllerConfig, WatchConfig
from git_poller import GitPoller
from github_poller import GitHubPoller
from pipeline_runner import PipelineRunner
from state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("controller")


def reconcile(
    cfg: ControllerConfig,
    state_mgr: StateManager,
    git_poller: GitPoller,
    github_poller: GitHubPoller,
    runner: PipelineRunner,
) -> None:
    watch_config: WatchConfig = cfg.load()
    state = state_mgr.load()
    state.setdefault("branches", {})
    state.setdefault("prs", {})

    for repo in watch_config.repositories:
        # ---- Branch polling ------------------------------------------------
        sha = git_poller.get_head_sha(repo.url, repo.branch)
        key = f"{repo.url}#{repo.branch}"
        if sha and sha != state["branches"].get(key):
            try:
                run_name = runner.create_for_branch(repo, sha)
                log.info(
                    "Created branch PipelineRun: %s  (repo=%s branch=%s sha=%s)",
                    run_name,
                    repo.image_name,
                    repo.branch,
                    sha[:12],
                )
                state["branches"][key] = sha
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to create branch PipelineRun for %s: %s", repo.url, exc)

        # ---- PR polling ----------------------------------------------------
        if repo.watch_prs and watch_config.github_token:
            try:
                prs = github_poller.get_open_prs(repo.github_owner, repo.github_repo)
            except Exception as exc:  # noqa: BLE001
                log.warning("PR polling failed for %s/%s: %s", repo.github_owner, repo.github_repo, exc)
                prs = []

            for pr in prs:
                pr_key = f"{repo.url}#PR-{pr['number']}"
                pr_sha = pr["head_sha"]
                if pr_sha != state["prs"].get(pr_key):
                    try:
                        run_name = runner.create_for_pr(repo, pr["number"], pr_sha)
                        log.info(
                            "Created PR PipelineRun: %s  (repo=%s pr=#%d sha=%s)",
                            run_name,
                            repo.image_name,
                            pr["number"],
                            pr_sha[:12],
                        )
                        state["prs"][pr_key] = pr_sha
                    except Exception as exc:  # noqa: BLE001
                        log.error(
                            "Failed to create PR PipelineRun for %s#%d: %s",
                            repo.url,
                            pr["number"],
                            exc,
                        )

    state_mgr.save(state)


def main() -> None:
    namespace = os.environ.get("NAMESPACE", "default")

    # Load Kubernetes configuration (works both in-cluster and locally)
    try:
        k8s_config.load_incluster_config()
        log.info("Loaded in-cluster Kubernetes config.")
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        log.info("Loaded kubeconfig.")

    core_v1 = k8s_client.CoreV1Api()
    custom_api = k8s_client.CustomObjectsApi()

    cfg = ControllerConfig(core_v1, namespace)
    state_mgr = StateManager(core_v1, namespace)
    git_poller = GitPoller()
    github_poller = GitHubPoller(token=os.environ.get("GITHUB_API_TOKEN"))
    runner = PipelineRunner(custom_api, namespace)

    log.info("GitOps polling controller started. Namespace: %s", namespace)

    while True:
        try:
            watch_config = cfg.load()
            interval = watch_config.poll_interval_seconds
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to load watch config: %s — retrying in 60s", exc)
            time.sleep(60)
            continue

        log.info(
            "Reconciling %d repo(s) (poll_interval=%ds)…",
            len(watch_config.repositories),
            interval,
        )
        try:
            reconcile(cfg, state_mgr, git_poller, github_poller, runner)
        except Exception as exc:  # noqa: BLE001
            log.error("Reconcile loop error: %s", exc, exc_info=True)

        log.info("Sleeping %ds until next poll.", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
