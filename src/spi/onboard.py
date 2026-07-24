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

"""Cluster-side onboarding: grant a GitHub service-fork repo permission to deploy
into this spi-stack cluster (CI/CD deploy lane, design SS9.4.A).

This is the cluster-side half of a two-step onboarding flow. It creates the Azure
identity + RBAC a fork's GitHub Actions need to `kubectl set image` its own Deployment,
then writes the handoff secrets/variables onto the repo. The fork-side half
(init.yml / settings-apply.yml in osdu-spi) configures the repo itself.

Authorization model: this cluster runs **Azure RBAC for Kubernetes**
(`aadProfile.enableAzureRBAC = true`, `disableLocalAccounts = true`). A plain
Kubernetes RoleBinding is therefore NOT the authorization path (Phase 0 gate 0b);
deploy permission is granted via **Azure role assignments**. To honor the design's
least-privilege intent (SS6.1 step 3 -- patch only the named Deployment + read
pods/replicasets/events/logs, NOT broad write), we define a **custom Azure role**
with the equivalent dataActions and assign it at the namespace scope, rather than
the broad built-in "Azure Kubernetes Service RBAC Writer".

Every step is idempotent: re-running against the same --repo makes no duplicate
identities or role assignments and does not overwrite secrets unless
--force-rewrite-secrets is given.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import typer

from .console import console, display_result
from .guard import DEFAULT_FLUX_NAMESPACE
from .shell import run_command

# Federated-credential subjects to register on the target repo. The corp Entra tenant
# commonly disables wildcard subjects, so we enumerate the OSDU branch set explicitly
# plus the pull_request subject. (Design SS6.1 step 2: "enumerate explicit refs if the
# wildcard feature is unavailable".)
OSDU_BRANCHES = ("main", "fork_integration", "fork_upstream")
GH_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GH_OIDC_AUDIENCE = "api://AzureADTokenExchange"
GH_API_VERSION = "2026-03-10"
NO_DATA_ACCESS_IDENTITY_NAME = "spi-ci-no-data-access"
FEDERATED_CREDENTIAL_LIMIT = 20
NO_DATA_ACCESS_TOKEN_ENVS = {
    # Storage is the active SPI profile with a deliberately skipped negative-auth test.
    "storage": "NO_DATA_ACCESS_TESTER_ACCESS_TOKEN",
}

# Least-privilege dataActions for the custom deploy role (Azure RBAC for Kubernetes).
# Mirrors the Kubernetes Role in design SS6.1 step 3: patch the deployment + read the
# pod/replicaset/event chain that `kubectl rollout status` walks. Pod log read and Flux
# Kustomization read are intentionally NOT here: Azure RBAC for Kubernetes registers no
# `pods/log/read` dataAction and no dataAction for the Flux CRD group (verified against the
# Microsoft.ContainerService provider operations), so `az role definition create` rejects
# them. Flux Kustomization read (needed by the CI-mode suspend pre-check) is granted via
# native k8s RBAC instead; see _ensure_flux_read_rbac.
DEPLOY_DATA_ACTIONS = [
    "Microsoft.ContainerService/managedClusters/apps/deployments/read",
    "Microsoft.ContainerService/managedClusters/apps/deployments/write",
    "Microsoft.ContainerService/managedClusters/apps/replicasets/read",
    "Microsoft.ContainerService/managedClusters/pods/read",
    "Microsoft.ContainerService/managedClusters/events/read",
]
FLUX_READER_ROLE = "spi-ci-flux-reader"
AKS_CLUSTER_USER_ROLE = "Azure Kubernetes Service Cluster User Role"
KEY_VAULT_SECRETS_USER_ROLE = "Key Vault Secrets User"

# Audience the deploy lane's integration-test mints the acceptance-test token against
# (`az account get-access-token --resource <AAD_CLIENT_ID>`). SPI CI identities are MSIs
# federated to GitHub; an MSI is not itself a requestable resource (`--resource <msi-appid>`
# -> AADSTS100040), and the stack has no dedicated OSDU AAD app registration. So the token is
# minted for ARM (a universally-requestable resource); it carries aud=management.azure.com and
# appid=<MSI>. The istio RequestAuthentication (ADR-016) trusts this audience and the Lua
# projects the appid as x-user-id, which entitlements authorizes via the seeded membership.
AAD_TOKEN_RESOURCE = "https://management.azure.com"

# Entitlements seed (per-identity model). `spi up` only provisions the tenant (the deployer
# UAMI becomes OWNER of every group); it never grants any other identity access. So once a
# freshly onboarded CI identity flows as itself through the mesh, it is a member of nothing
# and 403s every user-facing service. `spi onboard` therefore seeds the new identity into
# the same four root groups ADME data-seeding uses (InstanceInit.cs), via a short in-cluster
# Job that runs under the OSDU workload identity (the OWNER, so it is authorized to call the
# entitlements AddMember API). This is per CI identity, so it belongs to onboard, not to the
# stack bootstrap.
WORKLOAD_IDENTITY_SA = "workload-identity-sa"
SEED_JOB_NAME = "spi-onboard-seed"
SEED_IMAGE = "python:3.12-slim"
ENTITLEMENTS_SEED_GROUPS = (
    "users",
    "users.datalake.ops",
    "users.datalake.admins",
    "users.data.root",
)

# Self-contained add-member script (no dependency on the bootstrap scripts ConfigMap). It
# mints a v1.0 management token under the workload identity (matching osdu-spi-init auth.py),
# discovers the entitlements domain from the OWNER's own group list, then adds the CI
# identity appid to the four root groups and verifies membership.
_SEED_SCRIPT = r"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error

PARTITION = os.environ["PARTITION"]
APPID = os.environ["CI_MSI_APPID"].strip().lower()
ENT = os.environ.get("ENTITLEMENTS_HOST", "http://entitlements.osdu.svc.cluster.local")
BASE = ENT + "/api/entitlements/v2"
GROUPS = ["users", "users.datalake.ops", "users.datalake.admins", "users.data.root"]


def get_token():
    tenant = os.environ["AZURE_TENANT_ID"]
    client = os.environ["AZURE_CLIENT_ID"]
    path = os.environ.get(
        "AZURE_FEDERATED_TOKEN_FILE", "/var/run/secrets/azure/tokens/azure-identity-token"
    )
    with open(path) as fh:
        assertion = fh.read().strip()
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
        "resource": "https://management.azure.com/",
    }).encode()
    req = urllib.request.Request(
        "https://login.microsoftonline.com/%s/oauth2/token" % tenant,
        data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read())["access_token"]


def call(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": "Bearer " + token, "data-partition-id": PARTITION})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        return resp.getcode(), resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def resolve_domain(token):
    for _ in range(60):
        code, payload = call("GET", "/groups", token)
        if code == 200:
            groups = json.loads(payload).get("groups", [])
            for g in groups:
                email = g.get("email", "")
                if "@" in email:
                    right = email.split("@", 1)[1]
                    if right.startswith(PARTITION + "."):
                        return right[len(PARTITION) + 1:], len(groups)
        time.sleep(5)
    return None, 0


def main():
    print("Seeding entitlements member '%s' into partition '%s'" % (APPID, PARTITION))
    token = get_token()
    domain, count = resolve_domain(token)
    if not domain:
        print("ERROR: could not resolve entitlements groups for '%s'" % PARTITION)
        return 1
    print("  domain = %s (groups visible: %d)" % (domain, count))
    rc = 0
    for name in GROUPS:
        grp = "%s@%s.%s" % (name, PARTITION, domain)
        code, payload = call("POST", "/groups/%s/members" % grp, token,
                             {"email": APPID, "role": "MEMBER"})
        ok = code in (200, 409)
        print("  add %s -> %s%s" % (grp, code, "" if ok else " " + payload[:160]))
        if not ok:
            rc = 1
    for name in GROUPS:
        grp = "%s@%s.%s" % (name, PARTITION, domain)
        code, payload = call("GET", "/groups/%s/members" % grp, token)
        present = (APPID in payload.lower()) if code == 200 else False
        print("  verify %s -> present=%s" % (grp, present))
        if not present:
            rc = 1
    print("RESULT rc=%s" % rc)
    return rc


sys.exit(main())
"""


