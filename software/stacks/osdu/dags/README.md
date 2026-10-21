# Core DAG inputs

The core Airflow overlay includes this directory as local configuration.
Kustomize copies the generated data into `spec.values.spiDagFiles`; the
Airflow chart creates `platform/spi-osdu-dag-files` through `extraConfigMaps`.
The generator is never applied. Its contents contribute to the chart's
`checksum/extra-configmaps` on both Deployments. No embedded file may contain
a Flux substitution opener; `tests/test_dag_loader.py` enforces this.

[ADR-044](../../../../docs/decisions/044-core-profile-dags.md) owns the
delivery and identity decisions. A catalog edit approves code execution as
the OSDU identity, not only a new Workflow name.

## Catalog and loader

`catalog.yaml` contains the two families from issue #392. The ingestion
commit is the target of upstream's
[v0.30.0 tag](https://community.opengroup.org/osdu/platform/data-flow/ingestion/ingestion-dags/-/tags/v0.30.0).
Both SHA-256 values match the raw files at that commit.

`load.py` fetches a GitLab archive restricted to `strip_prefix`, inspects
every member without extracting it, and verifies the selected file hashes.
Only cataloged files reach `dags/<family>/`. Paths, links, duplicate members,
missing files and hash failures are rejected before any destination writes.
A successful retry replaces an interrupted load in the pod's own emptyDir.
Download, member-count and file-size limits bound archive processing.

The two local DAGs use the Airflow 3 SDK. `Echo_DAG` retains
`list_packages_task`, logs installed packages and returns the inventory as
an XCom. `airflow_monitoring` retains the `echo` task and its `test` result,
which Workflow's latest-task acceptance checks can read. Neither DAG calls
OSDU or prints a run's credentials.

## Python lock

The image is `apache/airflow:3.2.2`, whose default Python is 3.13.
`requirements.in` pins `osdu-api==1.2.4`, `osdu-airflow==0.30.9` and
`osdu-ingestion==0.30.2`, without extras. The image supplies its Azure SDK;
the `azure` extra would install another version, and the `AF3` extra
requires Airflow 3.3.0.

Regenerate from the repository root:

```sh
uv run python scripts/lock_core_dags.py
```

The script invokes `uv --no-config pip compile` for Linux/Python 3.13,
isolating resolution from the CLI's own package indexes. Public PyPI
metadata supplies SHA-256 hashes; some mirrors also advertise MD5, which
pip's hash mode refuses. uv omits hashes for the remote find-links wheels,
so the script downloads those three wheels and verifies their SHA-256
against the registry before writing their pins. The anonymous Microsoft PyPI mirror
can serve the same wheels at runtime with `PIP_INDEX_URL`; it is not a
second OSDU index. The three OSDU registry URLs remain `--find-links` pages
so a missing package cannot be forwarded to PyPI by a GitLab extra index.
The constraints used for this lock have SHA-256
`b3e3c347186f40738abb0e0529affb7ac9f7de8e3cb6bbc8df7be66a5bcfcfd0`.

Both main containers let the image entrypoint install
`-r /opt/spi/osdu/requirements.txt --require-hashes --only-binary :all:`.
The loader bypasses that install and uses Python and PyYAML already in the
image. The scheduler's startup probe permits 18 failures. The processor
has a wider liveness budget and a post-rendered readiness probe because
chart 1.22.0 exposes no processor startup or readiness probe. Running pip
alone must not mark that pod Ready and retire the old parser.

## Identity and service endpoints

The scheduler is the only Airflow component using
`platform/workload-identity-sa`. The webhook supplies its client, tenant
and federated token file. `CLOUD_PROVIDER=azure` and
`AZURE_ENABLE_WORKLOAD_IDENTITY=true` select the library's Workload
Identity path. `AZURE_RESOURCE_ID` is the service audience, not the
identity's Azure resource path.

`spi up` copies the effective `osdu-config/AAD_CLIENT_ID` into
`osdu-flux/spi-cluster-config`. `spi reconcile` also copies it from the
running environment before requesting reconciliation, including on
v0.30.x upgrades. Read failure retains the previous value. An absent key
uses `https://management.azure.com`, the existing bootstrap audience
accepted by the mesh (ADR-016), not an empty substitution. An explicitly
configured application audience is preserved.

The seven `AIRFLOW_VAR_CORE__SERVICE__*__URL` values use the `osdu`
namespace's Service names and the same versioned API roots as the service
manifests. The SDK appends endpoint paths, so roots have no trailing slash.

Caller propagation is in the pinned sources:

- [osdu-api 1.2.4 `BaseClient.prepare_headers`](https://community.opengroup.org/osdu/platform/system/sdks/common-python-sdk/-/blob/5e856f1c99516a83353f17cef058bfadee50dbf6/src/osdu_api/clients/base_client.py#L381)
  sends `on-behalf-of` and `x-on-behalf-of` for a caller.
- [osdu-airflow 0.30.9 manifest processor](https://community.opengroup.org/osdu/platform/data-flow/ingestion/osdu-airflow-lib/-/blob/cb8bf7261f08f58aceee073d0ceae1914e26eec4/osdu_airflow/operators/process_manifest_r3.py#L179)
  passes the run's `execution_context.userId` into Storage, Search and Schema
  clients. The integrity, validation and status operators use the same
  path.
- Both libraries exclude tenant identities ending in `@service.local`.
  Azure deploy identities are GUIDs and people retain their user IDs, so
  neither acceptance caller is excluded.

No token, client secret, operator enrollment or remote log store is added.
