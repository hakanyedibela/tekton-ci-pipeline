#!/usr/bin/env python3
"""Tekton GitOps CI/CD installer CLI.

Usage:
    python install.py install   [--namespace NS] [--tekton-version VER] [--skip-dashboard]
    python install.py uninstall [--namespace NS] [--delete-pvcs]
    python install.py status    [--namespace NS]
    python install.py add-repo  --url URL --branch BRANCH --image-name NAME
                                --image-registry REG --gitops-repo URL
                                [--namespace NS] [--watch-prs]
                                [--github-owner ORG] [--github-repo REPO]
    python install.py remove-repo --url URL --branch BRANCH [--namespace NS]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any

import yaml  # pip install pyyaml

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"{_GREEN}✓{_RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"{_RED}✗{_RESET}  {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"{_YELLOW}⚠{_RESET}  {msg}")


def header(msg: str) -> None:
    print(f"\n{_BOLD}{msg}{_RESET}")


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture,
        text=True,
    )


def kubectl(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return _run(["kubectl", *args], check=check, capture=capture)


def kubectl_out(*args: str) -> str:
    result = kubectl(*args, capture=True, check=False)
    return result.stdout.strip()


def kubectl_apply(path: str, namespace: str) -> None:
    kubectl("apply", "-n", namespace, "-f", path)


def wait_for_deployment(name: str, namespace: str, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = kubectl_out(
            "get", "deployment", name, "-n", namespace,
            "-o", "jsonpath={.status.readyReplicas}",
        )
        if ready and int(ready) >= 1:
            return True
        time.sleep(5)
    return False


def _get_configmap_data(name: str, namespace: str) -> dict[str, str]:
    raw = kubectl_out("get", "configmap", name, "-n", namespace, "-o", "json")
    if not raw:
        return {}
    return json.loads(raw).get("data", {})


def _patch_configmap(name: str, namespace: str, data: dict[str, str]) -> None:
    patch = json.dumps({"data": data})
    kubectl("patch", "configmap", name, "-n", namespace, "--type=merge", "-p", patch)


# ---------------------------------------------------------------------------
# Subcommand: install
# ---------------------------------------------------------------------------

TEKTON_PIPELINE_BASE = "https://storage.googleapis.com/tekton-releases/pipeline/latest/release.yaml"
TEKTON_DASHBOARD_BASE = "https://storage.googleapis.com/tekton-releases/dashboard/latest/release.yaml"


def cmd_install(args: argparse.Namespace) -> None:
    ns = args.namespace
    tekton_ver = args.tekton_version
    skip_dash = args.skip_dashboard

    header("Pre-flight checks")
    try:
        kubectl("version", "--client", capture=True)
        ok("kubectl found")
    except FileNotFoundError:
        fail("kubectl not found — install it and ensure it is on PATH.")
        sys.exit(1)

    result = kubectl("get", "nodes", capture=True, check=False)
    if result.returncode != 0:
        fail("Cannot reach cluster — check KUBECONFIG or cluster connectivity.")
        sys.exit(1)
    ok("Cluster reachable")

    sc = kubectl_out("get", "storageclass", "-o", "jsonpath={.items[0].metadata.name}")
    if sc:
        ok(f"Default StorageClass: {sc}")
    else:
        warn("No StorageClass found — PVCs may not provision automatically.")

    header("Installing Tekton Pipelines")
    release_url = (
        f"https://storage.googleapis.com/tekton-releases/pipeline/previous/{tekton_ver}/release.yaml"
        if tekton_ver
        else TEKTON_PIPELINE_BASE
    )
    kubectl("apply", "--filename", release_url)
    ok("Tekton Pipelines manifests applied")

    print("  Waiting for tekton-pipelines-controller…", end="", flush=True)
    if wait_for_deployment("tekton-pipelines-controller", "tekton-pipelines"):
        print(f" {_GREEN}ready{_RESET}")
    else:
        print(f" {_YELLOW}timed out (check manually){_RESET}")

    if not skip_dash:
        header("Installing Tekton Dashboard")
        kubectl("apply", "--filename", TEKTON_DASHBOARD_BASE)
        ok("Tekton Dashboard manifests applied")

    header("Applying RBAC")
    kubectl_apply("rbac/serviceaccount.yaml", ns)
    ok("RBAC applied")

    header("Applying PVCs")
    kubectl_apply("workspaces/pvcs.yaml", ns)
    ok("PVCs applied")

    header("Applying Tasks")
    kubectl("apply", "-n", ns, "-f", "tasks/")
    ok("Tasks applied")

    header("Applying Pipelines")
    kubectl("apply", "-n", ns, "-f", "pipelines/")
    ok("Pipelines applied")

    header("Applying GitOps controller config")
    kubectl_apply("gitops-controller/watch-config.yaml", ns)
    ok("Watch ConfigMap applied")

    kubectl_apply("gitops-controller/deployment.yaml", ns)
    ok("Controller Deployment applied")

    print("  Waiting for gitops-controller…", end="", flush=True)
    if wait_for_deployment("gitops-controller", ns):
        print(f" {_GREEN}ready{_RESET}")
    else:
        print(f" {_YELLOW}timed out (check manually){_RESET}")

    header("Next steps")
    print(
        """
  1. Build & push the controller image:
       docker build -t ghcr.io/<yourorg>/tekton-gitops-controller:latest gitops-controller/
       docker push ghcr.io/<yourorg>/tekton-gitops-controller:latest

  2. Add required secrets (registry, gitops git credentials, GitHub PAT):
       kubectl apply -n {ns} -f workspaces/secrets.yaml   # then edit values

  3. Add your first repository:
       python install.py add-repo \\
         --url https://github.com/myorg/my-service.git \\
         --branch main \\
         --image-name my-service \\
         --image-registry harbor.example.com/myteam \\
         --gitops-repo https://github.com/myorg/gitops-repo.git \\
         --namespace {ns}

  4. Watch controller logs:
       kubectl logs -n {ns} -l app.kubernetes.io/name=gitops-controller -f