def _resolve(cmd_list: List[str]) -> List[str]:
    """Resolve argv[0] to an absolute path so subprocess (shell=False) finds it.

    Windows exposes az/gh/kubectl as ``.cmd`` shims that PATHEXT resolves for a shell but
    not for ``subprocess.run`` with a bare name. Resolve up front; fall back to the name.
    """
    if not cmd_list:
        return cmd_list
    found = shutil.which(cmd_list[0])
    return [found, *cmd_list[1:]] if found else cmd_list


def _run(cmd_list: List[str], **kwargs: Any) -> Any:
    """``run_command`` with argv[0] resolved for Windows (az/gh/kubectl are ``.cmd`` shims).

    ``run_command`` invokes subprocess with ``shell=False``; a bare ``az`` is not found by
    ``CreateProcess`` on Windows. Resolve up front so the mutating onboarding calls work
    cross-platform, mirroring the ``_resolve`` wrapping used on the read-only paths.
    """
    return run_command(_resolve(cmd_list), **kwargs)


@dataclass
class OnboardInputs:
    service: str
    repo: str  # org/repo
    aks_cluster: str
    aks_rg: str
    identities_rg: str
    namespace: str = "osdu"
    flux_namespace: str = DEFAULT_FLUX_NAMESPACE
    partition: str = "opendes"
    keyvault: Optional[str] = None
    gateway_url: Optional[str] = None
    no_data_access_token_env: Optional[str] = None
    dry_run: bool = False
    force_rewrite_secrets: bool = False
    # Captured/derived during the run.
    subscription_id: str = ""
    tenant_id: str = ""
    cluster_resource_id: str = ""
    identity_client_id: str = ""
    identity_principal_id: str = ""
    no_data_access_identity_client_id: str = ""
    no_data_access_identity_principal_id: str = ""
    github_oidc_subject_prefix: str = ""
    deployment_name: str = ""
    container_name: str = ""
    kv_secret_names: List[str] = field(default_factory=list)

    @property
    def identity_name(self) -> str:
        return f"spi-ci-{self.service}"

    @property
    def deploy_role_name(self) -> str:
        return f"spi-ci-{self.service}-deploy"

    @property
    def no_data_access_identity_name(self) -> str:
        return NO_DATA_ACCESS_IDENTITY_NAME

    @property
    def resolved_no_data_access_token_env(self) -> str:
        if self.no_data_access_token_env is not None:
            return self.no_data_access_token_env.strip()
        return NO_DATA_ACCESS_TOKEN_ENVS.get(self.service.lower(), "")

    @property
    def uses_no_data_access_identity(self) -> bool:
        return bool(self.resolved_no_data_access_token_env)

    @property
    def namespace_scope(self) -> str:
        return f"{self.cluster_resource_id}/namespaces/{self.namespace}"

    @property
    def flux_namespace_scope(self) -> str:
        return f"{self.cluster_resource_id}/namespaces/{self.flux_namespace}"


