# Installation

SPI Stack is distributed as a versioned Python wheel attached to each
[GitHub Release](https://github.com/Azure/osdu-spi-stack/releases). The
[`uv`](https://docs.astral.sh/uv/) tool installs and manages the `spi` executable.

## Latest release

The latest-release commands resolve the newest wheel from GitHub, so they do not
contain a version number.

**macOS and Linux**

```bash
uv tool install --default-index https://packagefeedproxy.microsoft.io/pypi/simple/ \
  "$(curl -fsSL https://api.github.com/repos/Azure/osdu-spi-stack/releases/latest \
  | grep -o 'https://github.com/Azure/osdu-spi-stack/releases/download/[^"]*-py3-none-any.whl')"
```

**Windows PowerShell**

```powershell
$wheel = (irm https://api.github.com/repos/Azure/osdu-spi-stack/releases/latest).assets.where({ $_.name -like '*-py3-none-any.whl' }).browser_download_url
uv tool install --default-index https://packagefeedproxy.microsoft.io/pypi/simple/ $wheel
```

The explicit index is the anonymous Microsoft PyPI proxy. A released wheel does
not inherit the index declared in this repository's `pyproject.toml`, and uv does
not read the corporate `pip.ini`.

Verify the installed version:

```bash
spi --version
```

The installed `spi` executable is on `PATH`. Commands in this guide do not require
the `uv run` prefix.

## Pinned release

Install a specific wheel for CI, reproducible environments, or bug reports:

```bash
uv tool install --default-index https://packagefeedproxy.microsoft.io/pypi/simple/ \
  https://github.com/Azure/osdu-spi-stack/releases/download/v0.1.0/spi-0.1.0-py3-none-any.whl
```

Copy the wheel URL for another version from its
[release page](https://github.com/Azure/osdu-spi-stack/releases).

## Permissions

`spi up` needs two role assignments on the deployment subscription:

- **Contributor**, to create the resource group and every resource in it.
- **Role Based Access Control Administrator** with a condition that allows
  creating and deleting assignments only for the eleven roles the stack uses.
  Contributor cannot create role assignments, and `spi up` assigns these roles
  to the identities it creates and to the deployer.

Owner and User Access Administrator are not needed. Both assignments sit at
subscription scope, which also covers the DNS zone's resource group in `dns`
ingress mode and remains after `spi down`.

`spi check` reads the signed-in identity's permissions and, when one is
missing, prints the exact commands with the object id filled in. `spi up` runs
the same check and stops before creating anything. The check does not evaluate
assignment conditions or deny assignments.

An administrator grants both for a deployer. `--assignee-principal-type` is
`ServicePrincipal` for an automation identity:

```bash
DEPLOYER=<deployer-object-id>

az role assignment create \
  --role "Contributor" \
  --assignee-object-id "$DEPLOYER" \
  --assignee-principal-type User \
  --scope /subscriptions/<subscription-id>

az role assignment create \
  --role "Role Based Access Control Administrator" \
  --assignee-object-id "$DEPLOYER" \
  --assignee-principal-type User \
  --scope /subscriptions/<subscription-id> \
  --condition-version 2.0 \
  --condition '((!(ActionMatches{'\''Microsoft.Authorization/roleAssignments/write'\''})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4d97b98b-1d4f-4787-a291-c67834d212e7, b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b, 4abbcc35-e782-43d8-92c5-2d3f1bd2253f, b86a8fe4-44ce-4948-aee5-eccb2c155cd7, 4633458b-17de-408a-b874-0445c86b69e6, ba92f5b4-2d11-453d-a403-e96b0029c9fe, 0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3, 69a216fc-b8fb-44d8-bc22-1f3c2cd27a39, 4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0, 7f951dda-4ed3-4680-a7ca-43fe172d538d, befefa01-2a29-4197-83a8-272ff33ce314})) AND ((!(ActionMatches{'\''Microsoft.Authorization/roleAssignments/delete'\''})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4d97b98b-1d4f-4787-a291-c67834d212e7, b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b, 4abbcc35-e782-43d8-92c5-2d3f1bd2253f, b86a8fe4-44ce-4948-aee5-eccb2c155cd7, 4633458b-17de-408a-b874-0445c86b69e6, ba92f5b4-2d11-453d-a403-e96b0029c9fe, 0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3, 69a216fc-b8fb-44d8-bc22-1f3c2cd27a39, 4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0, 7f951dda-4ed3-4680-a7ca-43fe172d538d, befefa01-2a29-4197-83a8-272ff33ce314}))'
```

The condition lists these role definitions:

| Role | Definition id |
|---|---|
| Network Contributor | `4d97b98b-1d4f-4787-a291-c67834d212e7` |
| Azure Kubernetes Service RBAC Cluster Admin | `b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b` |
| Azure Kubernetes Service Cluster User Role | `4abbcc35-e782-43d8-92c5-2d3f1bd2253f` |
| Key Vault Secrets Officer | `b86a8fe4-44ce-4948-aee5-eccb2c155cd7` |
| Key Vault Secrets User | `4633458b-17de-408a-b874-0445c86b69e6` |
| Storage Blob Data Contributor | `ba92f5b4-2d11-453d-a403-e96b0029c9fe` |
| Storage Table Data Contributor | `0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3` |
| Azure Service Bus Data Sender | `69a216fc-b8fb-44d8-bc22-1f3c2cd27a39` |
| Azure Service Bus Data Receiver | `4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0` |
| AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` |
| DNS Zone Contributor | `befefa01-2a29-4197-83a8-272ff33ce314` |

## Upgrade

The installed CLI resolves newer wheels from GitHub Releases:

```bash
spi update           # Check for and install a newer version
spi update --check   # Report whether an update is available
spi update --force   # Reinstall the latest version
```

On native Windows installations managed by `uv`, `spi update` exits before
replacing its active tool environment. Run the recovery command it prints from a
new terminal:

```powershell
uv tool install --force --default-index https://packagefeedproxy.microsoft.io/pypi/simple/ <wheel-url>
```

## Git installation

`uv` can install directly from a Git tag:

```bash
uv tool install --default-index https://packagefeedproxy.microsoft.io/pypi/simple/ \
  git+https://github.com/Azure/osdu-spi-stack.git@v0.1.0
```

Release wheels are preferred because they preserve the tag-derived value reported
by `spi --version`.
