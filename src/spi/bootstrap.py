# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-cluster bootstrap: namespaces, ConfigMaps, StorageClasses, Gateway API CRDs."""

import json

from .console import console, display_result, display_yaml
from .shell import kubectl_apply_yaml, kubectl_json, run_command, run_process
from .templates import (
    LEGAL_TAG_BASE,
    TESTER_NAMESPACE,
    parse_init_values,
    spi_init_values_configmap,
    storage_class,
)

STORAGE_CLASSES = ["pg-storageclass", "redis-storageclass", "es-storageclass"]
ISTIO_REVISION_CONFIGMAP = "spi-cluster-config"
ISTIO_REVISION_NAMESPACE = "osdu-flux"
ISTIO_REVISION_KEY = "ISTIO_REVISION"
# Fork RBAC substitutes the principal id; spi info publishes the rest.
DEPLOY_IDENTITY_KEYS = (
    "DEPLOY_IDENTITY_CLIENT_ID",
    "DEPLOY_IDENTITY_PRINCIPAL_ID",
    "MEMBER_IDENTITY_CLIENT_ID",
    "NO_ACCESS_IDENTITY_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_RESOURCE_GROUP",
    "AKS_CLUSTER_NAME",
)


def deploy_identity_facts(
    infra_outputs: dict, cluster_name: str, resource_group: str
) -> dict[str, str]:
    """The spi-cluster-config entries that describe the environment deploy identity."""
    return {
        "DEPLOY_IDENTITY_CLIENT_ID": infra_outputs.get("deploy_identity_client_id", ""),
        "DEPLOY_IDENTITY_PRINCIPAL_ID": infra_outputs.get("deploy_identity_principal_id", ""),
        "MEMBER_IDENTITY_CLIENT_ID": infra_outputs.get("member_identity_client_id", ""),
        "NO_ACCESS_IDENTITY_CLIENT_ID": infra_outputs.get("no_access_identity_client_id", ""),
        "AZURE_TENANT_ID": infra_outputs.get("tenant_id", ""),
        "AZURE_SUBSCRIPTION_ID": infra_outputs.get("subscription_id", ""),
        "AZURE_RESOURCE_GROUP": resource_group,
        "AKS_CLUSTER_NAME": cluster_name,
    }


class ClusterConfigError(RuntimeError):
    """The live spi-cluster-config could not be read, as opposed to not existing."""


