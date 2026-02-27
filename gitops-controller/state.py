from __future__ import annotations

import json
import logging
import os

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

log = logging.getLogger(__name__)

_DEFAULT_STATE: dict = {"branches": {}, "prs": {}}


class StateManager:
    """Persists the last-seen commit SHAs in a Kubernetes ConfigMap.

    State structure (stored as JSON under key ``state.json``):
    ::

        {
            "branches": { "url#branch": "sha..." },
            "prs":      { "url#PR-42": "sha..." }
        }
    """

    def __init__(self, core_v1, namespace: str):
        self._core_v1 = core_v1
        self._namespace = namespace
        self._name = os.environ.get("STATE_CONFIG_MAP", "gitops-controller-state")

    def load(self) -> dict:
        try:
            cm = self._core_v1.read_namespaced_config_map(
                name=self._name,
                namespace=self._namespace,
            )
            raw = cm.data.get("state.json", "{}")
            state = json.loads(raw)
            state.setdefault("branches", {})
            state.setdefault("prs", {})
            return state
        except ApiException as exc:
            if exc.status == 404:
                log.info("State ConfigMap not found — starting with empty state.")
                return dict(_DEFAULT_STATE)
            raise

    def save(self, state: dict) -> None:
        payload = json.dumps(state)
        body = k8s_client.V1ConfigMap(
            metadata=k8s_client.V1ObjectMeta(
                name=self._name,
                namespace=self._namespace,
                labels={"app.kubernetes.io/name": "gitops-controller"},
            ),
            data={"state.json": payload},
        )
        try:
            self._core_v1.replace_namespaced_config_map(
                name=self._name,
                namespace=self._namespace,
                body=body,
            )
            log.debug("State ConfigMap updated.")
        except ApiException as exc:
            if exc.status == 404:
                self._core_v1.create_namespaced_config_map(
                    namespace=self._namespace,
                    body=body,
                )
                log.info("State ConfigMap created.")
            else:
                raise
