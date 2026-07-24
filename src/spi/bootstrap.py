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

"""In-cluster bootstrap: namespaces, StorageClasses, Gateway API CRDs."""

from .console import console, display_result, display_yaml
from .shell import kubectl_apply_yaml, kubectl_json, run_command
from .templates import storage_class

STORAGE_CLASSES = ["pg-storageclass", "redis-storageclass", "es-storageclass"]


def _detect_istio_revision() -> str:
    """Detect the installed Istio ASM revision from the cluster."""
    data = kubectl_json(["get", "deploy", "-n", "aks-istio-system"])
    if data and data.get("items"):
        for item in data["items"]:
            name = item.get("metadata", {}).get("name", "")
            if name.startswith("istiod-"):
                return name.removeprefix("istiod-")

    data = kubectl_json(["get", "ns", "aks-istio-system"])
    if data:
        rev = data.get("metadata", {}).get("labels", {}).get("istio.io/rev", "")
        if rev:
            return rev

    data = kubectl_json(["get", "pods", "-n", "aks-istio-system"])
    if data and data.get("items"):
        rev = data["items"][0].get("metadata", {}).get("labels", {}).get("istio.io/rev", "")
        if rev:
            return rev
    raise RuntimeError("Unable to detect the installed AKS managed Istio revision.")


def ensure_namespaces(istio_revision: str = "") -> None:
    """Create namespaces with Istio sidecar injection labels."""
    console.print("\n[bold]Ensuring namespaces...[/bold]")

    if not istio_revision:
        istio_revision = _detect_istio_revision()
    console.print(f"  [info]Istio revision: {istio_revision}[/info]")

    for ns in ["osdu-flux", "foundation", "platform"]:
        yaml_content = f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
"""
        kubectl_apply_yaml(yaml_content, f"create namespace {ns}")

    # Only osdu namespace gets Istio injection (platform middleware
    # does not need the service mesh and istio-init requires NET_ADMIN
    # which AKS Deployment Safeguards rejects).
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