def _read_configmap_data(name: str) -> dict[str, str]:
    """A ConfigMap's data in osdu-flux; empty when absent, an error on any other failure."""
    result = run_process(
        ["kubectl", "get", "configmap", name, "-n", ISTIO_REVISION_NAMESPACE]
        + ["--ignore-not-found", "-o", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "kubectl failed"
        raise ClusterConfigError(f"Could not read {name}: {detail}")
    if not result.stdout.strip():
        return {}
    return dict(json.loads(result.stdout).get("data", {}) or {})


def read_cluster_config() -> dict[str, str]:
    """Live spi-cluster-config data; empty when absent, an error on any other failure."""
    return _read_configmap_data(ISTIO_REVISION_CONFIGMAP)


def _detect_istio_revision() -> str | None:
    """Detect the installed Istio ASM revision from the cluster.

    The aks-istio-system namespace carries no ``istio.io/rev`` label, so the
    ``istiod-<revision>`` deployment name is the source. Returns ``None``
    when the query fails or no such deployment is visible.
    """
    data = kubectl_json(["get", "deploy", "-n", "aks-istio-system"])
    if data and data.get("items"):
        for item in data["items"]:
            name = item.get("metadata", {}).get("name", "")
            if name.startswith("istiod-"):
                return name.removeprefix("istiod-")
    return None


def ensure_namespaces(istio_revision: str = "") -> str:
    """Create namespaces with Istio sidecar injection labels."""
    console.print("\n[bold]Ensuring namespaces...[/bold]")

    if not istio_revision:
        istio_revision = _detect_istio_revision() or "asm-1-30"
    console.print(f"  [info]Istio revision: {istio_revision}[/info]")

    for ns in ["osdu-flux", "foundation", "platform", TESTER_NAMESPACE]:
        run_process(
            ["kubectl", "create", "namespace", ns],
            capture_output=True,
            text=True,
        )

    # Only osdu gets sidecar injection; the middleware does not need the mesh.
    yaml_content = f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: osdu
  labels:
    istio.io/rev: {istio_revision}
"""
    kubectl_apply_yaml(yaml_content, "create namespace osdu")

    display_result("Namespaces ready")
    return istio_revision


def render_istio_revision_configmap(
    istio_revision: str, extra: dict[str, str] | None = None
) -> str:
    """Render the Flux substitution ConfigMap: the Istio revision plus deploy identity facts."""

    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {ISTIO_REVISION_CONFIGMAP}",
        f"  namespace: {ISTIO_REVISION_NAMESPACE}",
        "  labels:",
        "    app.kubernetes.io/managed-by: osdu-spi-stack",
        "data:",
        f"  {ISTIO_REVISION_KEY}: {json.dumps(istio_revision)}",
    ]
    # An absent key lets fork-rbac's ${...:=default} bind nobody; an empty
    # value would substitute to "" and make the RoleBinding subject invalid.
    for key in DEPLOY_IDENTITY_KEYS:
        value = (extra or {}).get(key, "")
        if value:
            lines.append(f"  {key}: {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def create_istio_revision_configmap(
    istio_revision: str = "", extra: dict[str, str] | None = None
) -> None:
    """Apply the ConfigMap that carries the Istio revision and deploy identity facts for Flux.

    A refresh without ``extra`` keeps the identity facts already on the
    cluster, since ``kubectl apply`` would otherwise drop the omitted keys.
    """

    if extra is None:
        try:
            extra = read_cluster_config()
        except ClusterConfigError as exc:
            console.print(
                f"[warning]{exc}; leaving the existing {ISTIO_REVISION_CONFIGMAP} "
                "ConfigMap unchanged.[/warning]"
            )
            return
    if not istio_revision:
        detected = _detect_istio_revision()
        if not detected:
            console.print(
                f"[warning]Could not detect the Istio revision; leaving the existing "
                f"{ISTIO_REVISION_CONFIGMAP} ConfigMap unchanged.[/warning]"
            )
            return
        istio_revision = detected
    yaml_content = render_istio_revision_configmap(istio_revision, extra)
    display_yaml(yaml_content, f"ConfigMap: {ISTIO_REVISION_CONFIGMAP}")
    kubectl_apply_yaml(yaml_content, f"apply {ISTIO_REVISION_CONFIGMAP} ConfigMap")
    display_result(f"{ISTIO_REVISION_CONFIGMAP} ConfigMap created")


INIT_VALUES_CONFIGMAP = "spi-init-values"


def read_init_values() -> str:
    """The live values.yaml blob the init chart renders from; empty when absent."""
    return _read_configmap_data(INIT_VALUES_CONFIGMAP).get("values.yaml", "")


def refresh_spi_init_values() -> None:
    """Re-render spi-init-values from the live partition list and deploy identity.

    ``spi reconcile`` has no Config, so the partitions and legal tag come from
    the ConfigMap already on the cluster and the member list from
    spi-cluster-config. A cluster bootstrapped before either existed is left
    alone: there is nothing to re-render from, and ``spi up`` writes both.
    """
    try:
        values = parse_init_values(read_init_values())
        partitions = values.get("partitions") or []
        if not partitions:
            console.print("  [dim]spi-init-values not found; skipping members refresh[/dim]")
            return
        cluster_cfg = read_cluster_config()
        client_id = cluster_cfg.get("DEPLOY_IDENTITY_CLIENT_ID", "")
        member_id = cluster_cfg.get("MEMBER_IDENTITY_CLIENT_ID", "")
    except ClusterConfigError as exc:
        console.print(f"[warning]{exc}; leaving {INIT_VALUES_CONFIGMAP} unchanged.[/warning]")
        return
    members = [client_id] if client_id else []
    member_users = [member_id] if member_id else []
    if members == list(values.get("entitlementsMembers") or []) and member_users == list(
        values.get("entitlementsMemberUsers") or []
    ):
        return
    legal_tag = values.get("legalTag") or LEGAL_TAG_BASE
    yaml_content = spi_init_values_configmap(partitions, members, legal_tag, member_users)
    display_yaml(yaml_content, f"ConfigMap: {INIT_VALUES_CONFIGMAP}")
    kubectl_apply_yaml(yaml_content, f"refresh {INIT_VALUES_CONFIGMAP} ConfigMap")


def create_storage_classes() -> None:
    """Create Premium StorageClasses for stateful middleware."""
    console.print("\n[bold]Creating StorageClasses...[/bold]")
    provisioner = "disk.csi.azure.com"
    extra_params = "  skuName: Premium_LRS\n  kind: Managed\n  cachingMode: ReadOnly"
    console.print(f"  [info]Using provisioner: {provisioner}[/info]")

    for sc_name in STORAGE_CLASSES:
        yaml_content = storage_class(sc_name, provisioner, extra_params)
        display_yaml(yaml_content, f"StorageClass: {sc_name}")
        kubectl_apply_yaml(yaml_content, f"apply StorageClass {sc_name}")
        console.print(f"  [success]{sc_name} created[/success]")


def install_gateway_api_crds() -> None:
    console.print("\n[bold]Installing Gateway API CRDs...[/bold]")
    run_command(
        [
            "kubectl",
            "apply",
            "-f",
            "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml",
        ],
        description="Install Gateway API CRDs",
    )
    display_result("Gateway API CRDs installed")
