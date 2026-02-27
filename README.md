# Tekton CI/CD Pipeline for Java Microservices

A production-ready, end-to-end CI/CD pipeline built on [Tekton](https://tekton.dev/) for Java Spring Boot microservices. This project implements the full software delivery lifecycle — from a new commit detected by a GitOps polling controller to a signed, vulnerability-scanned container image deployed via GitOps — entirely inside Kubernetes, with no inbound webhooks required.

---

## Table of Contents

- [What Is This Project?](#what-is-this-project)
- [Why Tekton?](#why-tekton)
- [Architecture Overview](#architecture-overview)
- [Pipeline Stages](#pipeline-stages)
- [Project Structure](#project-structure)
- [Components In Detail](#components-in-detail)
  - [Tasks](#tasks)
  - [Pipelines](#pipelines)
  - [GitOps Polling Controller](#gitops-polling-controller)
  - [RBAC](#rbac)
  - [Workspaces](#workspaces)
  - [Supply Chain Security (Tekton Chains)](#supply-chain-security-tekton-chains)
  - [Monitoring](#monitoring)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration Reference](#configuration-reference)
- [Secrets Setup](#secrets-setup)
- [Adding and Removing Repositories](#adding-and-removing-repositories)
- [Running the Pipeline Manually](#running-the-pipeline-manually)
- [Viewing Controller Logs](#viewing-controller-logs)
- [Key Design Decisions](#key-design-decisions)
- [Monitoring & Alerting](#monitoring--alerting)
- [Troubleshooting](#troubleshooting)
- [Useful Commands](#useful-commands)

---

## What Is This Project?

This repository provides a complete, batteries-included Tekton CI/CD pipeline configuration for teams building Java microservices on Kubernetes. It solves the common challenge of setting up a secure, auditable, and automated delivery pipeline without inbound webhooks or hosted CI services.

**What it does, end to end:**

1. A **GitOps polling controller** runs inside the cluster and periodically calls `git ls-remote` on each watched repository.
2. When a new commit is detected on a watched branch, the controller automatically creates a **PipelineRun**.
3. When an open pull request is updated, a lightweight **PR CI PipelineRun** is created.
4. The PipelineRun executes the following stages:
   - Clone the source repository
   - Compile the Java project and run unit tests with Maven
   - Build a container image using **Kaniko** (no Docker socket, no privileged containers)
   - Scan the image for vulnerabilities with **Trivy** (pipeline fails on HIGH/CRITICAL CVEs)
   - Update the **GitOps repository** with the new image tag (triggers ArgoCD to deploy)
5. Regardless of success or failure, a **Slack notification** is sent.
6. **Tekton Chains** automatically signs the container image and generates SLSA provenance, submitting it to the Sigstore Rekor transparency log.

**What it is good for:**

- Teams running Kubernetes who want CI/CD *inside* the cluster with no inbound network exposure.
- Organizations with strict security requirements — no Docker socket, no privileged pods, signed images, supply chain provenance.
- Platform engineers setting up a golden path for development teams.
- Learning how Tekton Tasks, Pipelines, and a custom controller fit together in a realistic production setup.

---

## Why Tekton?

| Concern | What Tekton Offers |
|---|---|
| **Kubernetes-native** | All CI/CD resources are CRDs; `kubectl` and RBAC apply uniformly |
| **No server to maintain** | No Jenkins controller, no GitLab runner — just Kubernetes controllers |
| **Isolation** | Every pipeline run gets its own pods; no shared agents |
| **Auditability** | PipelineRuns and TaskRuns are Kubernetes objects — inspectable, queryable, versioned |
| **Supply chain** | Tekton Chains adds automatic signing and SLSA provenance with zero code changes |
| **Composability** | Tasks are reusable building blocks; pipelines are DAGs of tasks |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  GitOps Polling Controller (runs in cluster)                         │
│                                                                       │
│  Every poll_interval_seconds:                                         │
│    git ls-remote <repo> refs/heads/<branch>  ──►  new SHA?           │
│    GitHub REST API /repos/{owner}/{repo}/pulls  ──►  new PR SHA?     │
└────────────────────────────┬────────────────────────────────────────┘
                             │  creates PipelineRun (Tekton CRD)
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  PipelineRun: java-microservice-ci-cd  (branch push)               │
│                                                                      │
│  ① clone-repo  ──►  ② maven-build  ──►  ③ build-image             │
│     (git-clone)       (Maven Task)        (Kaniko Task)            │
│                                                  │                  │
│                                           ④ scan-image             │
│                                            (Trivy Task)            │
│                                                  │                  │
│                                      [only on main + PASS]         │
│                                           ⑤ update-gitops          │
│                                            (yq + git push)         │
│                                                  │                  │
│                                    ─── ArgoCD detects ─►           │
│                                                                      │
│  [finally]  ⑥ notify-slack   (always runs — success AND failure)   │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│  PipelineRun: java-microservice-pr-ci  (pull request)              │
│                                                                      │
│  ① clone-repo  ──►  ② maven-build  ──►  ③ trivy-scan (filesystem) │
│     (git-clone)       (Maven Task)        (no image build/push)    │
└────────────────────────────────────────────────────────────────────┘

                         │ (post-pipeline, automatic)
                         ▼
┌────────────────────────────────────────────────────────────────────┐
│  Tekton Chains                                                       │
│  - Signs container image with cosign                                 │
│  - Generates SLSA provenance (slsa/v1)                              │
│  - Submits to Rekor transparency log (sigstore.dev)                 │
└────────────────────────────────────────────────────────────────────┘
```

---

## Pipeline Stages

### Stage 1 — Clone Repository (`clone-repo`)

Clones the application source repository using a shallow (`--depth 1`) clone for speed. Outputs the exact commit SHA, repository URL, and committer timestamp as Tekton results, which are used by downstream tasks for immutable image tagging.

### Stage 2 — Maven Build & Test (`maven-build`)

Compiles the Java project and runs all unit tests using Maven. By default runs `clean verify`, which enforces that tests must pass for the pipeline to continue. A persistent PVC (`maven-cache-pvc`) is mounted as the local Maven repository, so dependencies downloaded in one run are available in subsequent runs — reducing build times by 3–5x on warm caches.

### Stage 3 — Container Image Build (`build-image`)

Builds the Docker image using **Kaniko** (`gcr.io/kaniko-project/executor`). Kaniko builds images from a `Dockerfile` entirely in userspace — it does not require the Docker daemon, a Docker socket mount, or privileged containers. The image is tagged with the exact git commit SHA for immutability and traceability.

### Stage 4 — Vulnerability Scan (`scan-image`)

Scans the freshly built image for known CVEs using **Aqua Security Trivy**. The pipeline fails on `HIGH` and `CRITICAL` severity vulnerabilities that have a known fix available. The scan result (`PASS` or `FAIL`) gates the GitOps update step.

### Stage 5 — GitOps Repository Update (`update-gitops`) — Conditional

This stage only executes if both conditions are met: the Trivy scan returned `PASS` and the git branch is `main` or `master`. It clones the GitOps repository, uses `yq` to update the `.image.tag` field in the target Helm `values.yaml`, commits, and pushes. ArgoCD detects this commit and syncs the new image to the cluster automatically.

### Finally — Slack Notification (`notify-slack`)

Runs unconditionally after all other stages, whether the pipeline succeeded or failed. Sends a formatted Slack message via an Incoming Webhook URL, including the pipeline run name, final status, git commit SHA, and the image URL.

### PR Pipeline — `java-microservice-pr-ci`

A lightweight pipeline for pull requests: clone → Maven build → Trivy filesystem scan. No image build, no image push, no GitOps update, no Slack notification. Keeps PR feedback fast and avoids registry pollution.

---

## Project Structure

```
tekton-ci-pipeline/
│
├── tasks/                          # Individual reusable Tekton Task definitions
│   ├── 01-git-clone.yaml           #   Clone source repo (SSH + HTTPS, submodules)
│   ├── 02-maven-build.yaml         #   Maven compile, test, package (Java 21, Maven 3.9)
│   ├── 03-kaniko-build.yaml        #   Build + push OCI image without Docker socket
│   ├── 04-trivy-scan.yaml          #   CVE + misconfiguration scan; gates on severity
│   ├── 05-update-gitops-repo.yaml  #   Clone GitOps repo, update image tag, push
│   └── 06-slack-notify.yaml        #   Send Slack notification (success or failure)
│
├── pipelines/
│   ├── ci-cd-pipeline.yaml         # Main Pipeline resource (branch pushes)
│   ├── pr-pipeline.yaml            # Lightweight PR CI pipeline
│   └── pipeline-run-example.yaml   # Example PipelineRun for manual testing
│
├── gitops-controller/              # In-cluster polling controller
│   ├── controller.py               #   Main reconciliation loop
│   ├── config.py                   #   ConfigMap-backed watch configuration
│   ├── state.py                    #   Persists last-seen commit SHAs
│   ├── git_poller.py               #   git ls-remote branch polling
│   ├── github_poller.py            #   GitHub REST API PR polling
│   ├── pipeline_runner.py          #   Creates Tekton PipelineRuns
│   ├── requirements.txt            #   Python dependencies
│   ├── Dockerfile                  #   Container image for the controller
│   ├── watch-config.yaml           #   ConfigMap: repos + poll settings
│   └── deployment.yaml             #   ServiceAccount, Role, RoleBinding, Deployment
│
├── rbac/
│   └── serviceaccount.yaml         # ServiceAccounts, Roles, RoleBindings for all components
│
├── workspaces/
│   ├── pvcs.yaml                   # PersistentVolumeClaims (Maven cache, Gradle cache)
│   └── secrets.yaml                # Secret + ConfigMap templates (fill in before applying)
│
├── monitoring/
│   └── servicemonitor.yaml         # Prometheus ServiceMonitor + PrometheusRule (alerting)
│
├── chains/
│   └── tekton-chains-config.yaml   # Tekton Chains: SLSA provenance + cosign image signing
│
└── install.py                      # Python CLI installer (install/uninstall/status/add-repo/remove-repo)
```

---

## Components In Detail

### Tasks

Tasks are the atomic units of work in Tekton. Each task runs in its own pod and can have multiple steps (containers). All tasks in this project are self-contained and can be reused across different pipelines.

| Task | Image Used | Key Inputs | Key Outputs |
|---|---|---|---|
| `git-clone` | `cgr.dev/chainguard/git` | `url`, `revision` | `commit` SHA, `committer-date` |
| `maven-build` | `maven:3.9-eclipse-temurin-21` | `GOALS`, `MAVEN_OPTS` | `artifact-version`, `artifact-id` |
| `kaniko-build` | `gcr.io/kaniko-project/executor:v1.21.0` | `IMAGE`, `DOCKERFILE` | `IMAGE_URL`, `IMAGE_DIGEST` |
| `trivy-scan` | `aquasec/trivy:0.50.0` | `IMAGE` or `SCAN_TARGET`, `SEVERITY` | `SCAN_RESULT` (PASS/FAIL) |
| `update-gitops-repo` | `alpine/git`, `mikefarah/yq:4.40.5` | `GITOPS_REPO_URL`, `IMAGE_TAG` | `GITOPS_COMMIT_SHA` |
| `slack-notify` | `curlimages/curl:8.5.0` | `PIPELINE_STATUS`, `IMAGE_URL` | _(none)_ |

### Pipelines

**`java-microservice-ci-cd`** — Full CI/CD pipeline for branch pushes (main):
- Workspaces: `source`, `maven-cache`, `docker-credentials`, `gitops-credentials`, `maven-settings`
- Results: `commit-sha`, `image-url`, `image-digest`
- Conditional `update-gitops` step (main branch + scan PASS)
- `notify-slack` in `finally` block (always runs)
- Timeout: 1 hour

**`java-microservice-pr-ci`** — Lightweight PR validation:
- Tasks: `clone-repo` → `maven-build` → `trivy-scan` (filesystem, not image)
- Workspaces: `source`, `maven-cache`
- No image build, no push, no GitOps update, no Slack
- Timeout: 30 minutes

### GitOps Polling Controller

The controller runs as a Kubernetes Deployment (`gitops-controller`) and periodically reconciles the desired state:

**How it works:**
1. Reads `gitops-watch-config` ConfigMap to get the list of repositories and `poll_interval_seconds`.
2. For each repository, calls `git ls-remote <url> refs/heads/<branch>` (no clone needed).
3. If the returned SHA differs from the last-seen SHA (stored in `gitops-controller-state` ConfigMap), creates a branch `PipelineRun`.
4. If `watch_prs: true`, calls the GitHub REST API to list open PRs. For each PR with a new head SHA, creates a PR `PipelineRun`.
5. Saves updated SHAs and sleeps until the next poll interval.

**Python modules:**

| Module | Responsibility |
|---|---|
| `controller.py` | Main loop, orchestrates all components |
| `config.py` | Loads `WatchConfig` and `RepoConfig` from ConfigMap |
| `state.py` | Read/write last-seen SHAs in a state ConfigMap |
| `git_poller.py` | `git ls-remote` wrapper with 30s timeout |
| `github_poller.py` | GitHub REST API `/pulls?state=open` with pagination |
| `pipeline_runner.py` | Creates `PipelineRun` CRDs via Kubernetes Python client |

**Building the controller image:**
```bash
docker build -t ghcr.io/<yourorg>/tekton-gitops-controller:latest gitops-controller/
docker push ghcr.io/<yourorg>/tekton-gitops-controller:latest
```

### RBAC

Three ServiceAccounts are used:

| ServiceAccount | Purpose | Key Permissions |
|---|---|---|
| `tekton-pipeline-sa` | Runs pipeline task pods | Read Secrets/ConfigMaps, manage PVCs, read Tekton resources |
| `tekton-triggers-sa` | Legacy triggers (retained for compatibility) | Create PipelineRuns, read Trigger resources |
| `gitops-controller-sa` | GitOps polling controller | Get/create/update ConfigMaps, read Secrets, create PipelineRuns |

All permissions are namespace-scoped `Role`/`RoleBinding`.

### Workspaces

**PersistentVolumeClaims** (`pvcs.yaml`)

| PVC | Size | Purpose |
|---|---|---|
| `maven-cache-pvc` | 5 Gi | Maven local repository, reused across all pipeline runs |
| `gradle-cache-pvc` | 5 Gi | Gradle cache, for Gradle-based projects (optional) |

For the source workspace, each PipelineRun uses a `volumeClaimTemplate` — an ephemeral PVC provisioned at run start and deleted at run end.

**Secrets** (`secrets.yaml`)

| Secret | Type | Purpose |
|---|---|---|
| `github-api-token` | Opaque | GitHub PAT for PR polling via REST API |
| `harbor-registry-secret` | `kubernetes.io/dockerconfigjson` | Docker registry auth for pushing images |
| `gitops-git-credentials` | Opaque | `.gitconfig` + `.git-credentials` for GitOps repo push |
| `slack-webhook-secret` | Opaque | Slack Incoming Webhook URL for notifications |
| `maven-settings` (ConfigMap) | — | Custom `settings.xml` for Nexus/Artifactory proxy |

### Supply Chain Security (Tekton Chains)

Tekton Chains watches completed TaskRuns and automatically signs artifacts. This project includes `chains/tekton-chains-config.yaml` that configures:

- **Artifact signing**: OCI images signed with `cosign`; SLSA v1 provenance stored as TaskRun annotations.
- **Storage**: Signatures stored as OCI artifacts in the same registry.
- **Transparency log**: Signatures submitted to `rekor.sigstore.dev`.

Tekton Chains reads the `IMAGE_URL` and `IMAGE_DIGEST` results from the Kaniko task — no pipeline code changes required.

```bash
cosign verify harbor.example.com/myteam/my-service:abc1234ef \
  --certificate-identity=tekton@example.com \
  --certificate-oidc-issuer=https://accounts.google.com
```

### Monitoring

The `monitoring/servicemonitor.yaml` defines Prometheus ServiceMonitors and PrometheusRules for the Tekton controllers. See [Monitoring & Alerting](#monitoring--alerting) for details.

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Kubernetes | 1.27+ | Cluster where everything runs |
| `kubectl` | 1.27+ | Apply manifests |
| Python | 3.10+ | Run `install.py` CLI |
| `pyyaml` | 6.x | Install with `pip install pyyaml` |
| `tkn` | Latest | Tekton CLI for monitoring and debugging runs |
| ArgoCD | 2.x+ | GitOps operator watching the GitOps repo |
| Prometheus Operator | — | Required for monitoring (optional) |

```bash
brew install tektoncd-cli        # macOS
# or
curl -LO https://github.com/tektoncd/cli/releases/latest/download/tkn_Linux_x86_64.tar.gz
```

---

## Quick Start

### 1. Install Tekton + GitOps Controller

The `install.py` CLI installs Tekton Pipelines and the GitOps polling controller, then applies all resources in the correct order:

```bash
# Install with defaults (namespace: default, latest Tekton)
python install.py install

# Override namespace and pin Tekton version
python install.py install --namespace ci --tekton-version v0.59.0

# Skip Tekton Dashboard installation
python install.py install --skip-dashboard
```

The installer performs these steps:
1. Pre-flight checks: `kubectl` available, cluster reachable, StorageClass present
2. Install **Tekton Pipelines** and wait for the controller to be ready
3. Optionally install **Tekton Dashboard**
4. Apply **RBAC** (ServiceAccounts, Roles, RoleBindings)
5. Create **PVCs** (Maven cache)
6. Apply all **Tasks** and **Pipelines** (including the new PR pipeline)
7. Apply the watch ConfigMap and controller Deployment
8. Print next-steps

### 2. Build and Push the Controller Image

```bash
docker build -t ghcr.io/<yourorg>/tekton-gitops-controller:latest gitops-controller/
docker push ghcr.io/<yourorg>/tekton-gitops-controller:latest
```

Update the image reference in `gitops-controller/deployment.yaml` if needed, then:
```bash
kubectl apply -n default -f gitops-controller/deployment.yaml
```

### 3. Configure Secrets

Edit `workspaces/secrets.yaml` and replace all `REPLACE_WITH_*` placeholders, then apply:

```bash
kubectl apply -f workspaces/secrets.yaml -n default
```

Or create the secrets directly:

```bash
# GitHub PAT for PR polling (repo:read scope is sufficient for public repos)
kubectl create secret generic github-api-token \
  --from-literal=token=ghp_YOUR_GITHUB_PAT

# Harbor registry credentials
kubectl create secret docker-registry harbor-registry-secret \
  --docker-server=harbor.example.com \
  --docker-username=robot-tekton \
  --docker-password=YOUR_HARBOR_TOKEN

# GitOps repository credentials
kubectl create secret generic gitops-git-credentials \
  --from-literal=.gitconfig='[credential]
  helper = store
[user]
  email = tekton-ci@example.com
  name = Tekton CI' \
  --from-literal=.git-credentials='https://tekton-bot:YOUR_GITHUB_PAT@github.com'

# Slack webhook
kubectl create secret generic slack-webhook-secret \
  --from-literal=webhook-url=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

### 4. Add Your First Repository

```bash
python install.py add-repo \
  --url https://github.com/myorg/my-service.git \
  --branch main \
  --image-name my-service \
  --image-registry harbor.example.com/myteam \
  --gitops-repo https://github.com/myorg/gitops-repo.git \
  --watch-prs \
  --github-owner myorg \
  --namespace default
```

The controller picks up the new repository on its next poll cycle (within `poll_interval_seconds`).

---

## Configuration Reference

### watch-config.yaml

The `gitops-watch-config` ConfigMap controls all polling behaviour:

```yaml
poll_interval_seconds: 60          # How often to poll (default: 60s)
github_api_token_secret: github-api-token

repositories:
  - url: https://github.com/myorg/my-service.git
    branch: main
    image_name: my-service
    image_registry: harbor.example.com/myteam
    gitops_repo_url: https://github.com/myorg/gitops-repo.git
    gitops_values_path: apps/my-service/values.yaml
    registry_secret: harbor-registry-secret
    gitops_secret: gitops-git-credentials
    watch_prs: true
    github_owner: myorg
    github_repo: my-service
```

### Pipeline Parameters

| Parameter | Default | Description |
|---|---|---|
| `git-url` | _(required)_ | Source repository URL |
| `git-revision` | `main` | Branch, tag, or commit SHA to build |
| `image-registry` | _(required)_ | Container registry prefix |
| `image-name` | _(required)_ | Image name without registry |
| `gitops-repo-url` | _(required)_ | GitOps repository URL |
| `gitops-values-path` | `apps/<image-name>/values.yaml` | Path to `values.yaml` in GitOps repo |
| `deploy-to-prod` | `false` | Set `true` to allow production deployment |
| `maven-goals` | `["clean", "verify", ...]` | Maven goals to execute |

---

## Secrets Setup

| Secret Name | Key | Where Used |
|---|---|---|
| `github-api-token` | `token` | GitOps controller PR polling |
| `harbor-registry-secret` | `.dockerconfigjson` | Kaniko push to registry |
| `gitops-git-credentials` | `.gitconfig`, `.git-credentials` | GitOps repo push |
| `slack-webhook-secret` | `webhook-url` | Slack notification task |

For cloud-native registry auth without static credentials:
- **AWS EKS**: Annotate `tekton-pipeline-sa` with an IRSA role ARN that has ECR push permissions.
- **GKE**: Annotate with a Workload Identity GCP service account bound to Artifact Registry.

---

## Adding and Removing Repositories

**Add a repository to the watch list:**
```bash
python install.py add-repo \
  --url https://github.com/myorg/another-service.git \
  --branch main \
  --image-name another-service \
  --image-registry harbor.example.com/myteam \
  --gitops-repo https://github.com/myorg/gitops-repo.git \
  --watch-prs \
  --github-owner myorg \
  --namespace default
```

**Remove a repository:**
```bash
python install.py remove-repo \
  --url https://github.com/myorg/another-service.git \
  --branch main \
  --namespace default
```

This removes the repo from the watch ConfigMap and clears its state entries so the controller no longer polls it.

**Check what is currently watched:**
```bash
python install.py status --namespace default
```

---

## Running the Pipeline Manually

```bash
tkn pipeline start java-microservice-ci-cd \
  --param git-url=https://github.com/myorg/my-service.git \
  --param git-revision=main \
  --param image-registry=harbor.example.com/myteam \
  --param image-name=my-service \
  --param gitops-repo-url=https://github.com/myorg/gitops-repo.git \
  --workspace name=source,volumeClaimTemplateFile=workspaces/pvcs.yaml \
  --workspace name=maven-cache,claimName=maven-cache-pvc \
  --workspace name=docker-credentials,secret=harbor-registry-secret \
  --workspace name=gitops-credentials,secret=gitops-git-credentials \
  --showlog
```

Or apply the example PipelineRun directly (after editing the values):
```bash
kubectl apply -f pipelines/pipeline-run-example.yaml
tkn pipelinerun logs java-microservice-run-001 -f
```

---

## Viewing Controller Logs

```bash
# Stream controller logs
kubectl logs -n default -l app.kubernetes.io/name=gitops-controller -f

# Example log output:
# 2026-02-27T10:00:00 INFO     controller  Reconciling 2 repo(s) (poll_interval=60s)…
# 2026-02-27T10:00:01 INFO     controller  Created branch PipelineRun: branch-my-service-abc1234-1709028001
# 2026-02-27T10:00:01 INFO     controller  Sleeping 60s until next poll.
```

The controller stores the last-seen SHAs in the `gitops-controller-state` ConfigMap:
```bash
kubectl get configmap gitops-controller-state -o jsonpath='{.data.state\.json}' | python3 -m json.tool
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| **Trigger mechanism** | GitOps polling (no webhooks) | No inbound network exposure; works in air-gapped or firewalled clusters |
| **Branch polling** | `git ls-remote` | Lightweight — no clone needed; works with any git hosting |
| **PR detection** | GitHub REST API | Reliable; avoids webhook event loss on restart |
| **State storage** | Kubernetes ConfigMap (JSON) | No external database; survives pod restarts; inspectable with kubectl |
| **Image builder** | Kaniko | No Docker daemon, no privileged containers — safe for multi-tenant clusters |
| **Image tagging** | Git commit SHA | Immutable and fully traceable; eliminates `latest` anti-pattern |
| **Security scanning** | Trivy with pipeline gate | Fast, comprehensive CVE database; blocks deployments of vulnerable images |
| **Supply chain** | Tekton Chains | Automatic signing and SLSA provenance with zero pipeline code changes |
| **Deployment strategy** | GitOps via ArgoCD | Clean separation of CI and CD; ArgoCD is the single source of truth |
| **Build cache** | Persistent PVC for Maven repo | 3–5x faster builds by reusing downloaded dependencies across runs |
| **Source workspace** | `volumeClaimTemplate` per run | Isolated storage per PipelineRun; no data leakage between concurrent runs |
| **Notifications** | Tekton `finally` tasks | Guaranteed to run on success AND failure — no missed notifications |
| **RBAC** | Separate ServiceAccounts | Least-privilege: controller SA only creates runs; pipeline SA only reads what it needs |

---

## Monitoring & Alerting

After applying `monitoring/servicemonitor.yaml`, Prometheus will scrape these key metrics:

| Metric | Description |
|---|---|
| `tekton_pipelines_controller_pipelinerun_count` | Total pipeline runs, labelled by status |
| `tekton_pipelines_controller_pipelinerun_duration_seconds` | Histogram of pipeline run durations |
| `tekton_pipelines_controller_running_pipelineruns` | Currently active pipeline runs |
| `workqueue_depth{name=~"tekton.*"}` | Controller queue depth |

**Pre-configured Alerts:**

| Alert | Threshold | Severity |
|---|---|---|
| `TektonHighPipelineFailureRate` | >10% failure rate over 15 min | Warning |
| `TektonPipelineLongRunning` | Average duration >1 hour | Warning |
| `TektonControllerQueueDepthHigh` | Queue depth >100 items | Critical |

```bash
kubectl port-forward -n tekton-pipelines svc/tekton-pipelines-controller 9090:9090
curl http://localhost:9090/metrics | grep tekton
```

---

## Troubleshooting

### Controller is not creating PipelineRuns

```bash
# Check controller pod is running
kubectl get pods -n default -l app.kubernetes.io/name=gitops-controller

# Stream logs
kubectl logs -n default -l app.kubernetes.io/name=gitops-controller -f

# Check watch config is valid
kubectl get configmap gitops-watch-config -o yaml

# Inspect current state (last-seen SHAs)
kubectl get configmap gitops-controller-state -o jsonpath='{.data.state\.json}' | python3 -m json.tool
```

Common causes:
- Repository not added to watch config (run `python install.py add-repo ...`)
- `git ls-remote` failing due to authentication (check if repo is private; configure credentials)
- GitHub API token missing or expired (check `github-api-token` Secret)
- Controller image not built/pushed yet

### Pipeline stuck or failing

```bash
# List recent pipeline runs
tkn pipelinerun list --limit 5

# Describe a specific run (shows task status and errors)
tkn pipelinerun describe <run-name>

# Stream logs of the latest run
tkn pipelinerun logs --last -f

# Describe a failing task run
tkn taskrun describe <taskrun-name>
```

### Tekton webhook admission errors

```
Internal error occurred: failed calling webhook "config.webhook.pipeline.tekton.dev": context deadline exceeded
```

```bash
kubectl rollout restart deployment/tekton-pipelines-webhook -n tekton-pipelines
kubectl wait --for=condition=ready pod \
  -l app=tekton-pipelines-webhook \
  -n tekton-pipelines \
  --timeout=120s
```

### Maven build fails with dependency download errors

```bash
kubectl get pvc maven-cache-pvc
# If Pending: check StorageClass
kubectl get storageclass

# If corrupted, recreate:
kubectl delete pvc maven-cache-pvc
kubectl apply -f workspaces/pvcs.yaml
```

### Trivy scan blocks the pipeline unexpectedly

```bash
docker run --rm aquasec/trivy:0.50.0 image \
  --severity HIGH,CRITICAL \
  --ignore-unfixed \
  harbor.example.com/myteam/my-service:<sha>
```

---

## Useful Commands

```bash
# --- CLI installer ---
python install.py install --namespace default
python install.py status --namespace default
python install.py add-repo --url https://github.com/myorg/svc.git --branch main \
  --image-name svc --image-registry harbor.example.com/myteam \
  --gitops-repo https://github.com/myorg/gitops.git
python install.py remove-repo --url https://github.com/myorg/svc.git --branch main
python install.py uninstall --namespace default

# --- Controller ---
kubectl logs -n default -l app.kubernetes.io/name=gitops-controller -f
kubectl get configmap gitops-controller-state -o jsonpath='{.data.state\.json}' | python3 -m json.tool

# --- Pipeline runs ---
tkn pipelinerun list                       # List all runs
tkn pipelinerun logs --last -f             # Stream logs of latest run
tkn pipelinerun describe <run-name>        # Inspect DAG status, results, params
tkn pipelinerun cancel <run-name>          # Cancel a stuck run
tkn pipelinerun delete --keep 10           # Clean up old runs (keep last 10)

# --- Task runs ---
tkn taskrun list                           # List all task runs
tkn taskrun logs <taskrun-name> -f         # Stream a specific task run's logs

# --- Inspect results ---
tkn pipelinerun describe <run-name> -o jsonpath='{.status.pipelineResults}'

# --- Tekton Chains (verify image signature) ---
cosign verify harbor.example.com/myteam/my-service:<sha> \
  --certificate-identity=tekton@example.com \
  --certificate-oidc-issuer=https://accounts.google.com

# --- Tekton Dashboard ---
kubectl port-forward -n tekton-pipelines svc/tekton-dashboard 9097:9097
# Open: http://localhost:9097
```