def _az_json(args: List[str], check: bool = True) -> Any:
    """Run an `az ... -o json` command silently and return parsed JSON (or None).

    Used for existence probes and lookups where the transparent command panel from
    ``run_command`` would be noise. ``check=False`` lets callers treat a non-zero
    exit (e.g. "not found") as ``None`` for idempotency checks.
    """
    cmd = _resolve(["az", *args, "--output", "json"])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        if check:
            console.print(
                f"  [error]az {' '.join(args[:3])} failed: {(result.stderr or '').strip()}[/error]"
            )
            raise typer.Exit(code=1)
        return None
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def _gh_json(args: List[str], check: bool = True) -> Any:
    """Run a read-only ``gh ...`` command and return decoded JSON."""
    result = subprocess.run(
        _resolve(["gh", *args]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        if check:
            console.print(
                f"  [error]gh {' '.join(args[:3])} failed: {(result.stderr or '').strip()}[/error]"
            )
            raise typer.Exit(code=1)
        return None
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def _plan(message: str) -> None:
    console.print(f"  [warning][dry-run][/warning] {message}")


# --------------------------------------------------------------------------------------
# Step 1 - operator precondition checks
# --------------------------------------------------------------------------------------
def _check_preconditions(inp: OnboardInputs) -> None:
    """Verify az/kubectl/gh are authenticated with the access onboarding needs.

    Fails fast with a remediation message rather than half-applying a grant.
    """
    console.print("\n[bold]Verifying operator preconditions...[/bold]")

    account = _az_json(["account", "show"], check=False)
    if not account:
        console.print(
            "  [error]Azure CLI is not logged in. Run: az login --tenant <tenant> "
            "and az account set --subscription <sub>[/error]"
        )
        raise typer.Exit(code=1)
    inp.subscription_id = account.get("id", "")
    inp.tenant_id = account.get("tenantId", "")
    console.print(
        f"  [info]Subscription: {account.get('name', '?')} ({inp.subscription_id})[/info]"
    )

    # Resolve + cache the cluster resource id (also proves AKS read access).
    cluster = _az_json(
        ["aks", "show", "--name", inp.aks_cluster, "--resource-group", inp.aks_rg], check=False
    )
    if not cluster:
        console.print(
            f"  [error]Cannot read AKS cluster {inp.aks_cluster} in {inp.aks_rg}. "
            "Check --aks-cluster/--aks-rg and your Azure RBAC.[/error]"
        )
        raise typer.Exit(code=1)
    inp.cluster_resource_id = cluster.get("id", "")
    if not cluster.get("aadProfile", {}).get("enableAzureRbac"):
        console.print(
            "  [warning]Cluster does not report Azure RBAC for Kubernetes. This command "
            "provisions Azure role assignments; on a non-Azure-RBAC cluster a Kubernetes "
            "RoleBinding would be required instead (design SS6.1 step 3).[/warning]"
        )

    # gh auth + admin on the target repo (needed to write secrets/variables).
    gh_status = subprocess.run(_resolve(["gh", "auth", "status"]), capture_output=True, text=True)
    if gh_status.returncode != 0:
        console.print("  [error]GitHub CLI is not authenticated. Run: gh auth login[/error]")
        raise typer.Exit(code=1)
    repo_view = subprocess.run(
        _resolve(["gh", "repo", "view", inp.repo, "--json", "viewerPermission"]),
        capture_output=True,
        text=True,
    )
    if repo_view.returncode != 0:
        console.print(
            f"  [error]Cannot access repo {inp.repo} via gh. Check the name and SSO.[/error]"
        )
        raise typer.Exit(code=1)
    perm = (json.loads(repo_view.stdout or "{}").get("viewerPermission") or "").upper()
    if perm not in ("ADMIN", "MAINTAIN", "WRITE"):
        console.print(
            f"  [error]Insufficient permission on {inp.repo} (have {perm or 'NONE'}); need "
            "WRITE+ to set secrets/variables.[/error]"
        )
        raise typer.Exit(code=1)

    oidc_config = _gh_json(
        [
            "api",
            "-H",
            f"X-GitHub-Api-Version: {GH_API_VERSION}",
            f"repos/{inp.repo}/actions/oidc/customization/sub",
        ],
        check=False,
    )
    if not oidc_config:
        console.print(
            f"  [error]Cannot resolve the GitHub OIDC subject format for {inp.repo}. "
            "Verify Actions OIDC settings access and retry.[/error]"
        )
        raise typer.Exit(code=1)
    if not oidc_config.get("use_default", False):
        console.print(
            f"  [error]{inp.repo} uses a custom GitHub OIDC subject template. "
            "spi onboard currently requires the default subject format.[/error]"
        )
        raise typer.Exit(code=1)
    inp.github_oidc_subject_prefix = str(oidc_config.get("sub_claim_prefix") or "")
    if not inp.github_oidc_subject_prefix:
        console.print(
            f"  [error]GitHub did not return an OIDC subject prefix for {inp.repo}.[/error]"
        )
        raise typer.Exit(code=1)
    console.print(f"  [info]GitHub OIDC subject: {inp.github_oidc_subject_prefix}:...[/info]")

    display_result("Preconditions OK (az + AKS read + gh repo write)")


# --------------------------------------------------------------------------------------
# Step 2 - verify the Deployment exists, capture names
# --------------------------------------------------------------------------------------
def _verify_deployment(inp: OnboardInputs) -> None:
    """Confirm Deployment/<name> exists in the namespace; capture deployment + container.

    The convention is Deployment name == ``osdu-<service>``; we resolve it from the live
    cluster rather than assuming, and read the first container's name (D13).
    """
    console.print("\n[bold]Verifying target Deployment...[/bold]")
    # Ensure we have a kube context for this cluster (idempotent). Skip in dry-run -- fetching
    # credentials mutates the local kubeconfig, which a plan-only run must not do.
    if not inp.dry_run:
        _run(
            [
                "az",
                "aks",
                "get-credentials",
                "--resource-group",
                inp.aks_rg,
                "--name",
                inp.aks_cluster,
                "--overwrite-existing",
                "--only-show-errors",
            ],
            description="Get AKS credentials",
            check=False,
        )

    candidate = f"osdu-{inp.service}"
    deployment = subprocess.run(
        _resolve(["kubectl", "get", "deployment", candidate, "-n", inp.namespace, "-o", "json"]),
        capture_output=True,
        text=True,
    )
    if deployment.returncode != 0:
        msg = (
            f"Deployment/{candidate} not found in namespace '{inp.namespace}'. "
            "The service must be deployed (its HelmRelease reconciled) before onboarding."
        )
        if inp.dry_run:
            # A plan-only run may not have a kube context yet; warn and assume the convention.
            console.print(
                f"  [warning][dry-run] could not read the live Deployment ({msg}). Assuming '{candidate}'.[/warning]"
            )
            inp.deployment_name = candidate
            inp.container_name = candidate
            display_result(f"(dry-run) target Deployment assumed '{candidate}'")
            return
        console.print(f"  [error]{msg}[/error]")
        raise typer.Exit(code=1)
    obj = json.loads(deployment.stdout)
    inp.deployment_name = obj["metadata"]["name"]
    containers = obj["spec"]["template"]["spec"]["containers"]
    inp.container_name = containers[0]["name"]
    display_result(
        f"Deployment '{inp.deployment_name}' (container '{inp.container_name}') in '{inp.namespace}'"
    )


# --------------------------------------------------------------------------------------
# Step 3 - User-Assigned Managed Identity
# --------------------------------------------------------------------------------------
def _ensure_identity_resource(
    inp: OnboardInputs, identity_name: str, heading: str
) -> Dict[str, Any]:
    console.print(f"\n[bold]{heading}[/bold]")
    existing = _az_json(
        ["identity", "show", "--name", identity_name, "--resource-group", inp.identities_rg],
        check=False,
    )
    if existing:
        display_result(f"Identity '{identity_name}' already exists")
    elif inp.dry_run:
        _plan(f"az identity create --name {identity_name} --resource-group {inp.identities_rg}")
        return {
            "clientId": f"<client-id:{identity_name}>",
            "principalId": f"<principal-id:{identity_name}>",
        }
    else:
        _run(
            [
                "az",
                "identity",
                "create",
                "--name",
                identity_name,
                "--resource-group",
                inp.identities_rg,
                "--output",
                "none",
            ],
            description=f"Create UAMI {identity_name}",
        )
        existing = _az_json(
            ["identity", "show", "--name", identity_name, "--resource-group", inp.identities_rg]
        )
    if not existing or not existing.get("clientId") or not existing.get("principalId"):
        console.print(f"  [error]Could not resolve identity IDs for {identity_name}.[/error]")
        raise typer.Exit(code=1)
    return existing


def _ensure_identity(inp: OnboardInputs) -> None:
    existing = _ensure_identity_resource(inp, inp.identity_name, "Ensuring managed identity...")
    inp.identity_client_id = existing.get("clientId", "")
    inp.identity_principal_id = existing.get("principalId", "")


def _ensure_no_data_access_identity(inp: OnboardInputs) -> None:
    existing = _ensure_identity_resource(
        inp,
        inp.no_data_access_identity_name,
        "Ensuring shared no-data-access identity...",
    )
    inp.no_data_access_identity_client_id = existing.get("clientId", "")
    inp.no_data_access_identity_principal_id = existing.get("principalId", "")


# --------------------------------------------------------------------------------------
# Step 4 - federated credentials
# --------------------------------------------------------------------------------------
def _service_federated_credentials(inp: OnboardInputs) -> Dict[str, str]:
    if not inp.github_oidc_subject_prefix:
        raise ValueError("GitHub OIDC subject prefix has not been resolved")
    subjects = {
        f"spi-ci-{inp.service}-pull-request": (f"{inp.github_oidc_subject_prefix}:pull_request"),
    }
    for branch in OSDU_BRANCHES:
        subjects[f"spi-ci-{inp.service}-branch-{branch}"] = (
            f"{inp.github_oidc_subject_prefix}:ref:refs/heads/{branch}"
        )
    return subjects


def _no_data_access_federated_credentials(inp: OnboardInputs) -> Dict[str, str]:
    if not inp.github_oidc_subject_prefix:
        raise ValueError("GitHub OIDC subject prefix has not been resolved")
    subjects = {
        f"spi-no-data-{inp.service}-pull-request": (
            f"{inp.github_oidc_subject_prefix}:pull_request"
        ),
    }
    for branch in OSDU_BRANCHES:
        subjects[f"spi-no-data-{inp.service}-branch-{branch}"] = (
            f"{inp.github_oidc_subject_prefix}:ref:refs/heads/{branch}"
        )
    return subjects


def _reconcile_federated_credentials(
    inp: OnboardInputs, identity_name: str, subjects: Dict[str, str]
) -> None:
    console.print(f"\n[bold]Ensuring federated credentials for {identity_name}...[/bold]")

    existing = (
        _az_json(
            [
                "identity",
                "federated-credential",
                "list",
                "--identity-name",
                identity_name,
                "--resource-group",
                inp.identities_rg,
            ],
            check=False,
        )
        or []
    )
    existing_subjects = {fc.get("subject") for fc in existing}
    existing_by_name = {fc.get("name"): fc for fc in existing}
    credential_count = len(existing)

    for name, subject in subjects.items():
        if subject in existing_subjects:
            console.print(f"  [info]federated credential for '{subject}' already present[/info]")
            continue
        if name in existing_by_name:
            if inp.dry_run:
                _plan(f"az identity federated-credential update --name {name} --subject {subject}")
                continue
            _run(
                [
                    "az",
                    "identity",
                    "federated-credential",
                    "update",
                    "--name",
                    name,
                    "--identity-name",
                    identity_name,
                    "--resource-group",
                    inp.identities_rg,
                    "--issuer",
                    GH_OIDC_ISSUER,
                    "--subject",
                    subject,
                    "--audiences",
                    GH_OIDC_AUDIENCE,
                    "--output",
                    "none",
                ],
                description=f"Update federated credential {name}",
            )
            console.print(f"  [success]updated federated credential for '{subject}'[/success]")
            continue
        if credential_count >= FEDERATED_CREDENTIAL_LIMIT:
            console.print(
                f"  [error]Identity {identity_name} already has "
                f"{FEDERATED_CREDENTIAL_LIMIT} federated credentials. "
                "Use another shared identity before onboarding more repositories.[/error]"
            )
            raise typer.Exit(code=1)
        if inp.dry_run:
            _plan(f"az identity federated-credential create --name {name} --subject {subject}")
            credential_count += 1
            continue
        _run(
            [
                "az",
                "identity",
                "federated-credential",
                "create",
                "--name",
                name,
                "--identity-name",
                identity_name,
                "--resource-group",
                inp.identities_rg,
                "--issuer",
                GH_OIDC_ISSUER,
                "--subject",
                subject,
                "--audiences",
                GH_OIDC_AUDIENCE,
                "--output",
                "none",
            ],
            description=f"Federated credential {subject}",
        )
        credential_count += 1
    display_result(f"{len(subjects)} federated-credential subject(s) reconciled")


def _ensure_federated_credentials(inp: OnboardInputs) -> None:
    _reconcile_federated_credentials(inp, inp.identity_name, _service_federated_credentials(inp))


def _ensure_no_data_access_federated_credentials(inp: OnboardInputs) -> None:
    _reconcile_federated_credentials(
        inp,
        inp.no_data_access_identity_name,
        _no_data_access_federated_credentials(inp),
    )


def _remove_no_data_access_federated_credentials(inp: OnboardInputs) -> None:
    identity = _az_json(
        [
            "identity",
            "show",
            "--name",
            inp.no_data_access_identity_name,
            "--resource-group",
            inp.identities_rg,
        ],
        check=False,
    )
    if not identity:
        return

    existing = (
        _az_json(
            [
                "identity",
                "federated-credential",
                "list",
                "--identity-name",
                inp.no_data_access_identity_name,
                "--resource-group",
                inp.identities_rg,
            ]
        )
        or []
    )
    existing_names = {credential.get("name") for credential in existing}
    removed = 0
    for name in _no_data_access_federated_credentials(inp):
        if name not in existing_names:
            continue
        if inp.dry_run:
            _plan(
                "az identity federated-credential delete "
                f"--identity-name {inp.no_data_access_identity_name} --name {name}"
            )
        else:
            _run(
                [
                    "az",
                    "identity",
                    "federated-credential",
                    "delete",
                    "--name",
                    name,
                    "--identity-name",
                    inp.no_data_access_identity_name,
                    "--resource-group",
                    inp.identities_rg,
                    "--yes",
                    "--output",
                    "none",
                ],
                description=f"Remove federated credential {name}",
            )
        removed += 1
    if removed:
        display_result(f"{removed} no-data-access federated credential(s) removed")


# --------------------------------------------------------------------------------------
# Steps 5-7 - Azure RBAC role assignments
# --------------------------------------------------------------------------------------
def _assign_role(inp: OnboardInputs, role: str, scope: str, description: str) -> None:
    """Idempotently create an Azure role assignment for the identity at a scope."""
    if not inp.identity_principal_id:
        if inp.dry_run:
            _plan(f"(after identity exists) assign '{role}' at {scope}")
            return
        console.print("  [error]Identity principalId unknown; cannot assign role.[/error]")
        raise typer.Exit(code=1)

    def _assignment_present() -> bool:
        return bool(
            _az_json(
                [
                    "role",
                    "assignment",
                    "list",
                    "--assignee",
                    inp.identity_principal_id,
                    "--role",
                    role,
                    "--scope",
                    scope,
                ],
                check=False,
            )
        )

    if _assignment_present():
        console.print(f"  [info]{description}: already assigned[/info]")
        return
    if inp.dry_run:
        _plan(f"az role assignment create --role '{role}' --scope {scope}")
        return
    # A freshly-created custom role definition can lag in propagation, so
    # ``az role assignment create`` may transiently fail to resolve --role by name.
    # Retry with backoff and verify via re-query (the source of truth), then surface
    # a hard failure rather than continuing silently. Previously this ran once with
    # check=False, so the propagation-race error was swallowed and the namespace
    # deploy role was left unassigned (the deploy lane then 403'd and the grant had
    # to be done by hand).
    attempts = 6
    delay_seconds = 10
    for attempt in range(1, attempts + 1):
        _run(
            [
                "az",
                "role",
                "assignment",
                "create",
                "--assignee-object-id",
                inp.identity_principal_id,
                "--assignee-principal-type",
                "ServicePrincipal",
                "--role",
                role,
                "--scope",
                scope,
                "--output",
                "none",
            ],
            description=description,
            check=False,
        )
        if _assignment_present():
            if attempt > 1:
                display_result(f"{description}: assigned (after {attempt} attempts)")
            return
        if attempt < attempts:
            console.print(
                f"  [warning]{description}: not yet present "
                f"(attempt {attempt}/{attempts}); retrying in {delay_seconds}s "
                "(role-definition propagation)[/warning]"
            )
            time.sleep(delay_seconds)
    console.print(
        f"  [error]{description}: role assignment did not materialize after "
        f"{attempts} attempts (scope={scope}).[/error]"
    )
    raise typer.Exit(code=1)


def _ensure_custom_deploy_role(inp: OnboardInputs) -> None:
    """Create/update the least-privilege custom Azure role for namespace deploy.

    Assignable at the cluster scope; the assignment (separate step) is namespace-scoped.
    """
    description = (
        f"SPI CI deploy for {inp.service}: patch Deployment/{inp.deployment_name} and read "
        "pods/replicasets/events/logs in its namespace. Least-privilege (design SS6.1)."
    )
    existing = _az_json(["role", "definition", "list", "--name", inp.deploy_role_name], check=False)
    existing_role = existing[0] if isinstance(existing, list) and existing else None
    if existing_role:
        if not existing_role.get("id") or not existing_role.get("roleName"):
            console.print(
                f"  [error]Existing custom role '{inp.deploy_role_name}' has no role ID.[/error]"
            )
            raise typer.Exit(code=1)

        # Custom roles are subscription-wide. Rehoming one service repo to a new
        # Stack must add the new AKS scope without invalidating retained clusters.
        scopes: list[str] = []
        seen: set[str] = set()
        for scope in [*existing_role.get("assignableScopes", []), inp.cluster_resource_id]:
            normalized = scope.lower()
            if normalized not in seen:
                scopes.append(scope)
                seen.add(normalized)
        role_def = {
            **existing_role,
            "roleName": inp.deploy_role_name,
            "description": description,
            "permissions": [
                {
                    "actions": [],
                    "notActions": [],
                    "dataActions": DEPLOY_DATA_ACTIONS,
                    "notDataActions": [],
                }
            ],
            "assignableScopes": scopes,
        }
    else:
        # ``az role definition create`` accepts the documented create schema,
        # while update requires the camelCase shape returned by list/show.
        role_def = {
            "Name": inp.deploy_role_name,
            "Description": description,
            "Actions": [],
            "NotActions": [],
            "DataActions": DEPLOY_DATA_ACTIONS,
            "NotDataActions": [],
            "AssignableScopes": [inp.cluster_resource_id],
        }

    if inp.dry_run:
        verb = "update" if existing_role else "create"
        _plan(
            f"az role definition {verb} '{inp.deploy_role_name}' (custom least-privilege deploy role)"
        )
        return
    action = "update" if existing_role else "create"
    # Pass the role definition as a temp @file rather than inline JSON. On Windows the az
    # entrypoint is a .cmd shim, and cmd.exe re-parses an inline JSON string (the braces,
    # quotes, and brackets trip "was unexpected at this time"); the @file form sidesteps all
    # shell quoting. az reads JSON from the path after the leading '@'.
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(role_def, handle)
        handle.close()
        _run(
            [
                "az",
                "role",
                "definition",
                action,
                "--role-definition",
                f"@{handle.name}",
                "--output",
                "none",
            ],
            description=f"{action.capitalize()} custom role {inp.deploy_role_name}",
            check=True,
        )
    finally:
        os.unlink(handle.name)
    # A brand-new custom role definition is not immediately resolvable by name in
    # ``az role assignment create``; poll until it is queryable so the subsequent
    # namespace-scoped assignment does not lose a propagation race.
    if action == "create":
        for _ in range(12):
            if _az_json(
                ["role", "definition", "list", "--name", inp.deploy_role_name],
                check=False,
            ):
                break
            time.sleep(5)


def _ensure_flux_read_rbac(inp: OnboardInputs) -> None:
    """Grant the CI identity read on Flux reconciliation resources via native k8s RBAC.

    The deploy lane's CI-mode suspend pre-check lists Kustomizations and HelmReleases in the
    flux namespace. Azure RBAC for Kubernetes registers no dataAction for Flux CRD groups, so
    this cannot be a custom Azure role; a native Role + RoleBinding is the path (native RBAC
    is additive to Azure RBAC, so it is honored even with Azure RBAC enabled and local accounts
    disabled). The binding subject is the identity's AAD object id (principalId), matching how
    the cluster's Azure RBAC webhook names service-principal callers.
    """
    console.print("\n[bold]Ensuring Flux read (native RBAC)...[/bold]")
    if not inp.identity_principal_id:
        console.print("  [warning]identity principal id unknown; skipping Flux read RBAC[/warning]")
        return
    binding = f"spi-ci-{inp.service}-flux-reader"
    if inp.dry_run:
        _plan(
            f"kubectl apply Role {FLUX_READER_ROLE} + RoleBinding {binding} in "
            f"'{inp.flux_namespace}' (Flux read for {inp.identity_principal_id})"
        )
        return
    manifest = f"""apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {FLUX_READER_ROLE}
  namespace: {inp.flux_namespace}
rules:
  - apiGroups: ["kustomize.toolkit.fluxcd.io"]
    resources: ["kustomizations"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["helm.toolkit.fluxcd.io"]
    resources: ["helmreleases"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {binding}
  namespace: {inp.flux_namespace}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {FLUX_READER_ROLE}
subjects:
  - apiGroup: rbac.authorization.k8s.io
    kind: User
    name: {inp.identity_principal_id}
"""
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    try:
        handle.write(manifest)
        handle.close()
        _run(
            ["kubectl", "apply", "-f", handle.name],
            description=f"Apply Flux read RBAC ({binding})",
        )
        display_result(
            f"Flux Kustomization/HelmRelease read granted to {inp.identity_principal_id} in "
            f"'{inp.flux_namespace}'"
        )
    finally:
        os.unlink(handle.name)


def _ensure_rbac(inp: OnboardInputs) -> None:
    console.print("\n[bold]Ensuring Azure RBAC...[/bold]")
    # 5. Cluster User (get kubeconfig).
    _assign_role(
        inp, AKS_CLUSTER_USER_ROLE, inp.cluster_resource_id, "AKS Cluster User (cluster scope)"
    )
    # 6. Namespace-scoped least-privilege deploy (custom role).
    _ensure_custom_deploy_role(inp)
    _assign_role(
        inp,
        inp.deploy_role_name,
        inp.namespace_scope,
        f"Custom deploy role on namespace '{inp.namespace}'",
    )
    # Flux Kustomization read for the CI-mode suspend pre-check. Azure RBAC for Kubernetes
    # has no dataAction for the Flux CRD group, so this is granted via native k8s RBAC.
    _ensure_flux_read_rbac(inp)
    # 7. Key Vault Secrets User (acceptance-test secrets).
    if inp.keyvault:
        kv = _az_json(["keyvault", "show", "--name", inp.keyvault], check=False)
        if kv and kv.get("id"):
            inp.kv_secret_names = _list_kv_secret_names(inp.keyvault)
            _assign_role(
                inp,
                KEY_VAULT_SECRETS_USER_ROLE,
                kv["id"],
                f"Key Vault Secrets User on '{inp.keyvault}'",
            )
        else:
            console.print(
                f"  [warning]Key Vault '{inp.keyvault}' not found; skipping KV grant.[/warning]"
            )
    else:
        console.print(
            "  [info]No --keyvault given; skipping Key Vault grant (set it for integration tests).[/info]"
        )


def _render_seed_manifest(inp: OnboardInputs) -> str:
    """Render the ConfigMap (seed script) + Job that adds the CI identity to entitlements."""
    indented = "\n".join("    " + ln for ln in _SEED_SCRIPT.strip("\n").splitlines())
    return f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: {SEED_JOB_NAME}
  namespace: {inp.namespace}
data:
  seed_member.py: |
{indented}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {SEED_JOB_NAME}
  namespace: {inp.namespace}
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 600
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels:
        azure.workload.identity/use: "true"
    spec:
      serviceAccountName: {WORKLOAD_IDENTITY_SA}
      restartPolicy: Never
      containers:
        - name: seed
          image: {SEED_IMAGE}
          command: ["python", "/seed/seed_member.py"]
          env:
            - name: PARTITION
              value: "{inp.partition}"
            - name: CI_MSI_APPID
              value: "{inp.identity_client_id}"
            - name: ENTITLEMENTS_HOST
              value: "http://entitlements.{inp.namespace}.svc.cluster.local"
          volumeMounts:
            - name: seed
              mountPath: /seed
              readOnly: true
          securityContext:
            allowPrivilegeEscalation: false
            runAsUser: 1000
            capabilities:
              drop: [ALL]
      volumes:
        - name: seed
          configMap:
            name: {SEED_JOB_NAME}
"""


def _ensure_entitlements_membership(inp: OnboardInputs) -> None:
    """Seed the onboarded CI identity into the partition's entitlements root groups.

    `spi up` only provisions the tenant (the deployer UAMI becomes OWNER); no other identity
    is granted access. With per-identity JWT projection a CI identity flows as itself and so
    403s every user-facing service until it is a real member. This runs a short in-cluster Job
    under the OSDU workload identity (the OWNER, authorized to AddMember) that POSTs the CI
    identity's appid into users, users.datalake.ops, users.datalake.admins and users.data.root
    for the partition. Idempotent: AddMember returns 409 for an existing member, which the Job
    treats as success.
    """
    console.print("\n[bold]Seeding entitlements membership...[/bold]")
    if not inp.identity_client_id:
        console.print("  [warning]identity client id unknown; skipping entitlements seed[/warning]")
        return
    groups_human = ", ".join(ENTITLEMENTS_SEED_GROUPS)
    if inp.dry_run:
        _plan(
            f"kubectl apply Job {SEED_JOB_NAME} (run as {WORKLOAD_IDENTITY_SA}) adding "
            f"{inp.identity_client_id} to [{groups_human}]@{inp.partition}.<domain>"
        )
        return

    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    try:
        handle.write(_render_seed_manifest(inp))
        handle.close()
        # The Job spec is immutable; clear any prior run before applying.
        _run(
            ["kubectl", "delete", "job", SEED_JOB_NAME, "-n", inp.namespace, "--ignore-not-found"],
            description="Clear any prior entitlements seed Job",
            check=False,
        )
        _run(
            ["kubectl", "apply", "-f", handle.name],
            description=f"Apply entitlements seed Job for {inp.identity_client_id}",
        )
        subprocess.run(
            _resolve(
                [
                    "kubectl",
                    "wait",
                    "--for=condition=complete",
                    f"job/{SEED_JOB_NAME}",
                    "-n",
                    inp.namespace,
                    "--timeout=240s",
                ]
            ),
            capture_output=True,
            text=True,
        )
        logs = (
            subprocess.run(
                _resolve(
                    ["kubectl", "logs", f"job/{SEED_JOB_NAME}", "-n", inp.namespace, "--tail=40"]
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout
            or ""
        )
        for line in logs.splitlines():
            if line.strip():
                console.print(f"    [dim]{line}[/dim]")
        if "RESULT rc=0" not in logs:
            console.print(
                "  [error]Entitlements seed did not complete; see Job logs above. Confirm the "
                "stack is deployed and tenant-provisioning has run.[/error]"
            )
            raise typer.Exit(code=1)
        display_result(
            f"CI identity {inp.identity_client_id} seeded into [{groups_human}]@{inp.partition}"
        )
    finally:
        _run(
            [
                "kubectl",
                "delete",
                "job",
                SEED_JOB_NAME,
                "-n",
                inp.namespace,
                "--ignore-not-found",
                "--wait=false",
            ],
            description="Remove entitlements seed Job",
            check=False,
        )
        _run(
            [
                "kubectl",
                "delete",
                "configmap",
                SEED_JOB_NAME,
                "-n",
                inp.namespace,
                "--ignore-not-found",
                "--wait=false",
            ],
            description="Remove entitlements seed ConfigMap",
            check=False,
        )
        os.unlink(handle.name)


def _list_kv_secret_names(vault: str) -> List[str]:
    data = _az_json(
        ["keyvault", "secret", "list", "--vault-name", vault, "--query", "[].name"], check=False
    )
    return data or []


# --------------------------------------------------------------------------------------
# Step 8 - write handoff secrets + variables to the repo
# --------------------------------------------------------------------------------------
def _gh_set_secret(inp: OnboardInputs, name: str, value: str) -> None:
    if inp.dry_run:
        _plan(f"gh secret set {name} -R {inp.repo} (value hidden)")
        return
    proc = subprocess.run(
        _resolve(["gh", "secret", "set", name, "-R", inp.repo, "--body", value]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        console.print(
            f"  [error]gh secret set {name} failed: {(proc.stderr or '').strip()}[/error]"
        )
        raise typer.Exit(code=1)
    console.print(f"  [success]secret {name} set[/success]")


def _gh_set_variable(inp: OnboardInputs, name: str, value: str) -> None:
    if inp.dry_run:
        _plan(f"gh variable set {name}={value} -R {inp.repo}")
        return
    # `gh variable set` updates if present, so this is idempotent.
    proc = subprocess.run(
        _resolve(["gh", "variable", "set", name, "-R", inp.repo, "--body", value]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        console.print(
            f"  [error]gh variable set {name} failed: {(proc.stderr or '').strip()}[/error]"
        )
        raise typer.Exit(code=1)
    console.print(f"  [success]variable {name}={value} set[/success]")


def _gh_delete_variable(inp: OnboardInputs, name: str) -> None:
    if inp.dry_run:
        _plan(f"gh variable delete {name} -R {inp.repo}")
        return
    proc = subprocess.run(
        _resolve(["gh", "variable", "delete", name, "-R", inp.repo]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        error = (proc.stderr or "").strip()
        if "HTTP 404" in error:
            return
        console.print(f"  [error]gh variable delete {name} failed: {error}[/error]")
        raise typer.Exit(code=1)
    console.print(f"  [success]variable {name} removed[/success]")


def _gh_get_variable(inp: OnboardInputs, name: str) -> str:
    """Return the current value of a repo Actions variable, or '' if unset/unreadable.

    Used to detect a re-home: if AZURE_CLIENT_ID already names a *different* identity, the repo
    is being moved from a previous cluster to this one.
    """
    proc = subprocess.run(
        _resolve(["gh", "api", f"repos/{inp.repo}/actions/variables/{name}", "--jq", ".value"]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        error = (proc.stderr or "").strip()
        if "HTTP 404" in error:
            return ""
        console.print(f"  [error]gh variable read {name} failed: {error}[/error]")
        raise typer.Exit(code=1)
    return proc.stdout.strip()


def _resolve_no_data_access_profile(inp: OnboardInputs) -> None:
    if inp.no_data_access_token_env is not None:
        return
    existing = _gh_get_variable(inp, "NO_DATA_ACCESS_TOKEN_ENV")
    inp.no_data_access_token_env = existing or NO_DATA_ACCESS_TOKEN_ENVS.get(
        inp.service.lower(), ""
    )
    if inp.no_data_access_token_env:
        console.print(
            f"  [info]Negative-authorization token enabled as {inp.no_data_access_token_env}[/info]"
        )
    else:
        console.print("  [info]No negative-authorization token required for this profile[/info]")


def _should_write_secrets(secret_present: bool, is_rehome: bool, force: bool) -> bool:
    """Decide whether to (re)write the AZURE_* secrets.

    Write when they are missing, when the identity changed (a re-home onto a new cluster), or
    when explicitly forced. Skipping only the true idempotent case -- the same identity is
    already set -- keeps the AZURE_CLIENT_ID secret and its variable copy from ever diverging.
    """
    return (not secret_present) or is_rehome or force


def _write_handoff(inp: OnboardInputs) -> None:
    console.print("\n[bold]Writing handoff secrets + variables to the repo...[/bold]")

    # Re-home detection: the AZURE_CLIENT_ID variable records the identity (and therefore the
    # cluster) the repo is currently linked to. A different value means this is a move from a
    # previous cluster onto this one -- so we must repoint the AZURE_* secrets too, not just the
    # variables, or azure/login would keep authenticating as the retired cluster's identity while
    # everything else points here. (Model: one repo <-> one cluster; this makes retire-A /
    # onboard-B seamless in a single command.)
    existing_client_id = _gh_get_variable(inp, "AZURE_CLIENT_ID")
    is_rehome = (
        bool(existing_client_id)
        and bool(inp.identity_client_id)
        and existing_client_id != inp.identity_client_id
    )
    if is_rehome:
        console.print(
            f"  [warning]Re-homing {inp.repo}: AZURE_CLIENT_ID {existing_client_id} -> "
            f"{inp.identity_client_id} (repointing the repo from the previous cluster's "
            "identity to this one).[/warning]"
        )

    secret_present = _secret_present(inp, "AZURE_CLIENT_ID")
    if _should_write_secrets(secret_present, is_rehome, inp.force_rewrite_secrets):
        _gh_set_secret(inp, "AZURE_CLIENT_ID", inp.identity_client_id)
        _gh_set_secret(inp, "AZURE_TENANT_ID", inp.tenant_id)
        _gh_set_secret(inp, "AZURE_SUBSCRIPTION_ID", inp.subscription_id)
    else:
        console.print(
            "  [info]AZURE_* secrets already current for this identity; leaving as-is "
            "(use --force-rewrite-secrets to overwrite).[/info]"
        )

    # Variables (always reconcile; non-sensitive). Together with the AZURE_* secrets these fully
    # pin the repo->cluster link, so a single `spi onboard` against a new cluster re-homes the
    # repo with no manual variable edits.
    _gh_set_variable(inp, "K8S_DEPLOYMENT_NAME", inp.deployment_name)
    _gh_set_variable(inp, "K8S_CONTAINER_NAME", inp.container_name)
    # AZURE_CLIENT_ID as a variable too: validate.yml's `if:` gates on `vars.AZURE_CLIENT_ID`,
    # and the next onboard reads it for re-home detection (above). Kept in lock-step with the
    # secret written above so the two never diverge (design SS6.1 step 5).
    _gh_set_variable(inp, "AZURE_CLIENT_ID", inp.identity_client_id)
    # Cluster-routing variables -- onboard already knows these from its own arguments, so it
    # writes them to remove the manual "set the AKS_*/KEYVAULT_NAME vars" step and to repoint
    # them on a re-home.
    _gh_set_variable(inp, "AKS_RESOURCE_GROUP", inp.aks_rg)
    _gh_set_variable(inp, "AKS_CLUSTER_NAME", inp.aks_cluster)
    _gh_set_variable(inp, "K8S_NAMESPACE", inp.namespace)
    _gh_set_variable(inp, "FLUX_NAMESPACE", inp.flux_namespace)
    # AAD_CLIENT_ID is the resource/audience the integration-test mints the acceptance-test
    # token for, NOT an identity. SPI MSIs can only mint ARM-audience tokens, so this is a
    # constant; the CI identity is carried by AZURE_CLIENT_ID (the token's appid). See
    # AAD_TOKEN_RESOURCE.
    _gh_set_variable(inp, "AAD_CLIENT_ID", AAD_TOKEN_RESOURCE)
    no_data_variables = (
        "NO_DATA_ACCESS_TESTER_CLIENT_ID",
        "NO_DATA_ACCESS_TESTER_PRINCIPAL_ID",
        "NO_DATA_ACCESS_TESTER_IDENTITY_NAME",
        "NO_DATA_ACCESS_TOKEN_ENV",
    )
    if inp.uses_no_data_access_identity:
        _gh_set_variable(
            inp,
            "NO_DATA_ACCESS_TESTER_CLIENT_ID",
            inp.no_data_access_identity_client_id,
        )
        _gh_set_variable(
            inp,
            "NO_DATA_ACCESS_TESTER_PRINCIPAL_ID",
            inp.no_data_access_identity_principal_id,
        )
        _gh_set_variable(
            inp,
            "NO_DATA_ACCESS_TESTER_IDENTITY_NAME",
            inp.no_data_access_identity_name,
        )
        _gh_set_variable(
            inp,
            "NO_DATA_ACCESS_TOKEN_ENV",
            inp.resolved_no_data_access_token_env,
        )
    else:
        for variable_name in no_data_variables:
            _gh_delete_variable(inp, variable_name)
    if inp.keyvault:
        _gh_set_variable(inp, "KEYVAULT_NAME", inp.keyvault)
    if inp.gateway_url:
        _gh_set_variable(inp, "GATEWAY_URL", inp.gateway_url)


def _secret_present(inp: OnboardInputs, name: str) -> bool:
    proc = subprocess.run(
        _resolve(["gh", "secret", "list", "-R", inp.repo]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    return any(line.split("\t")[0].strip() == name for line in proc.stdout.splitlines())


# --------------------------------------------------------------------------------------
# Step 9 - JSON summary
# --------------------------------------------------------------------------------------
def _emit_summary(inp: OnboardInputs) -> None:
    summary = {
        "service": inp.service,
        "repo": inp.repo,
        "dry_run": inp.dry_run,
        "identity": {
            "name": inp.identity_name,
            "client_id": inp.identity_client_id,
            "principal_id": inp.identity_principal_id,
        },
        "no_data_access_identity": (
            {
                "enabled": True,
                "name": inp.no_data_access_identity_name,
                "client_id": inp.no_data_access_identity_client_id,
                "principal_id": inp.no_data_access_identity_principal_id,
                "token_env": inp.resolved_no_data_access_token_env,
                "azure_rbac": "none",
                "osdu_entitlements": "none",
            }
            if inp.uses_no_data_access_identity
            else {"enabled": False}
        ),
        "cluster": {
            "name": inp.aks_cluster,
            "resource_group": inp.aks_rg,
            "namespace": inp.namespace,
            "flux_namespace": inp.flux_namespace,
        },
        "secrets_written": ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"],
        "variables_written": {
            "K8S_DEPLOYMENT_NAME": inp.deployment_name,
            "K8S_CONTAINER_NAME": inp.container_name,
            "AZURE_CLIENT_ID": inp.identity_client_id,
            "AKS_RESOURCE_GROUP": inp.aks_rg,
            "AKS_CLUSTER_NAME": inp.aks_cluster,
            "K8S_NAMESPACE": inp.namespace,
            "FLUX_NAMESPACE": inp.flux_namespace,
            **(
                {
                    "NO_DATA_ACCESS_TESTER_CLIENT_ID": (inp.no_data_access_identity_client_id),
                    "NO_DATA_ACCESS_TESTER_PRINCIPAL_ID": (
                        inp.no_data_access_identity_principal_id
                    ),
                    "NO_DATA_ACCESS_TESTER_IDENTITY_NAME": (inp.no_data_access_identity_name),
                    "NO_DATA_ACCESS_TOKEN_ENV": (inp.resolved_no_data_access_token_env),
                }
                if inp.uses_no_data_access_identity
                else {}
            ),
            **({"KEYVAULT_NAME": inp.keyvault} if inp.keyvault else {}),
            **({"GATEWAY_URL": inp.gateway_url} if inp.gateway_url else {}),
        },
        "kv_secret_names_to_populate": inp.kv_secret_names,
        "next_steps": [
            *(
                []
                if inp.gateway_url
                else [
                    "Set GATEWAY_URL on the repo (cluster ingress base URL); pass --gateway-url "
                    "next time to have onboard write it."
                ]
            ),
            "Set the per-service test variables: ACCEPTANCE_TEST_DIR, ACCEPTANCE_TEST_SECRET_MAP, "
            "ACCEPTANCE_TEST_DEPENDENCIES (and ACCEPTANCE_TEST_ENV_MAP if the suite reads "
            "non-secret config such as PARTITION_BASE_URL / MY_TENANT).",
            f"Run settings-apply.yml on {inp.repo} (or wait for its schedule) to reconcile "
            "rulesets, required-check filtering, and GHCR visibility.",
            "Populate the Key Vault secret VALUES out of band (this command grants read access "
            "but does not set values).",
        ],
    }
    console.print("\n[bold]Onboarding summary[/bold]")
    console.print_json(json.dumps(summary))


def onboard(inp: OnboardInputs) -> None:
    """Run the full cluster-side onboarding flow (design SS9.4.A)."""
    title = f"[bold]spi onboard[/bold] - grant {inp.repo} deploy access to {inp.aks_cluster}"
    if inp.dry_run:
        title += "\n[warning]DRY RUN: planning only, no changes[/warning]"
    from rich.panel import Panel

    console.print(Panel(title, border_style="cyan"))

    _check_preconditions(inp)
    _resolve_no_data_access_profile(inp)
    _verify_deployment(inp)
    _ensure_identity(inp)
    _ensure_federated_credentials(inp)
    if inp.uses_no_data_access_identity:
        _ensure_no_data_access_identity(inp)
        _ensure_no_data_access_federated_credentials(inp)
    else:
        _remove_no_data_access_federated_credentials(inp)
    _ensure_rbac(inp)
    _ensure_entitlements_membership(inp)
    _write_handoff(inp)
    _emit_summary(inp)

    if inp.dry_run:
        console.print("\n[warning]Dry run complete - no changes were made.[/warning]")
    else:
        display_result(f"Onboarding complete for {inp.repo}")