""".format(ns=ns)
    )


# ---------------------------------------------------------------------------
# Subcommand: uninstall
# ---------------------------------------------------------------------------

def cmd_uninstall(args: argparse.Namespace) -> None:
    ns = args.namespace
    header("Uninstalling GitOps controller")
    kubectl("delete", "-n", ns, "-f", "gitops-controller/deployment.yaml", check=False)
    kubectl("delete", "-n", ns, "configmap", "gitops-watch-config", check=False)
    kubectl("delete", "-n", ns, "configmap", "gitops-controller-state", check=False)
    ok("Controller resources removed")

    header("Removing Tasks and Pipelines")
    kubectl("delete", "-n", ns, "-f", "pipelines/", check=False)
    kubectl("delete", "-n", ns, "-f", "tasks/", check=False)
    ok("Tasks / Pipelines removed")

    header("Removing RBAC")
    kubectl("delete", "-n", ns, "-f", "rbac/serviceaccount.yaml", check=False)
    ok("RBAC removed")

    if args.delete_pvcs:
        header("Deleting PVCs")
        kubectl("delete", "-n", ns, "-f", "workspaces/pvcs.yaml", check=False)
        ok("PVCs deleted")
    else:
        warn("PVCs not deleted (pass --delete-pvcs to remove them).")

    ok("Uninstall complete.")


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> None:
    ns = args.namespace
    header("Tekton controller status")
    result = kubectl_out(
        "get", "pods", "-n", "tekton-pipelines",
        "-l", "app=tekton-pipelines-controller",
        "--no-headers",
    )
    print(f"  {result}" if result else "  (no pods found)")

    header("GitOps controller status")
    result = kubectl_out(
        "get", "pods", "-n", ns,
        "-l", "app.kubernetes.io/name=gitops-controller",
        "--no-headers",
    )
    print(f"  {result}" if result else "  (no pods found)")

    header("Watched repositories (from ConfigMap)")
    data = _get_configmap_data("gitops-watch-config", ns)
    if "config.yaml" in data:
        cfg = yaml.safe_load(data["config.yaml"])
        repos = cfg.get("repositories", [])
        if repos:
            for r in repos:
                print(f"  • {r['url']}  branch={r.get('branch','main')}  watch_prs={r.get('watch_prs', False)}")
        else:
            warn("No repositories configured.")
    else:
        warn("Watch ConfigMap not found.")

    header("Last processed SHAs (from state ConfigMap)")
    state_data = _get_configmap_data("gitops-controller-state", ns)
    if "state.json" in state_data:
        state = json.loads(state_data["state.json"])
        branches = state.get("branches", {})
        prs = state.get("prs", {})
        if branches:
            for k, sha in branches.items():
                print(f"  branch  {k} = {sha[:12]}")
        if prs:
            for k, sha in prs.items():
                print(f"  pr      {k} = {sha[:12]}")
        if not branches and not prs:
            print("  (no SHAs recorded yet)")
    else:
        warn("State ConfigMap not found — controller may not have run yet.")

    header("Last 5 PipelineRuns")
    result = kubectl_out(
        "get", "pipelineruns", "-n", ns,
        "--sort-by=.metadata.creationTimestamp",
        "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.conditions[0].reason,AGE:.metadata.creationTimestamp",
        "--no-headers",
    )
    lines = result.splitlines() if result else []
    for line in lines[-5:]:
        print(f"  {line}")
    if not lines:
        print("  (no PipelineRuns found)")


# ---------------------------------------------------------------------------
# Subcommand: add-repo
# ---------------------------------------------------------------------------

def cmd_add_repo(args: argparse.Namespace) -> None:
    ns = args.namespace
    data = _get_configmap_data("gitops-watch-config", ns)
    if "config.yaml" not in data:
        fail("Watch ConfigMap 'gitops-watch-config' not found. Run 'install' first.")
        sys.exit(1)

    cfg: dict[str, Any] = yaml.safe_load(data["config.yaml"])
    repos: list[dict] = cfg.setdefault("repositories", [])

    # Dedup check
    for r in repos:
        if r["url"] == args.url and r.get("branch", "main") == args.branch:
            warn(f"Repository {args.url}@{args.branch} is already configured.")
            return

    entry: dict[str, Any] = {
        "url": args.url,
        "branch": args.branch,
        "image_name": args.image_name,
        "image_registry": args.image_registry,
        "gitops_repo_url": args.gitops_repo,
        "gitops_values_path": args.gitops_values_path or f"apps/{args.image_name}/values.yaml",
        "registry_secret": args.registry_secret,
        "gitops_secret": args.gitops_secret,
        "watch_prs": args.watch_prs,
        "github_owner": args.github_owner or "",
        "github_repo": args.github_repo or args.image_name,
    }
    repos.append(entry)
    cfg["repositories"] = repos
    data["config.yaml"] = yaml.dump(cfg, default_flow_style=False)
    _patch_configmap("gitops-watch-config", ns, data)
    ok(f"Added repository {args.url}@{args.branch} to watch config.")
    print("  The controller will pick up the change on the next poll cycle.")


# ---------------------------------------------------------------------------
# Subcommand: remove-repo
# ---------------------------------------------------------------------------

def cmd_remove_repo(args: argparse.Namespace) -> None:
    ns = args.namespace
    data = _get_configmap_data("gitops-watch-config", ns)
    if "config.yaml" not in data:
        fail("Watch ConfigMap 'gitops-watch-config' not found.")
        sys.exit(1)

    cfg: dict[str, Any] = yaml.safe_load(data["config.yaml"])
    repos: list[dict] = cfg.get("repositories", [])
    before = len(repos)
    repos = [
        r for r in repos
        if not (r["url"] == args.url and r.get("branch", "main") == args.branch)
    ]
    if len(repos) == before:
        warn(f"No entry found for {args.url}@{args.branch}.")
        return

    cfg["repositories"] = repos
    data["config.yaml"] = yaml.dump(cfg, default_flow_style=False)
    _patch_configmap("gitops-watch-config", ns, data)
    ok(f"Removed repository {args.url}@{args.branch} from watch config.")

    # Also clear state for this repo
    state_data = _get_configmap_data("gitops-controller-state", ns)
    if "state.json" in state_data:
        state = json.loads(state_data["state.json"])
        prefix = f"{args.url}#{args.branch}"
        state["branches"] = {k: v for k, v in state.get("branches", {}).items() if not k.startswith(args.url)}
        state["prs"] = {k: v for k, v in state.get("prs", {}).items() if not k.startswith(args.url)}
        state_data["state.json"] = json.dumps(state)
        _patch_configmap("gitops-controller-state", ns, state_data)
        ok("Cleared state entries for removed repository.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Tekton GitOps CI/CD installer",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- install --
    p_install = sub.add_parser("install", help="Install Tekton + GitOps controller")
    p_install.add_argument("--namespace", default="default", metavar="NS")
    p_install.add_argument("--tekton-version", default=None, metavar="VER",
                           help="Pin a specific Tekton Pipelines release (e.g. v0.59.0).")
    p_install.add_argument("--skip-dashboard", action="store_true",
                           help="Skip installing Tekton Dashboard.")

    # -- uninstall --
    p_uninst = sub.add_parser("uninstall", help="Remove all installed resources")
    p_uninst.add_argument("--namespace", default="default", metavar="NS")
    p_uninst.add_argument("--delete-pvcs", action="store_true",
                          help="Also delete PersistentVolumeClaims.")

    # -- status --
    p_status = sub.add_parser("status", help="Show current cluster status")
    p_status.add_argument("--namespace", default="default", metavar="NS")

    # -- add-repo --
    p_add = sub.add_parser("add-repo", help="Add a repository to the watch config")
    p_add.add_argument("--url", required=True, help="Git repository URL")
    p_add.add_argument("--branch", required=True, help="Branch to watch")
    p_add.add_argument("--image-name", required=True, help="Short image name (e.g. my-service)")
    p_add.add_argument("--image-registry", required=True, help="Registry prefix (e.g. harbor.example.com/myteam)")
    p_add.add_argument("--gitops-repo", required=True, metavar="URL", help="GitOps repository URL")
    p_add.add_argument("--gitops-values-path", default=None, help="Path to values.yaml in GitOps repo")
    p_add.add_argument("--registry-secret", default="harbor-registry-secret")
    p_add.add_argument("--gitops-secret", default="gitops-git-credentials")
    p_add.add_argument("--watch-prs", action="store_true", help="Also poll open PRs via GitHub API")
    p_add.add_argument("--github-owner", default=None, help="GitHub org/user (required for PR polling)")
    p_add.add_argument("--github-repo", default=None, help="GitHub repo name (defaults to --image-name)")
    p_add.add_argument("--namespace", default="default", metavar="NS")

    # -- remove-repo --
    p_rm = sub.add_parser("remove-repo", help="Remove a repository from the watch config")
    p_rm.add_argument("--url", required=True, help="Git repository URL to remove")
    p_rm.add_argument("--branch", required=True, help="Branch to remove")
    p_rm.add_argument("--namespace", default="default", metavar="NS")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "add-repo": cmd_add_repo,
        "remove-repo": cmd_remove_repo,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
